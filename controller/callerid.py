"""Caller-ID health (C5): detect degradation, rotate, explore safely.

Every number a workflow may dial has a state:

    untested --admit--> probation --(>= 2*CID_MIN_DIALS dials, >= 85% of expected pickup)--> healthy
    healthy/probation --moderate drop--> resting (24h, then 48h, 96h ...) --rest over--> probation
    healthy/probation --severe drop (<= 50% of expected)--> out (a human clears it with the carrier)

Policy (an operational setting under the signed policy; reported, not customer-facing): volume is spread evenly
over healthy numbers, because a number's reputation degrades with its volume. Numbers on probation get a small
fixed share so a bad one costs little before the detector catches it. At most two numbers are on probation at
once.

Detection compares each number with the other numbers dialled to the same cohorts in the same hours (caller ID
is randomised per dial, so this is immune to time-of-day and retry-mix effects), over a fast window (2h, extended
until the number has CID_MIN_DIALS dials) and a slow window (12h, for power on numbers that are bad from the
start). With no other number to compare against it falls back to the pre-D0 history by hour of day.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict

from . import settings as S
from .db import atomic
from .estimates import allocation_change, volume
from .incidents import _rate, baselines
from .stats import count_z
from .util import effective_dist, normalise, tv_distance

log = logging.getLogger("controller.cid")


class CallerIds:
    def __init__(self, store, sim, gov, incidents, workflows: dict):
        self.s, self.sim, self.gov, self.inc, self.wfs = store, sim, gov, incidents, workflows
        incidents.cid = self

    # ------------------------------------------------------------ state
    def _load(self, wid: str) -> dict:
        st = self.s.get(f"cid:{wid}")
        if st is None:
            _, cfg = self.gov.current(wid)
            st = {k: {"state": "healthy", "since": None, "count": 0}
                  for c in self.scopes(cfg) for k, v in effective_dist(cfg, c, "caller_id").items() if v > 0}
            with self.s:
                self.s.set(f"cid:{wid}", st)
        return st

    def _store(self, wid: str, st: dict):
        with self.s:
            self.s.set(f"cid:{wid}", st)

    def _rest_until(self, e: dict) -> int:
        if e["state"] == "out":
            return 10 ** 9
        return e["since"] + S.CID_REST_HOURS * 60 * 2 ** (e["count"] - 1)

    def states(self, wid: str, minute: int) -> dict:
        """Effective state per number, with rests that have ended turned into probation."""
        st = self._load(wid)
        changed = False
        for k, e in st.items():
            if e["state"] == "resting" and minute >= self._rest_until(e):
                # Rest only counts once the number is actually quiet: with next_campaign_only, campaigns created
                # before the change keep dialling the old allocation for up to the cadence length.
                r = self.s.one("SELECT SUM(dials) d FROM agg_hour WHERE workflow_id=? AND cid=? AND hour>=?",
                               (wid, k, (minute - S.CID_REST_HOURS * 60) // 60))
                if (r["d"] or 0) > S.CID_QUIET_DIALS:
                    continue
                e["state"], e["probe_from"] = "probation", minute
                changed = True
        if changed:
            self._store(wid, st)
        return st

    def resting(self, wid: str, minute: int | None = None) -> set:
        minute = self.s.get("last_minute", 0) if minute is None else minute
        return {k for k, e in self.states(wid, minute).items() if e["state"] in ("resting", "out")}

    def scopes(self, cfg: dict) -> list[str]:
        return ["default"] + [c["id"] for c in cfg.get("cohorts", [])
                              if "caller_id" in cfg.get("allocations", {}).get(c["id"], {})]

    # ------------------------------------------------------------ tick
    def tick(self, minute: int):
        for wid in self.wfs:
            if self.inc.in_dialling_window(wid, minute):
                self.detect(minute, wid)
            if minute % 60 == 0:
                self.clear_probation(minute, wid)
                self.maintain(minute, wid)

    # ------------------------------------------------------------ detection
    def _assess(self, rows, d: str, base: dict, since: int) -> dict | None:
        """Observed connects of number `d` vs. what the other numbers' pickup predicts, compared WITHIN each
        (cohort, hour) stratum and then summed (an indirectly standardised, Mantel-Haenszel-style comparison).
        Pickup varies by hour and cohort, so pooling first would let a different hourly traffic mix look like a
        bad number (Simpson's paradox). Strata where no other number dialled are left out of the comparison."""
        rows = [r for r in rows if r["hour"] >= since]
        mine = defaultdict(lambda: [0, 0])     # (cohort, hour) -> [connects, dials] for d
        others = defaultdict(lambda: [0, 0])   # (cohort, hour) -> same, pooled over every other number
        for r in rows:
            k = (r["cohort_id"], r["hour"])
            t = mine if r["cid"] == d else others
            t[k][0] += r["c"]
            t[k][1] += r["d"]
        dials_all = sum(v[1] for v in mine.values())
        if dials_all < S.CID_MIN_DIALS:
            return None
        shared = [k for k in mine if others[k][1] > 0]
        if sum(mine[k][1] for k in shared) >= S.CID_MIN_DIALS and sum(others[k][1] for k in shared) >= S.CID_MIN_DIALS:
            method = "vs other caller IDs within the same cohort and hour"
            conn = dials = 0
            exp = var = 0.0
            for k in shared:
                (c, n), (oc, od) = mine[k], others[k]
                p = oc / od
                conn += c
                dials += n
                exp += n * p
                var += n * p * (1 - p) + n * n * p * (1 - p) / od
        else:
            method = "vs pre-D0 history by hour of day (no other caller ID in use)"
            conn = sum(v[0] for v in mine.values())
            dials = dials_all
            exp = var = 0.0
            for (_, hour), (c, n) in mine.items():
                p = _rate(base["pickup_by_hod"].get(str(hour % 24)), _rate(base["pickup"], 0.25))
                exp += n * p
                var += n * p * (1 - p)
        if exp <= 0:
            return None
        return {"conn": conn, "dials": dials, "exp": exp, "z": count_z(conn, exp, var), "rel": conn / exp,
                "method": method}

    def detect(self, minute: int, wid: str):
        now_h = minute // 60
        rows = self.s.q("SELECT cid, cohort_id, hour, SUM(dials) d, SUM(conn) c FROM agg_hour WHERE workflow_id=? "
                        "AND hour>=? GROUP BY cid, cohort_id, hour", (wid, now_h - S.CID_SLOW_WINDOW_HOURS + 1))
        if not rows:
            return
        st = self.states(wid, minute)
        base = baselines(self.s, wid)
        for d in sorted({r["cid"] for r in rows}):
            if st.get(d, {}).get("state") in ("resting", "out"):
                continue
            per_hour = defaultdict(int)
            for r in rows:
                if r["cid"] == d:
                    per_hour[r["hour"]] += r["d"]
            fast_since, acc = now_h - S.CID_WINDOW_HOURS, 0
            for h in sorted(per_hour, reverse=True):
                acc += per_hour[h]
                fast_since = min(fast_since, h)
                if h <= now_h - S.CID_WINDOW_HOURS and acc >= S.CID_MIN_DIALS:
                    break
            entry = st.get(d, {})
            floor = (entry.get("probe_from") or -10 ** 9) // 60 if entry.get("state") == "probation" else -10 ** 9
            hits = []
            for name, since in (("fast", max(fast_since, floor)),
                                ("slow", max(now_h - S.CID_SLOW_WINDOW_HOURS + 1, floor))):
                a = self._assess(rows, d, base, since)
                if a and a["z"] <= -S.CID_Z and a["rel"] <= 1 - S.CID_MIN_REL_DROP:
                    hits.append((name, since, a))
            if not hits:
                continue
            name, since, a = min(hits, key=lambda h: h[2]["z"])
            recent = self._assess(rows, d, base, now_h) if per_hour.get(now_h, 0) >= 40 else None
            rel_sev = min(a["rel"], recent["rel"]) if recent else a["rel"]
            self._flag(minute, wid, d, name, now_h - since + 1, a, rel_sev)

    def _flag(self, minute, wid, d, window, hours, a, rel_sev):
        st = self._load(wid)
        e = st.get(d, {"count": 0})
        count = e.get("count", 0) + 1
        # severe drop, or a second strike (it degraded again after resting): out until a human clears it
        severe = rel_sev <= S.CID_SEVERE_REL or count >= 2
        st[d] = {"state": "out" if severe else "resting", "since": minute, "count": count}
        self._store(wid, st)
        data = {"did": d, "window": window, "window_hours": hours, "dials": a["dials"], "connected": a["conn"],
                "expected_connected": round(a["exp"], 1), "pickup": round(a["conn"] / a["dials"], 4),
                "expected_pickup": round(a["exp"] / a["dials"], 4), "z": round(a["z"], 2), "method": a["method"],
                "severity": "spam-labelled (severe)" if severe else "volume fatigue (moderate)",
                "relative_pickup_for_severity": round(rel_sev, 3)}
        msg = (f"{d} out of rotation until a human clears it with the carrier" if severe else
               f"{d} rests {S.CID_REST_HOURS * 2 ** (count - 1)}h (detection #{count}), then probation at "
               f"{S.CID_PROBE_SHARE:.0%}")
        open_ = self.inc.open_incident(wid, "caller_id")
        if open_:
            inc = open_[0]
            idata = json.loads(inc["data"])
            idata.setdefault("also", []).append(data)
            self.inc._save(inc["incident_id"], inc["status"], idata)
            iid = inc["incident_id"]
        else:
            iid = self.inc._new(minute, wid, "caller_id", d, data,
                                f"caller ID {d} degraded: pickup {data['pickup']:.1%} vs expected "
                                f"{data['expected_pickup']:.1%} on {a['dials']} dials over {hours}h (z={a['z']:.1f}, "
                                f"{a['method']}; {data['severity']})")
        with self.s:
            self.s.record(minute, wid, "quarantine", msg, data, ref=f"inc:{iid}")
            if severe:
                self.s.alert(f"did_out:{wid}:{d}", minute, "warn", wid,
                             f"{d} looks spam-labelled (pickup {rel_sev:.0%} of expected); needs a human to clear it")

    def clear_probation(self, minute: int, wid: str):
        st = self.states(wid, minute)
        for d, e in st.items():
            if e["state"] != "probation" or e.get("probe_from") is None:
                continue
            since = e["probe_from"] // 60
            r = self.s.one("SELECT SUM(dials) d, SUM(conn) c FROM agg_hour WHERE workflow_id=? AND cid=? AND hour>=?",
                           (wid, d, since))
            if (r["d"] or 0) < 2 * S.CID_MIN_DIALS:
                continue
            # Graduation needs positive evidence, not just "the strict detector did not fire": the number must not
            # be clearly worse than the others dialled in the same cohorts and hours.
            rows = self.s.q("SELECT cid, cohort_id, hour, SUM(dials) d, SUM(conn) c FROM agg_hour WHERE workflow_id=? "
                            "AND hour>=? GROUP BY cid, cohort_id, hour", (wid, since))
            a = self._assess(rows, d, baselines(self.s, wid), since)
            if a and a["rel"] < S.CID_PROBATION_MIN_REL and a["z"] <= -2:
                self._flag(minute, wid, d, "probation", minute // 60 - since + 1, a, a["rel"])
                continue
            e["state"] = "healthy"
            self._store(wid, st)
            with self.s:
                self.s.record(minute, wid, "probation_cleared",
                              f"{d} passed probation: {r['c']}/{r['d']} = {r['c'] / r['d']:.1%} pickup, "
                              f"{a['rel']:.0%} of expected vs the other numbers" if a else
                              f"{d} passed probation: {r['c']}/{r['d']} = {r['c'] / r['d']:.1%} pickup", None)

    # ------------------------------------------------------------ target allocation
    def target(self, wid: str, minute: int, admit: bool = True) -> tuple[dict | None, list]:
        """Even over healthy numbers, a fixed small share for probation, untested numbers admitted to probation
        (at most two on probation at once). Returns (target, newly admitted)."""
        st = self.states(wid, minute)
        opts = self.wfs[wid]["options"]["caller_id"]
        healthy = [o for o in opts if st.get(o, {}).get("state") == "healthy"]
        probe = [o for o in opts if st.get(o, {}).get("state") == "probation"]
        untested = [o for o in opts if o not in st]
        concentrated = len(healthy) <= 1
        if concentrated:
            # all volume on one number is the known risk: bring in up to two untested numbers at an even share
            admitted = untested[:max(0, 2 - len(probe))] if admit else []
        else:
            # otherwise explore one untested number at a time, at the probe share, and not right after a failure
            recent_fail = any(e.get("since") and minute - e["since"] < 24 * 60 and e["state"] in ("resting", "out")
                              for e in st.values())
            admitted = untested[:1] if admit and not probe and not recent_fail else []
        probe += admitted
        if not healthy and not probe:
            return None, []
        # a number that failed before stays at the probe share until it clears probation, whatever else happens
        capped = [o for o in probe if st.get(o, {}).get("count", 0) >= 1]
        fresh = [o for o in probe if o not in capped]
        tgt = {o: S.CID_PROBE_SHARE for o in capped}
        if concentrated:
            pool = healthy + fresh or capped
        else:
            tgt.update({o: S.CID_PROBE_SHARE for o in fresh})
            pool = healthy
        rest = 1 - sum(v for k, v in tgt.items() if k not in pool)
        for o in pool:
            tgt[o] = rest / len(pool)
        return normalise(tgt), admitted

    def _step(self, wid: str, cur: dict, tgt: dict) -> tuple[dict, bool]:
        """Within auto-apply policy, move at most max_shift toward the target per step."""
        pol = self.wfs[wid]["governance"]["auto_apply"]
        if not self.auto_ok(wid):
            return tgt, False
        tv = tv_distance(cur, tgt)
        lam = min(1.0, pol["max_shift"] / tv) if tv > 0 else 1.0
        return normalise({k: (1 - lam) * cur.get(k, 0) + lam * tgt.get(k, 0) for k in set(cur) | set(tgt)}), lam < 1

    def auto_ok(self, wid: str) -> bool:
        pol = self.wfs[wid]["governance"]["auto_apply"]
        return bool(pol.get("enabled") and "caller_id" in pol.get("allowed_dims", []))

    # ------------------------------------------------------------ standing policy
    @atomic
    def maintain(self, minute: int, wid: str):
        if self.inc.open_incident(wid, "caller_id"):
            return
        owner = f"pol:cid:{wid}"
        if self.gov.open_intents(owner):
            return
        last = self.gov.last_returned(owner)
        if last and minute - last["updated_minute"] < 120:
            return
        if not self.auto_ok(wid) and not self.gov.can_request(wid, minute, urgent=False)[0]:
            return
        _, cfg = self.gov.current(wid)
        tgt, admitted = self.target(wid, minute)
        if tgt is None:
            return
        ops = []
        for c in self.scopes(cfg):
            cur = effective_dist(cfg, c, "caller_id")
            if tv_distance(cur, tgt) < S.CID_SPREAD_MIN_TV:
                continue
            new, _ = self._step(wid, cur, tgt)
            ops.append({"op": "set_alloc", "cohort": c, "dim": "caller_id", "dist": new})
        if not ops:
            return
        st = self._load(wid)
        for o in admitted:
            st[o] = {"state": "probation", "since": minute, "count": 0, "probe_from": minute}
        self._store(wid, st)
        est = allocation_change(self.s, wid, minute, "caller_id", ["default"], effective_dist(cfg, "default", "caller_id"),
                                ops[0]["dist"], "pickup_rate", exploration=True)
        probe = sorted(o for o, e in st.items() if e["state"] == "probation")
        self.gov.file_intent(
            minute, wid, "caller_id_spread", owner, ops,
            summary="caller-ID rotation: even spread over healthy numbers"
                    + (f", probation for {', '.join(probe)} at {S.CID_PROBE_SHARE:.0%}" if probe else ""),
            rationale=("A number's reputation degrades with its volume, so volume stays even across healthy numbers. "
                       f"Numbers on probation ({', '.join(probe) or 'none'}) get {S.CID_PROBE_SHARE:.0%} each until "
                       f"they have {2 * S.CID_MIN_DIALS} dials with no detection. Target {tgt}; out of rotation: "
                       f"{sorted(self.resting(wid, minute)) or 'none'}. Operational setting (caller-ID rotation) under "
                       "the signed policy; reported in the weekly summary."),
            evidence={"policy": "caller_id_rotation", "target": tgt, "states": st}, estimate=est)

    # ------------------------------------------------------------ incident mitigation
    @atomic
    def progress(self, minute: int, inc):
        wid, iid = inc["workflow_id"], inc["incident_id"]
        data = json.loads(inc["data"])
        owner = f"inc:{iid}"
        if self.gov.open_intents(owner):
            return
        version, cfg = self.gov.current(wid)
        q = self.resting(wid, minute)
        exposed = [c for c in self.scopes(cfg) if any(effective_dist(cfg, c, "caller_id").get(d, 0) > 0 for d in q)]
        if not exposed:
            if inc["status"] != "verifying":
                data["mitigated_minute"] = minute
                self.inc._save(iid, "verifying", data)
                with self.s:
                    self.s.record(minute, wid, "incident_mitigated", f"no traffic left on {', '.join(sorted(q))}",
                                  {"version": version}, ref=owner)
                return
            if minute - data["mitigated_minute"] >= 120:
                r = self.s.one("SELECT SUM(dials) d, SUM(conn) c FROM agg_hour WHERE workflow_id=? AND hour>=?",
                               (wid, data["mitigated_minute"] // 60))
                after = (r["c"] or 0) / r["d"] if r["d"] else None
                data["pickup_after"] = round(after, 4) if after is not None else None
                self.inc._save(iid, "resolved", data, minute)
                with self.s:
                    self.s.record(minute, wid, "incident_resolved",
                                  f"caller ID incident resolved: {data['did']} had {data['pickup']:.1%} pickup; "
                                  f"{after:.1%} workflow-wide since mitigation" if after is not None else
                                  "caller ID incident resolved", data, ref=owner)
            return
        if data.get("last_step_minute") and minute - data["last_step_minute"] < S.CID_STEP_COOLDOWN:
            return
        tgt, _ = self.target(wid, minute, admit=False)
        if tgt is None or not any(v > 0 for k, v in tgt.items() if k not in q):
            tgt, admitted = self.target(wid, minute, admit=True)
            if tgt is None:
                with self.s:
                    self.s.alert(f"cid_nowhere:{wid}", minute, "page", wid,
                                 "every caller ID is out of rotation; dialling continues on a degraded number")
                return
            st = self._load(wid)
            for o in admitted:
                st[o] = {"state": "probation", "since": minute, "count": 0, "probe_from": minute}
            self._store(wid, st)
        pol = self.wfs[wid]["governance"]["auto_apply"]
        ops, notes = [], []
        for c in exposed:
            cur = effective_dist(cfg, c, "caller_id")
            new, partial = self._step(wid, cur, tgt)
            if partial:
                notes.append(f"{c}: step of {pol['max_shift']:.0%} (policy max_shift); more steps follow, each at "
                             f"least {S.CID_STEP_COOLDOWN} min apart and re-checked")
            ops.append({"op": "set_alloc", "cohort": c, "dim": "caller_id", "dist": new})
        cur0 = effective_dist(cfg, exposed[0], "caller_id")
        est = allocation_change(self.s, wid, minute, "caller_id", exposed, cur0, ops[0]["dist"], "pickup_rate")
        lost = sum((max(0.0, data["expected_pickup"] - data["pickup"])) * volume(self.s, wid, minute)["dials"] *
                   cur0.get(d, 0) for d in q)
        eng = _rate(baselines(self.s, wid)["engagement"], 0.5)
        est["gain_leads_per_day_low"] = round(max(0.0, lost * eng * 0.5), 2)
        iid2 = self.gov.file_intent(
            minute, wid, "caller_id_mitigation", owner, ops,
            summary=f"move traffic off degraded caller ID {', '.join(sorted(q))}",
            rationale=(f"{data['did']} pickup {data['pickup']:.1%} vs expected {data['expected_pickup']:.1%} over "
                       f"{data['dials']} dials in {data['window_hours']}h (z={data['z']}); {data['method']}; "
                       f"{data['severity']}. Caller-ID rotation is an operational setting. " + " ".join(notes)),
            evidence={"incident_id": iid, "detection": data}, estimate=est, urgent=True)
        data["last_step_minute"] = minute
        data.setdefault("steps", []).append({"minute": minute, "intent": iid2, "ops": ops})
        self.inc._save(iid, "mitigating", data)
