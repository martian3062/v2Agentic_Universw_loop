"""Governance client (C9): the only code that changes a workflow's config.

Owners (experiments, incidents) never call the API to change config. They file *intents*: replayable ops
plus evidence. Governance turns intents into one proposal per workflow (batching), picks the path the
workflow's policy and our own rules allow, times the request against approver hours, quota and latency,
and reconciles every in-flight request after a crash.

Proposal lifecycle (ours, superset of the simulator's):

    prepared --POST ok--> pending --> approved --apply ok--> applied
        |                    |            |--apply 409 stale--> stale      (intents return to owners)
        |                    |            `--apply 410-------> lapsed      (pages: we missed the window)
        |                    |--> rejected | expired                       (intents return, cooldown)
        `--crash, transport error or 5xx--> stays prepared: reconciled from GET /approvals by proposal_id
                                             (adopted if it landed, re-sent with the same id if not)

    auto: prepared --PUT ok--> applied | --403--> refused (intents switch to the approval path)
          prepared --transport/5xx--> stays prepared: reconciled from config history by proposal_id

A proposal_id is written to the store before the request is sent, so a restart can always find out
whether the request landed: the simulator lists approvals (and config history) with our proposal_id.
Only a definite 4xx marks a request failed; an ambiguous error never does.

Recording an outcome (applied, or intents returned) and the owners' reaction to it (an experiment going live,
an incident starting verification) commit in ONE transaction. A crash or a raising callback leaves neither, and
the next reconcile replays both from the simulator's own records.
"""
from __future__ import annotations

import json
import logging
import math
import secrets

from . import settings as S
from .api import ApiError
from .db import Store
from .util import apply_ops, changed_dims, config_diff, fmt, max_shift, minute_of

log = logging.getLogger("controller.gov")

IN_FLIGHT = ("prepared", "pending", "approved")


