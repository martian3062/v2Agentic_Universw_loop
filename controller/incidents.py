"""Incidents: hallucination response (C8), KPI drift alerts, and the incident records that caller-ID health
(callerid.py, C5) also uses.

Detection is threshold-based on populations: a minimum sample, a minimum effect size, and an exact binomial
tail (hallucination flags are rare counts, where a normal approximation is badly anti-conservative).
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict

from . import settings as S
from .api import ApiError
from .db import Store, atomic
from .estimates import volume
from .stats import binom_sf, count_z, fixed_z
from .util import fmt

log = logging.getLogger("controller.incidents")


def baselines(s: Store, wid: str) -> dict:
    """Pre-D0 behaviour under the business default config, from the history chunks."""
    b = s.get(f"baseline:{wid}")
    if b:
        return b
    hod = defaultdict(lambda: [0, 0])
    for r in s.q("SELECT hour, SUM(conn) c, SUM(dials) d FROM agg_hour WHERE workflow_id=? AND hour<0 GROUP BY hour",
                 (wid,)):
        h = r["hour"] % 24
        hod[h][0] += r["c"]
        hod[h][1] += r["d"]
    t = s.one("SELECT SUM(dials) d, SUM(conn) c, SUM(eng) e, SUM(flags) f, SUM(qa_n) q FROM agg_hour "
              "WHERE workflow_id=? AND hour<0", (wid,))
    b = {"pickup_by_hod": {str(h): v for h, v in hod.items()},
         "pickup": [t["c"] or 0, t["d"] or 0], "engagement": [t["e"] or 0, t["c"] or 0],
         "flag": [t["f"] or 0, t["q"] or 0]}
    if t["d"]:
        with s:
            s.set(f"baseline:{wid}", b)
    return b


def _rate(pair, default=0.0):
    return pair[0] / pair[1] if pair and pair[1] else default


def engagement_margin(wf: dict) -> float:
    """How far engagement may drop under a change, from the workflow's own objective: its guardrail margin when
    engagement is the guardrail (e.g. {"engagement_rate_max_drop": 0.005}). When engagement is the primary KPI no
    margin is configured; we then use settings.PRIMARY_ENGAGEMENT_MAX_DROP."""
    g = (wf.get("objective") or {}).get("guardrail") or {}
    if "engagement_rate_max_drop" in g:
        return float(g["engagement_rate_max_drop"])
    return S.PRIMARY_ENGAGEMENT_MAX_DROP


class _PolicyCallbacks:
    """Governance callbacks for standing policies (owner 'pol:...'): they just leave an audit line."""

    def __init__(self, inc):
        self.inc = inc

    def on_applied(self, intent, minute, version):
        self.inc._policy_callback(intent, minute, f"applied in v{version}")

    def on_returned(self, intent, minute, why, reasons):
        self.inc._policy_callback(intent, minute, f"returned ({why}: {'; '.join(reasons)})")


class Incidents:
    def __init__(self, store: Store, sim, gov, workflows: dict):
        self.s, self.sim, self.gov, self.wfs = store, sim, gov, workflows
        gov.callbacks["inc"] = self
        gov.callbacks["pol"] = _PolicyCallbacks(self)
        self.cid = None   # set by callerid.CallerIds

    # ------------------------------------------------------------ helpers
    def open_incident(self, wid: str, kind: str):
        return self.s.q("SELECT * FROM incidents WHERE workflow_id=? AND kind=? AND status NOT IN "
                        "('resolved','abandoned')", (wid, kind))

    def _new(self, minute: int, wid: str, kind: str, key: str, data: dict, summary: str) -> str:
        n = self.s.next_seq("incident")
        iid = f"inc-{n:04d}"
        with self.s:
            self.s.x("INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?)",
                     (iid, wid, kind, key, "open", minute, None, json.dumps(data)))
            self.s.record(minute, wid, "incident_opened", summary, data, ref=f"inc:{iid}")
            self.s.alert(f"incident:{iid}", minute, "warn", wid, summary)
        return iid

    def _save(self, iid: str, status: str, data: dict, minute: int | None = None, note: str | None = None):
        with self.s:
            self.s.x("UPDATE incidents SET status=?, data=?, resolved_minute=COALESCE(?, resolved_minute) "
                     "WHERE incident_id=?",
                     (status, json.dumps(data), minute if status in ("resolved", "abandoned") else None, iid))
            if status in ("resolved", "abandoned") and minute is not None:
                self.s.resolve_alert(f"incident:{iid}", minute)
                self.s.resolve_alert(f"incident_age:{iid}", minute)

    # ------------------------------------------------------------ dispatch
    def quarantined(self, wid: str, minute: int | None = None) -> set:
        """Caller-ID numbers out of rotation (owned by callerid.CallerIds)."""
        return self.cid.resting(wid, minute) if self.cid else set()

    def owns_dim(self, wid: str, dim: str) -> bool:
        """While an incident is open on a dimension it owns it; experiments stand aside."""
        if dim == "caller_id":
            return bool(self.open_incident(wid, "caller_id"))
        if dim == "prompt_version":
            return bool(self.open_incident(wid, "hallucination"))
        return False

    def tick(self, minute: int):
        for wid in self.wfs:
            if self.in_dialling_window(wid, minute):
                self.detect_hallucination(minute, wid)
                self.detect_drift(minute, wid)
            for inc in self.s.q("SELECT * FROM incidents WHERE workflow_id=? AND status NOT IN ('resolved','abandoned')",
                                (wid,)):
                if inc["kind"] == "caller_id":
                    if self.cid:
                        self.cid.progress(minute, inc)
                else:
                    self.progress_hallucination(minute, inc)
                inc = self.s.one("SELECT * FROM incidents WHERE incident_id=?", (inc["incident_id"],))
                if inc["status"] not in ("resolved", "abandoned") and \
                        minute - inc["opened_minute"] > S.PAGE_INCIDENT_OPEN_HOURS * 60:
                    with self.s:
                        self.s.alert(f"incident_age:{inc['incident_id']}", minute, "page", wid,
                                     f"{inc['kind']} incident {inc['incident_id']} open for "
                                     f"{(minute - inc['opened_minute']) // 60}h (status {inc['status']})")

    def in_dialling_window(self, wid: str, minute: int) -> bool:
        a, b = self.wfs[wid]["dialling_window"]
        return a * 60 < minute % 1440 <= b * 60 + 15

    def _policy_callback(self, intent, minute, what):
        with self.s:
            self.s.record(minute, intent["workflow_id"], f"policy_{what.split()[0]}", f"{intent['summary']}: {what}",
                          None, ref=intent["owner"])

    # ------------------------------------------------------------ hallucination
    def detect_hallucination(self, minute: int, wid: str):
        if self.open_incident(wid, "hallucination"):
            return
        base = baselines(self.s, wid)
        p0 = max(_rate(base["flag"], 0.015), 0.002)
        _, cfg = self.gov.current(wid)
        live = cfg.get("prompt_version")   # a prompt applies to every call; judge the one that is dialling now
        windows = [(minute // 60 - S.HAL_WINDOW_HOURS, S.HAL_P, f"last {S.HAL_WINDOW_HOURS}h")]
        # A prompt change is a known change point: for 12h after one, also test only the calls since the change,
        # which is not diluted by the old prompt, at a looser level (few looks per change, ~0.5% false alarms).
        ch = self.s.one("SELECT MAX(applied_minute) m FROM config_versions WHERE workflow_id=? AND changed_dims LIKE "
                        "'%prompt_version%'", (wid,))
        if ch and ch["m"] is not None and 0 <= minute - ch["m"] <= 12 * 60:
            windows.append((ch["m"] // 60, S.HAL_P_AFTER_CHANGE, f"since the prompt change at {fmt(ch['m'])}"))
        for since, level, label in windows:
            r = self.s.one("SELECT SUM(flags) f, SUM(qa_n) n FROM agg_hour WHERE workflow_id=? AND hour>=? AND prompt=?",
                           (wid, since, live))
            f, n = (r["f"] or 0, r["n"] or 0) if r else (0, 0)
            if n < S.HAL_MIN_CONNECTED or f < S.HAL_MIN_FLAGS or f / n < S.HAL_MIN_RATIO * p0:
                continue
            pval = binom_sf(f, n, p0)
            if pval <= level:
                z = count_z(f, n * p0, n * p0 * (1 - p0))
                data = {"prompt": live, "flags": f, "connected": n, "rate": round(f / n, 4),
                        "baseline_rate": round(p0, 4), "z": round(z, 2), "p_value": float(f"{pval:.3g}"),
                        "window": label, "credits_spent": 0, "tried": [], "validated": {}}
                sup = [e for e in self.s.get(f"supervisor_edits:{wid}", []) if "prompt_version" in e["dims"]]
                if sup:
                    data["after_supervisor_edit"] = sup[-1]
                self._new(minute, wid, "hallucination", live, data,
                          f"hallucination flags {f}/{n} = {f / n:.1%} on {live} vs baseline {p0:.1%} "
                          f"(exact p={pval:.2g}, {label})")
                return

    def _prompt_stats(self, wid: str, prompt: str, since_hour: int | None = None):
        sql = "SELECT SUM(flags) f, SUM(qa_n) n, SUM(eng) e, SUM(conn) c, MAX(hour) last FROM agg_hour " \
              "WHERE workflow_id=? AND prompt=?"
        args = [wid, prompt]
        if since_hour is not None:
            sql += " AND hour>=?"
            args.append(since_hour)
        return self.s.one(sql, args)

    @staticmethod
    def _bad_set(data: dict) -> set:
        """Prompts this incident has shown to be bad: the one it opened on, plus every fix that failed."""
        return {data["prompt"], *data.get("tried", [])}

    def _live(self, wid: str) -> tuple[str, str, int | None]:
        """(live prompt, who set the live config version, when)."""
        version, cfg = self.gov.current(wid)
        r = self.s.one("SELECT actor, applied_minute FROM config_versions WHERE workflow_id=? AND version=?",
                       (wid, version))
        return cfg.get("prompt_version"), (r["actor"] if r else "unknown"), (r["applied_minute"] if r else None)

    def _candidates(self, minute: int, wid: str, data: dict) -> list[dict]:
        bad_set = self._bad_set(data)
        p0 = data["baseline_rate"]
        options = set(self.wfs[wid]["options"]["prompt_version"])
        known = []
        for r in self.s.q("SELECT prompt, SUM(flags) f, SUM(qa_n) n, MAX(hour) last FROM agg_hour WHERE workflow_id=? "
                          "GROUP BY prompt", (wid,)):
            if r["prompt"] in bad_set or r["prompt"] not in options or (r["n"] or 0) < 300:
                continue
            z = count_z(r["f"], r["n"] * p0, r["n"] * p0 * (1 - p0))
            known.append({"prompt": r["prompt"], "source": "observed in production", "flag_rate": round(r["f"] / r["n"], 4),
                          "n": r["n"], "z": round(z, 2), "last_hour": r["last"], "good": z < 2})
        good = sorted([k for k in known if k["good"]], key=lambda k: -k["last_hour"])
        bad_known = {k["prompt"] for k in known if not k["good"]}
        cands = [dict(k, rank=0) for k in good]
        for sug in data.get("suggestions", []):
            pv = sug["prompt_version"]
            if pv in bad_set or pv in bad_known or pv not in options or any(c["prompt"] == pv for c in cands):
                continue
            disfavoured = any(w in sug.get("summary", "").lower() for w in S.DISFAVOURED_FIX_WORDS)
            cands.append({"prompt": pv, "source": "llm suggestion", "summary": sug.get("summary"),
                          "rank": 2 if disfavoured else 1, "disfavoured": disfavoured})
        return sorted(cands, key=lambda c: c["rank"])

    def _credits(self) -> float:
        return self.sim.state()["credits_remaining"]

    def may_validate(self, data: dict, cost: int) -> bool:
        """Credit policy for validations. Every incident stays within its ceiling. Spending must leave the reserve
        intact, with one exception: an incident's first validation may dip into the reserve, because without one
        validated candidate the incident cannot be acted on at all (prompt changes need evidence). "First" counts
        charges, not successful results, so a lost response cannot reopen the exception."""
        if data["credits_spent"] + cost > S.HAL_CREDIT_CEILING:
            return False
        credits = self._credits()
        if credits - cost >= S.CREDIT_RESERVE:
            return True
        charged = any(c["kind"] == "validation" and c["status"] != "refunded" for c in data.get("charges", []))
        return not charged and credits - cost >= 0

    def _save_data(self, iid: str, data: dict):
        with self.s:
            self.s.x("UPDATE incidents SET data=? WHERE incident_id=?", (json.dumps(data), iid))

    def _paid_call(self, minute: int, wid: str, iid: str, data: dict, kind: str, cost: int, fn):
        """Spend credits safely: the charge is persisted BEFORE the request, so a crash or a lost response can never
        make spending invisible to the per-incident ceiling. Afterwards the charge is settled against the simulator's
        own credit balance: refunded only if the balance shows the call was not charged, kept otherwise.
        Returns the response, or None if the outcome was not a usable response."""
        assert not self.s.db.in_transaction, "a paid call must not run inside a transaction that could roll back"
        try:
            before = self._credits()
        except ApiError:
            return None   # can't even read the balance: spend nothing this tick
        data["credits_spent"] += cost
        data.setdefault("charges", []).append({"minute": minute, "kind": kind, "cost": cost, "status": "pending"})
        self._save_data(iid, data)
        charge = data["charges"][-1]
        try:
            res = fn()
            charge["status"] = "ok"
            self._save_data(iid, data)
            return res
        except ApiError as e:
            try:
                after = self._credits()
            except ApiError:
                after = None
            if after is not None and after > before - cost / 2:
                charge["status"] = "refunded"
                data["credits_spent"] -= cost
                note = f"{kind} failed ({e.error}) and was not charged"
            else:
                charge["status"] = "lost"
                note = (f"{kind} failed ({e.error}) after the simulator charged {cost} credits (or the balance could not "
                        "be read): the charge stays on this incident's ceiling")
            self._save_data(iid, data)
            with self.s:
                self.s.record(minute, wid, f"{kind}_failed", note, {"error": e.error, "status": e.status,
                                                                    "credits_before": before, "credits_after": after},
                              ref=f"inc:{iid}")
            return None

    def progress_hallucination(self, minute: int, inc):
        wid, iid = inc["workflow_id"], inc["incident_id"]
        owner = f"inc:{iid}"
        data = json.loads(inc["data"])
        current, actor, set_at = self._live(wid)
        st = inc["status"]
        if st in ("open", "retry"):
            if self.gov.open_intents(owner) or minute < data.get("retry_after", 0):
                return
            if current not in self._bad_set(data):
                # The live prompt is one this incident has not judged. Whoever set it (a supervisor, or a fix of ours
                # whose apply we are only now seeing), nothing is resolved until it is verified like any fix.
                data.update(chosen=current, applied_minute=set_at if set_at is not None else minute,
                            chosen_by=actor)
                self._save(iid, "verifying", data)
                with self.s:
                    self.s.record(minute, wid, "fix_verifying",
                                  f"live prompt is now {current} (set by {actor}); verifying it before the incident "
                                  "can close", None, ref=owner)
                return
            choice = self._choose_fix(minute, wid, iid, data)
            if choice is None:
                return   # stays open; _choose_fix pages if no candidate validates
            self._file_prompt_fix(minute, wid, iid, data, choice, current)
        elif st == "proposed" and not self.gov.open_intents(owner) and current != data.get("chosen"):
            # defensive: the request ended without this owner hearing back (cancels now run the owner callback,
            # but a store written by an earlier version may still hold such a request)
            data.pop("chosen", None)
            self._save(iid, "retry", data)
            with self.s:
                self.s.record(minute, wid, "fix_request_cancelled",
                              "prompt-fix request was cancelled; choosing again on fresh data", None, ref=owner)
        elif st == "verifying":
            if current != data.get("chosen"):
                # the prompt under verification is no longer live: judge whatever is live now
                data.pop("chosen", None)
                self._save(iid, "retry", data)
                with self.s:
                    self.s.record(minute, wid, "fix_superseded",
                                  f"prompt under verification was replaced by {current} (set by {actor}); re-assessing",
                                  None, ref=owner)
                return
            since = data["applied_minute"] // 60
            r = self._prompt_stats(wid, data["chosen"], since)
            n = (r["n"] or 0) if r else 0
            if n < S.HAL_VERIFY_MIN_CONNECTED or minute - data["applied_minute"] < S.HAL_VERIFY_HOURS * 60:
                return
            p0 = data["baseline_rate"]
            z = count_z(r["f"], n * p0, n * p0 * (1 - p0))
            base = baselines(self.s, wid)
            e0, c0 = base["engagement"]
            ez = fixed_z(r["e"], r["c"], e0, c0)
            drop = e0 / c0 - r["e"] / r["c"] if c0 and r["c"] else 0
            margin = engagement_margin(self.wfs[wid])
            result = {"flags": r["f"], "connected": n, "flag_rate": round(r["f"] / n, 4), "flag_z": round(z, 2),
                      "engagement": round(r["e"] / r["c"], 4) if r["c"] else None,
                      "baseline_engagement": round(e0 / c0, 4) if c0 else None, "engagement_z": round(ez, 2),
                      "engagement_margin": margin}
            data["verification"] = result
            # A failed prompt joins the bad set and stays live until a replacement is applied and verified; the
            # retry branch above files the next candidate against it.
            if z >= 2:
                data["tried"].append(data.pop("chosen"))
                self._save(iid, "retry", data)
                with self.s:
                    self.s.record(minute, wid, "fix_failed",
                                  f"{current} did not bring flags down ({r['f']}/{n}, z={z:.1f}); it stays live until a "
                                  "replacement is verified; trying the next candidate", result, ref=owner)
            elif ez <= -3 and drop > margin:
                data["tried"].append(data.pop("chosen"))
                self._save(iid, "retry", data)
                with self.s:
                    self.s.record(minute, wid, "fix_regressed",
                                  f"{current} fixed flags but engagement fell {drop:.1%} below baseline (z={ez:.1f}), "
                                  f"beyond this workflow's {margin:.1%} margin; trying the next candidate", result,
                                  ref=owner)
            elif drop > margin and n >= S.HAL_VERIFY_MAX_CONNECTED and ez <= -1.96:
                # at the full verification sample, a drop beyond the margin that is significant fails the fix
                data["tried"].append(data.pop("chosen"))
                self._save(iid, "retry", data)
                with self.s:
                    self.s.record(minute, wid, "fix_regressed",
                                  f"{current} fixed flags but after {n} calls engagement is still {drop:.1%} below "
                                  f"baseline (z={ez:.1f}), beyond this workflow's {margin:.1%} margin; trying the next "
                                  "candidate", result, ref=owner)
            elif drop > margin and n < S.HAL_VERIFY_MAX_CONNECTED:
                # flags are fine but engagement reads lower than the margin allows: not verified yet, whatever the
                # noise says. The fix is already live, so waiting only keeps the incident open longer.
                if not data.get("extended_logged"):
                    data["extended_logged"] = True
                    self._save(iid, "verifying", data)
                    with self.s:
                        self.s.record(minute, wid, "fix_inconclusive",
                                      f"{current}: flags fixed, engagement {drop:.1%} below baseline but inconclusive on "
                                      f"{n} calls (z={ez:.1f}); verifying up to {S.HAL_VERIFY_MAX_CONNECTED} calls",
                                      result, ref=owner)
            else:
                self._save(iid, "resolved", data, minute)
                with self.s:
                    self.s.record(minute, wid, "incident_resolved",
                                  f"{data['chosen']} verified: flags {r['f']}/{n} = {r['f'] / n:.1%} (baseline "
                                  f"{p0:.1%}), engagement {result['engagement']} vs {result['baseline_engagement']}",
                                  result, ref=owner)

    def _choose_fix(self, minute: int, wid: str, iid: str, data: dict) -> dict | None:
        """Assemble candidates, spend credits within the per-incident ceiling, pick the first that validates."""
        owner = f"inc:{iid}"
        cands = [c for c in self._candidates(minute, wid, data) if c["prompt"] not in data["tried"]]
        if not any(c["source"] == "observed in production" for c in cands) and "suggestions" not in data:
            cost = 3
            # optional spend: never below the reserve kept for incidents that have not happened yet
            if data["credits_spent"] + cost <= S.HAL_CREDIT_CEILING and self._credits() - cost >= S.CREDIT_RESERVE:
                problem = (f"hallucination flag rate {data['rate']:.1%} vs baseline {data['baseline_rate']:.1%} on "
                           f"prompt {data['prompt']} ({data['flags']}/{data['connected']} connected calls)")
                res = self._paid_call(minute, wid, iid, data, "llm_suggest", cost,
                                      lambda: self.sim.suggest_fixes(wid, problem))
                if res is not None:
                    data["suggestions"] = res.get("suggestions", [])
                    with self.s:
                        self.s.record(minute, wid, "llm_suggest", f"asked for fixes: {len(data['suggestions'])} "
                                      "suggestions", {"problem": problem, "suggestions": data["suggestions"],
                                                     "credits_remaining": res.get("credits_remaining")}, ref=owner)
                elif data["charges"] and data["charges"][-1]["status"] == "lost":
                    data["suggestions"] = []     # paid for but never seen: don't buy it again
                self._save_data(iid, data)
                cands = [c for c in self._candidates(minute, wid, data) if c["prompt"] not in data["tried"]]
        need = self.wfs[wid]["governance"]["min_validation_pass_rate"] + S.HAL_VALIDATION_MARGIN
        for c in cands:
            v = data["validated"].get(c["prompt"])
            if v is None:
                cost = -(-S.HAL_VALIDATION_N // 100)
                if not self.may_validate(data, cost):
                    break
                prompt = c["prompt"]
                v = self._paid_call(minute, wid, iid, data, "validation", cost,
                                    lambda: self.sim.validate(wid, prompt, S.HAL_VALIDATION_N))
                if v is None:
                    break   # retried next tick, still inside the ceiling
                data["validated"][c["prompt"]] = v
                with self.s:
                    self.s.record(minute, wid, "validation", f"{c['prompt']} pass_rate {v['pass_rate']:.3f} on "
                                  f"n={v['n']} ({v['validation_run_id']})", {"candidate": c, "run": v}, ref=owner)
                self._save_data(iid, data)
            if v["pass_rate"] >= need:
                return dict(c, validation=v)
        if not data.get("stuck_alerted"):
            data["stuck_alerted"] = True
            self._save_data(iid, data)
            with self.s:
                self.s.alert(f"no_fix:{iid}", minute, "page", wid,
                             f"no validated prompt fix for {data['prompt']} within {S.HAL_CREDIT_CEILING} credits; "
                             "needs a human")
        return None

    @atomic
    def _file_prompt_fix(self, minute: int, wid: str, iid: str, data: dict, choice: dict, current: str):
        owner = f"inc:{iid}"
        _, actor, set_at = self._live(wid)
        v = choice["validation"]
        vol = volume(self.s, wid, minute)
        excess = max(0.0, data["rate"] - data["baseline_rate"]) * vol["conn"]
        est = {"affected_leads": int(vol["conn"]), "gain_leads_per_day_low": round(excess * 0.5, 2),
               "dry_run": {"method": "prompt applies to every call; no per-cohort replay possible",
                           "candidate": choice, "validation": v,
                           "current_flag_rate": data["rate"], "baseline_flag_rate": data["baseline_rate"],
                           "note": "validation measures safety only; engagement is verified after apply"}}
        why = (f"{data['prompt']} flags {data['flags']}/{data['connected']} = {data['rate']:.1%} vs baseline "
               f"{data['baseline_rate']:.1%} (z={data['z']}).")
        if current != data["prompt"]:
            vr = data.get("verification") or {}
            why += (f" Our earlier fix {current} failed verification (flags {vr.get('flags')}/{vr.get('connected')}, "
                    f"engagement {vr.get('engagement')} vs {vr.get('baseline_engagement')}) and is still live.")
        why += (f" Candidate {choice['prompt']} ({choice['source']}"
                + (f": {choice.get('summary')}" if choice.get("summary") else "")
                + f") passed validation {v['pass_rate']:.3f} on n={v['n']} ({v['validation_run_id']}).")
        if actor == "supervisor":
            why += (f" The current prompt was set by a supervisor at {fmt(set_at)}; this request asks a human to "
                    "revert it rather than overwriting it silently.")
        iid2 = self.gov.file_intent(minute, wid, "prompt_fix", owner,
                                    [{"op": "set_prompt", "value": choice["prompt"]}],
                                    summary=f"switch prompt {current} -> {choice['prompt']} (hallucination fix)",
                                    rationale=why, evidence={"validation_run_id": v["validation_run_id"],
                                                             "incident_id": iid},
                                    estimate=est, urgent=True)
        data["chosen"] = choice["prompt"]
        data["intent"] = iid2
        self._save(iid, "proposed", data)

    # ------------------------------------------------------------ governance callbacks
    def on_applied(self, intent, minute: int, version: int):
        inc = self.s.one("SELECT * FROM incidents WHERE incident_id=?", (intent["owner"].split(":", 1)[1],))
        if not inc:
            return
        data = json.loads(inc["data"])
        if inc["kind"] == "hallucination":
            data["applied_minute"] = minute
            data["applied_version"] = version
            self._save(inc["incident_id"], "verifying", data)
        else:
            data.setdefault("applied", []).append({"minute": minute, "version": version})
            self._save(inc["incident_id"], inc["status"], data)

    def on_returned(self, intent, minute: int, why: str, reasons: list):
        inc = self.s.one("SELECT * FROM incidents WHERE incident_id=?", (intent["owner"].split(":", 1)[1],))
        if not inc or inc["status"] in ("resolved", "abandoned"):
            return
        data = json.loads(inc["data"])
        data.setdefault("returns", []).append({"minute": minute, "why": why, "reasons": reasons})
        if inc["kind"] == "hallucination":
            # Never abandoned while a bad prompt is live: back off, keep re-filing on fresh data, page a human.
            if len(data["returns"]) >= 2:
                data["retry_after"] = minute + 120
            if len(data["returns"]) >= 4:
                with self.s:
                    self.s.alert(f"incident_stuck:{inc['incident_id']}", minute, "page", inc["workflow_id"],
                                 f"prompt fix returned {len(data['returns'])} times ({why}); still retrying every 2h, "
                                 "needs a human")
            data.pop("chosen", None)
            self._save(inc["incident_id"], "retry", data)
        else:
            data["last_step_minute"] = None
            self._save(inc["incident_id"], inc["status"], data)

    # ------------------------------------------------------------ drift (alert only)
    def detect_drift(self, minute: int, wid: str):
        base = baselines(self.s, wid)
        since = minute // 60 - 6
        rows = self.s.q("SELECT hour, SUM(dials) d, SUM(conn) c FROM agg_hour WHERE workflow_id=? AND hour>=? "
                        "GROUP BY hour", (wid, since))
        d = sum(r["d"] for r in rows)
        if d < 600:
            return
        c = sum(r["c"] for r in rows)
        exp = sum(r["d"] * _rate(base["pickup_by_hod"].get(str(r["hour"] % 24)), _rate(base["pickup"], 0.25))
                  for r in rows)
        var = sum(r["d"] * (p := _rate(base["pickup_by_hod"].get(str(r["hour"] % 24)), 0.25)) * (1 - p) for r in rows)
        z = count_z(c, exp, var)
        key = f"drift:{wid}:pickup"
        with self.s:
            if z <= -S.CID_Z and c / exp <= 0.85:
                self.s.alert(key, minute, "warn", wid, f"workflow pickup {c / d:.1%} vs history {exp / d:.1%} over 6h "
                                                       f"(z={z:.1f}); not caller-ID specific")
            else:
                self.s.resolve_alert(key, minute)