class Governance:
    def __init__(self, store: Store, sim, workflows: dict):
        self.s, self.sim, self.wfs = store, sim, workflows
        self._cfg: dict[str, tuple[int, dict]] = {}
        self.callbacks = {}            # owner prefix -> object with on_applied / on_returned
        if self.s.get("nonce") is None:
            with self.s:
                self.s.set("nonce", secrets.token_hex(3))
        self.nonce = self.s.get("nonce")

    # ------------------------------------------------------------ config cache
    def current(self, wid: str, refresh: bool = False) -> tuple[int, dict]:
        if refresh or wid not in self._cfg:
            c = self.sim.config(wid)
            self._cfg[wid] = (c["version"], c["config"])
            with self.s:
                self.s.x("INSERT INTO config_versions(workflow_id,version,actor,applied_minute,config) "
                         "VALUES(?,?,?,?,?) ON CONFLICT DO UPDATE SET config=excluded.config",
                         (wid, c["version"], c["actor"],
                          minute_of(c["applied_at"]) if c.get("applied_at") else None, json.dumps(c["config"])))
        return self._cfg[wid]

    def invalidate(self, wid: str):
        self._cfg.pop(wid, None)

    def policy(self, wid: str) -> dict:
        return self.wfs[wid]["governance"]

    def pref(self, wid: str) -> S.TenantPreference:
        return S.TENANT_PREFERENCES.get(self.wfs[wid]["tenant_id"], S.TenantPreference())

    # ------------------------------------------------------------ intents (owner API)
    def file_intent(self, minute: int, wid: str, kind: str, owner: str, ops: list, *, summary: str,
                    rationale: str, evidence: dict | None = None, estimate: dict | None = None,
                    urgent: bool = False, derisk: bool = False) -> str | None:
        """Register a desired change. Returns intent_id, or None if it would change nothing."""
        version, cfg = self.current(wid)
        new = apply_ops(cfg, ops)
        dims = changed_dims(cfg, new)
        if not dims:
            return None
        path = self.choose_path(wid, cfg, new, dims, derisk)
        n = self.s.next_seq("intent")
        iid = f"int-{n:05d}"
        with self.s:
            self.s.x("INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (iid, wid, kind, owner, json.dumps(ops), summary, rationale, json.dumps(evidence or {}),
                      json.dumps(estimate or {}), int(urgent), path, "ready", None, 0, minute, minute,
                      "derisk" if derisk else None))
            self.s.record(minute, wid, "intent_filed", f"{kind}: {summary} (path={path}, urgent={urgent})",
                          {"intent_id": iid, "ops": ops, "dims": dims, "evidence": evidence, "estimate": estimate},
                          ref=owner)
        return iid

    def choose_path(self, wid: str, cfg: dict, new: dict, dims: list, derisk: bool) -> str:
        aa = self.policy(wid)["auto_apply"]
        if not aa.get("enabled"):
            return "approval"
        if not set(dims) <= set(aa.get("allowed_dims", [])):
            return "approval"
        if max_shift(cfg, new) > aa.get("max_shift", 0) + 1e-9:
            return "approval"
        customer_facing = bool(set(dims) & S.CUSTOMER_FACING_DIMS)
        # A tenant's own rule comes first: Northwind requires a named human approval for ANY customer-facing
        # change, including a rollback, so the de-risk exception below never applies to it.
        if customer_facing and self.pref(wid).human_approval_for_customer_facing:
            return "approval"
        # Our rule, stricter than any policy: a customer-facing change toward something new needs a human.
        # Moving traffic back toward what a human already approved (de-risking) may use auto-apply.
        if customer_facing and not derisk:
            return "approval"
        return "auto"

    def open_intents(self, owner: str) -> list:
        return self.s.q("SELECT * FROM intents WHERE owner=? AND status IN ('ready','in_proposal')", (owner,))

    def last_returned(self, owner: str):
        return self.s.one("SELECT * FROM intents WHERE owner=? AND status='returned' ORDER BY updated_minute DESC "
                          "LIMIT 1", (owner,))

    def cancel_owner_intents(self, minute: int, owner: str, reason: str):
        with self.s:
            for r in self.s.q("SELECT intent_id FROM intents WHERE owner=? AND status='ready'", (owner,)):
                self.s.x("UPDATE intents SET status='cancelled', note=?, updated_minute=? WHERE intent_id=?",
                         (reason, minute, r["intent_id"]))

    # ------------------------------------------------------------ tick
    def submit(self, minute: int, wid: str):
        self._submit_auto(minute, wid)
        self._submit_approval(minute, wid)

    def in_flight(self, wid: str) -> list:
        return self.s.q(f"SELECT * FROM proposals WHERE workflow_id=? AND status IN {IN_FLIGHT}", (wid,))

    # ------------------------------------------------------------ reconciliation
    def reconcile(self, minute: int):
        rows = self.s.q(f"SELECT * FROM proposals WHERE status IN {IN_FLIGHT} ORDER BY created_minute")
        listed: dict[str, dict] = {}
        for p in rows:
            # one proposal's failure (a simulator error, or an owner callback raising) must not block the others;
            # its step rolled back, so the next tick replays it
            try:
                if p["status"] == "prepared":
                    self._resolve_prepared(minute, p, listed)
                else:
                    self._on_status(minute, p, self.sim.approval(p["approval_id"]))
            except ApiError as e:
                log.warning("reconcile %s: simulator error %s; retrying next tick", p["proposal_id"], e)
            except Exception as e:  # noqa: BLE001 - isolate per proposal, page, retry next tick
                log.exception("reconcile %s failed", p["proposal_id"])
                with self.s:
                    self.s.alert(f"reconcile_failed:{p['proposal_id']}", minute, "page", p["workflow_id"],
                                 f"reconciling {p['proposal_id']} failed ({type(e).__name__}: {e}); retried every tick")
                continue
            with self.s:
                self.s.resolve_alert(f"reconcile_failed:{p['proposal_id']}", minute)

    def _resolve_prepared(self, minute: int, p, listed: dict):
        """A request we wrote down but may or may not have sent. Ask the simulator."""
        wid = p["workflow_id"]
        if p["path"] == "approval":
            if wid not in listed:
                listed[wid] = {a["proposal_id"]: a for a in self.sim.approvals(workflow_id=wid)}
            a = listed[wid].get(p["proposal_id"])
            if a:
                with self.s:
                    self.s.x("UPDATE proposals SET status='pending', approval_id=?, submitted_minute=? "
                             "WHERE proposal_id=?", (a["approval_id"], minute_of(a["created_at"]), p["proposal_id"]))
                    self.s.record(minute, wid, "reconciled", f"found {p['proposal_id']} as {a['approval_id']}",
                                  a, proposal_id=p["proposal_id"], approval_id=a["approval_id"])
                self._on_status(minute, self.s.one("SELECT * FROM proposals WHERE proposal_id=?",
                                                   (p["proposal_id"],)), a)
            else:
                self._send_approval(minute, p)
        else:
            hist = self.sim.config_history(wid)
            hit = [h for h in hist if h.get("proposal_id") == p["proposal_id"]]
            if hit:
                self._mark_applied(minute, p, hit[0]["version"], via="reconciled auto-apply")
            else:
                self._send_auto(minute, p)

    def _on_status(self, minute: int, p, a: dict):
        st = a["status"]
        if st == p["status"] and st != "approved":
            return
        wid = p["workflow_id"]
        if st == "approved":
            if p["status"] != "approved":
                with self.s:
                    self.s.x("UPDATE proposals SET status='approved', decided_minute=?, apply_by=? WHERE proposal_id=?",
                             (minute_of(a["decided_at"]), minute_of(a["apply_by"]) if a.get("apply_by") else None,
                              p["proposal_id"]))
                    self.s.record(minute_of(a["decided_at"]), wid, "approved",
                                  f"{p['approval_id']} approved by {wid} approver", a,
                                  proposal_id=p["proposal_id"], approval_id=p["approval_id"])
            self._apply(minute, p)
        elif st == "applied":
            cur_v, _ = self.current(wid, refresh=True)
            hist = self.sim.config_history(wid)
            hit = [h for h in hist if h.get("approval_id") == p["approval_id"]]
            self._mark_applied(minute, p, hit[0]["version"] if hit else cur_v, via="reconciled apply")
        elif st in ("rejected", "expired", "lapsed"):
            reasons = a.get("reasons") or []
            with self.s:   # status change + intents returned + owners' reaction: one transaction
                self.s.x("UPDATE proposals SET status=?, decided_minute=?, reasons=? WHERE proposal_id=?",
                         (st, minute_of(a["decided_at"]) if a.get("decided_at") else minute, json.dumps(reasons),
                          p["proposal_id"]))
                self.s.record(minute, wid, st, f"{p['approval_id']} {st}: {'; '.join(reasons) or 'no reason given'}",
                              a, proposal_id=p["proposal_id"], approval_id=p["approval_id"])
                if st == "lapsed":
                    self.s.alert(f"lapsed:{p['proposal_id']}", minute, "page", wid,
                                 f"approved change {p['approval_id']} lapsed before we applied it")
                self._return_intents(minute, p, st, reasons)

    def _apply(self, minute: int, p):
        wid = p["workflow_id"]
        try:
            r = self.sim.apply(wid, p["approval_id"])
        except ApiError as e:
            if e.error == "stale_base_version":
                self.invalidate(wid)
                with self.s:
                    self.s.x("UPDATE proposals SET status='stale', reasons=? WHERE proposal_id=?",
                             (json.dumps([str(e.detail)]), p["proposal_id"]))
                    self.s.record(minute, wid, "stale", f"{p['approval_id']} approved but config moved on "
                                  f"(now v{(e.detail or {}).get('current_version')}); will re-propose on the new base",
                                  e.detail, proposal_id=p["proposal_id"], approval_id=p["approval_id"])
                    self._return_intents(minute, p, "stale", ["config changed after proposal"])
            elif e.error == "not_approved" and isinstance(e.detail, dict) and e.detail.get("status") == "applied":
                self._on_status(minute, p, {"status": "applied"})
            elif e.error == "approval_lapsed":
                self._on_status(minute, p, {"status": "lapsed", "reasons": ["apply window passed"]})
            else:
                log.error("apply failed %s: %s", p["proposal_id"], e)
            return
        self.invalidate(wid)
        self._mark_applied(minute, p, r["version"], via="approval")

    def _mark_applied(self, minute: int, p, version: int, via: str):
        """Record the apply AND run the owners' transitions in one transaction. If the process dies or a callback
        raises, nothing is recorded; the simulator still says applied (or lists the proposal_id in config
        history), so the next reconcile replays this whole step. Owner callbacks are written to be replayable."""
        wid = p["workflow_id"]
        with self.s:
            self.s.x("UPDATE proposals SET status='applied', applied_minute=?, applied_version=? WHERE proposal_id=?",
                     (minute, version, p["proposal_id"]))
            self.s.x("INSERT INTO config_versions(workflow_id,version,actor,changed_dims,proposal_id,approval_id,"
                     "applied_minute,config) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                     "proposal_id=excluded.proposal_id, approval_id=excluded.approval_id, config=excluded.config",
                     (wid, version, "controller_approved" if p["path"] == "approval" else "controller_auto",
                      json.dumps(json.loads(p["body"]).get("_dims", [])), p["proposal_id"], p["approval_id"], minute,
                      p["proposed_config"]))
            self.s.record(minute, wid, "applied", f"v{version} live ({via}): {json.loads(p['body'])['change_summary']}",
                          {"diff": json.loads(p["body"]).get("diff")}, proposal_id=p["proposal_id"],
                          approval_id=p["approval_id"], version=version)
            intents = json.loads(p["intents"])
            for iid in intents:
                self.s.x("UPDATE intents SET status='applied', updated_minute=? WHERE intent_id=?", (minute, iid))
            self.invalidate(wid)
            for iid in intents:
                it = self.s.one("SELECT * FROM intents WHERE intent_id=?", (iid,))
                self._callback(it, "on_applied", minute, version)

    def _return_intents(self, minute: int, p, why: str, reasons: list):
        """Same atomicity as _mark_applied: the intents' return and the owners' reaction commit together. Callers
        wrap this together with the proposal's own status change in one transaction."""
        with self.s:
            for iid in json.loads(p["intents"]):
                self.s.x("UPDATE intents SET status='returned', note=?, attempts=attempts+1, updated_minute=? "
                         "WHERE intent_id=?", (f"{why}: {'; '.join(reasons)}", minute, iid))
            for iid in json.loads(p["intents"]):
                it = self.s.one("SELECT * FROM intents WHERE intent_id=?", (iid,))
                self._callback(it, "on_returned", minute, why, reasons)

    def _callback(self, intent, name: str, *args):
        prefix = intent["owner"].split(":", 1)[0]
        cb = self.callbacks.get(prefix)
        if cb and hasattr(cb, name):
            getattr(cb, name)(intent, *args)

    # ------------------------------------------------------------ timing and quota
    def quota(self, wid: str) -> int:
        q = self.policy(wid)["max_requests_per_24h"]
        pref = self.pref(wid).max_requests_per_day
        return min(q, pref) if pref else q

    def used_24h(self, wid: str, minute: int) -> int:
        r = self.s.one("SELECT COUNT(*) AS n FROM proposals WHERE workflow_id=? AND path='approval' "
                       "AND submitted_minute IS NOT NULL AND submitted_minute > ?", (wid, minute - 1440))
        return r["n"]

    def can_request(self, wid: str, minute: int, urgent: bool) -> tuple[bool, str]:
        pol = self.policy(wid)
        start, end = pol["approver_hours"]
        start = max(start, self.pref(wid).earliest_request_hour or start)
        mod = minute % 1440
        if not (start * 60 <= mod < end * 60):
            return False, f"outside approver hours {start}:00-{end}:00"
        used, q = self.used_24h(wid, minute), self.quota(wid)
        cap = q if urgent else max(1, q - S.URGENT_QUOTA_RESERVE)
        if used >= cap:
            return False, f"quota {used}/{cap} used in the last 24h"
        if not urgent:
            lat_max = pol["approver_latency_minutes"][1]
            remaining = end * 60 - mod
            if remaining < min(lat_max, (end - start) * 60 - S.TICK_ACTIVE):
                return False, f"only {remaining} min of approver time left today (latency up to {lat_max})"
        return True, "ok"

    # ------------------------------------------------------------ submission
    def _ready(self, wid: str, path: str):
        return self.s.q("SELECT * FROM intents WHERE workflow_id=? AND status='ready' AND path=? "
                        "ORDER BY urgent DESC, created_minute", (wid, path))

    def _compose(self, wid: str, intents: list) -> tuple[list, list]:
        """Batch intents that touch different things; later conflicting ones wait for the next proposal."""
        chosen, keys, ops_all = [], set(), []
        for it in intents:
            ops = json.loads(it["ops"])
            k = {(o["op"], o["cohort"]["id"] if isinstance(o.get("cohort"), dict) else o.get("cohort"), o.get("dim"))
                 for o in ops}
            if k & keys:
                continue
            keys |= k
            chosen.append(it)
            ops_all.extend(ops)
        return chosen, ops_all

    def _body(self, minute: int, wid: str, pid: str, version: int, cfg: dict, new: dict, intents: list) -> dict:
        ests = [json.loads(i["estimate"] or "{}") for i in intents]
        evid = {}
        for i in intents:
            evid.update(json.loads(i["evidence"] or "{}"))
        gain = sum(max(0.0, e.get("gain_leads_per_day_low", 0.0)) for e in ests)
        impact = round(1 - math.exp(-gain / S.IMPACT_SCALE_LEADS_PER_DAY), 3)
        dims = changed_dims(cfg, new)
        body = {
            "proposal_id": pid, "workflow_id": wid, "base_version": version, "proposed_config": new,
            "change_summary": " + ".join(i["summary"] for i in intents)[:500],
            "diff": config_diff(cfg, new),
            "affected_leads": int(sum(e.get("affected_leads", 0) for e in ests)),
            "dry_run": {i["intent_id"]: e.get("dry_run", {"note": "no historical replay possible"})
                        for i, e in zip(intents, ests)},
            "impact_score": min(1.0, max(0.0, impact)),
            "rationale": "\n".join(f"[{i['kind']}] {i['rationale']}" for i in intents)[:4000],
            "evidence": {k: v for k, v in evid.items() if k == "validation_run_id"} or None,
            "expires_in_hours": self.policy(wid)["proposal_ttl_hours"],
        }
        if body["evidence"] is None:
            body.pop("evidence")
        body["_dims"] = dims
        body["_evidence_full"] = evid
        return body

    def _prepare(self, minute: int, wid: str, path: str, intents: list, urgent: bool):
        version, cfg = self.current(wid, refresh=True)
        chosen, ops = self._compose(wid, intents)
        new = apply_ops(cfg, ops)
        if not changed_dims(cfg, new):
            with self.s:
                for it in chosen:
                    self.s.x("UPDATE intents SET status='cancelled', note='no-op on current base', updated_minute=? "
                             "WHERE intent_id=?", (minute, it["intent_id"]))
            return None
        if path == "auto" and self.choose_path(wid, cfg, new, changed_dims(cfg, new),
                                               all(i["note"] == "derisk" for i in chosen)) != "auto":
            with self.s:   # the base moved or the batch got too big: switch to approval
                for it in chosen:
                    self.s.x("UPDATE intents SET path='approval', updated_minute=? WHERE intent_id=?",
                             (minute, it["intent_id"]))
            return None
        n = self.s.next_seq("proposal")
        pid = f"ctl-{self.nonce}-{wid}-{n:04d}"
        body = self._body(minute, wid, pid, version, cfg, new, chosen)
        with self.s:
            self.s.x("INSERT INTO proposals(proposal_id,workflow_id,path,status,base_version,proposed_config,body,"
                     "intents,urgent,created_minute) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (pid, wid, path, "prepared", version, json.dumps(new), json.dumps(body),
                      json.dumps([i["intent_id"] for i in chosen]), int(urgent), minute))
            for it in chosen:
                self.s.x("UPDATE intents SET status='in_proposal', proposal_id=?, updated_minute=? WHERE intent_id=?",
                         (pid, minute, it["intent_id"]))
        return self.s.one("SELECT * FROM proposals WHERE proposal_id=?", (pid,))

    def _wire_body(self, p) -> dict:
        return {k: v for k, v in json.loads(p["body"]).items() if not k.startswith("_")}

    def _submit_approval(self, minute: int, wid: str):
        ready = self._ready(wid, "approval")
        if not ready:
            return
        urgent = any(r["urgent"] for r in ready)
        flight = self.in_flight(wid)
        if flight and (not urgent or any(f["urgent"] for f in flight)):
            return
        ok, why = self.can_request(wid, minute, urgent)
        if not ok:
            return
        if flight:  # an urgent request goes out alongside a pending routine one; it carries only urgent work
            ready = [r for r in ready if r["urgent"]]
        p = self._prepare(minute, wid, "approval", ready, urgent)
        if p:
            self._send_approval(minute, p)

    def _send_approval(self, minute: int, p):
        wid = p["workflow_id"]
        body = self._wire_body(p)
        try:
            a = self.sim.propose(body)
        except ApiError as e:
            if e.error == "duplicate_proposal_id":
                hit = {x["proposal_id"]: x for x in self.sim.approvals(workflow_id=wid)}.get(p["proposal_id"])
                if hit:
                    a = hit
                else:
                    return
            elif self._uncertain(e):
                self._note_uncertain(minute, p, e)
                return
            else:
                self.invalidate(wid)
                with self.s:   # a definite refusal (4xx): fail and hand back, atomically
                    self.s.x("UPDATE proposals SET status='failed', reasons=? WHERE proposal_id=?",
                             (json.dumps([f"{e.error}: {e.detail}"]), p["proposal_id"]))
                    self.s.record(minute, wid, "request_failed", f"{p['proposal_id']} refused: {e.error}",
                                  {"detail": e.detail}, proposal_id=p["proposal_id"])
                    self._return_intents(minute, p, "failed", [str(e.error)])
                return
        with self.s:
            self.s.x("UPDATE proposals SET status='pending', approval_id=?, submitted_minute=?, expires_at=? "
                     "WHERE proposal_id=?",
                     (a["approval_id"], minute, minute_of(a["expires_at"]), p["proposal_id"]))
            full = json.loads(p["body"])
            self.s.record(minute, wid, "requested", f"{a['approval_id']}: {body['change_summary']}",
                          {"diff": body["diff"], "affected_leads": body["affected_leads"],
                           "impact_score": body["impact_score"], "dry_run": body["dry_run"],
                           "evidence": full.get("_evidence_full"), "expires_at": a["expires_at"]},
                          proposal_id=p["proposal_id"], approval_id=a["approval_id"])

    def _submit_auto(self, minute: int, wid: str):
        ready = self._ready(wid, "auto")
        if not ready:
            return
        urgent = any(r["urgent"] for r in ready)
        if self.in_flight(wid) and not urgent:
            return  # don't make a pending human request stale for a routine change
        if urgent:
            ready = [r for r in ready if r["urgent"]]
        p = self._prepare(minute, wid, "auto", ready, urgent)
        if p:
            self._send_auto(minute, p)

    def _send_auto(self, minute: int, p):
        wid = p["workflow_id"]
        b = json.loads(p["body"])
        body = {"proposal_id": p["proposal_id"], "base_version": p["base_version"],
                "proposed_config": json.loads(p["proposed_config"]), "rationale": b["rationale"],
                "evidence": b.get("_evidence_full") or {}}
        try:
            r = self.sim.auto_apply(wid, body)
        except ApiError as e:
            if e.error == "duplicate_proposal_id":
                hit = [h for h in self.sim.config_history(wid) if h.get("proposal_id") == p["proposal_id"]]
                if hit:
                    self._mark_applied(minute, p, hit[0]["version"], via="auto-apply (reconciled)")
                return
            if self._uncertain(e):
                self._note_uncertain(minute, p, e)
                return
            status = "refused" if e.status == 403 else ("stale" if e.error == "stale_base_version" else "failed")
            self.invalidate(wid)
            with self.s:
                self.s.x("UPDATE proposals SET status=?, reasons=? WHERE proposal_id=?",
                         (status, json.dumps([f"{e.error}: {e.detail}"]), p["proposal_id"]))
                self.s.record(minute, wid, f"auto_{status}", f"auto-apply {p['proposal_id']} {status}: {e.error}",
                              {"detail": e.detail}, proposal_id=p["proposal_id"])
                if status == "refused":
                    for iid in json.loads(p["intents"]):
                        self.s.x("UPDATE intents SET status='ready', path='approval', proposal_id=NULL, "
                                 "updated_minute=? WHERE intent_id=?", (minute, iid))
                else:
                    self._return_intents(minute, p, status, [str(e.error)])
            return
        self._mark_applied(minute, p, r["version"], via="auto-apply within policy")

    # ------------------------------------------------------------ uncertain outcomes
    @staticmethod
    def _uncertain(e: ApiError) -> bool:
        """A transport error or a 5xx means the request may or may not have landed."""
        return e.status == 0 or e.status >= 500

    def _note_uncertain(self, minute: int, p, e: ApiError):
        """Leave the proposal 'prepared'. Every reconcile looks it up by proposal_id (GET /approvals, or config
        history for auto-apply), adopts it if it landed and re-sends it with the same id if it did not. Nothing is
        declared failed on an ambiguous error."""
        with self.s:
            self.s.record(minute, p["workflow_id"], "request_uncertain",
                          f"{p['proposal_id']}: {e.error} ({e.status}); outcome unknown, will reconcile by proposal_id",
                          {"detail": str(e.detail)[:300]}, proposal_id=p["proposal_id"])

    # ------------------------------------------------------------ events
    def on_event(self, e: dict):
        """Listener for config_changed / approval_decided events (runs inside the ingest transaction)."""
        if e["type"] == "config_changed":
            wid = e["workflow_id"]
            m = minute_of(e["ts"])
            self.s.x("INSERT INTO config_versions(workflow_id,version,actor,changed_dims,proposal_id,approval_id,"
                     "applied_minute) VALUES(?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET actor=excluded.actor, "
                     "changed_dims=excluded.changed_dims, applied_minute=excluded.applied_minute",
                     (wid, e["version"], e["actor"], json.dumps(e.get("changed_dims")), e.get("proposal_id"),
                      e.get("approval_id"), m))
            self.invalidate(wid)
            if e["actor"] == "supervisor":
                self.s.record(m, wid, "supervisor_edit",
                              f"supervisor changed {', '.join(e.get('changed_dims') or [])} -> v{e['version']}; "
                              "we will not overwrite it, affected work is reassessed", e, version=e["version"])
                pend = self.s.get(f"supervisor_edits:{wid}", [])
                pend.append({"version": e["version"], "minute": m, "dims": e.get("changed_dims") or []})
                self.s.set(f"supervisor_edits:{wid}", pend)
        elif e["type"] == "approval_decided":
            self.s.record(minute_of(e["ts"]), e["workflow_id"], "approval_event",
                          f"{e['approval_id']} {e['status']}" + (f": {'; '.join(e['reasons'])}" if e.get("reasons")
                                                                  else ""), e, approval_id=e["approval_id"])

    def describe(self, minute: int) -> str:
        return ", ".join(f"{p['workflow_id']}:{p['status']}" for p in
                         self.s.q(f"SELECT workflow_id,status FROM proposals WHERE status IN {IN_FLIGHT}")) or "none"


def fmt_minute(m):
    return fmt(m)
