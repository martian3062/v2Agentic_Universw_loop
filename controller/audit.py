"""Read-only views over the controller's own store (C11, C12, C14). Nothing here touches the simulator.

    python -m controller why    --db state/controller.db --workflow nw_renewal --at "D3 14:00"
    python -m controller view   --db state/controller.db [--workflow ks_cancel]
    python -m controller health --db state/controller.db
    python -m controller log    --db state/controller.db [--workflow X] [--kind applied] [--limit 50]
"""
from __future__ import annotations

import json

from .db import Store
from .util import fmt


def _j(s):
    return json.loads(s) if s else None


def _dist(d: dict) -> str:
    return ", ".join(f"{k} {v:.0%}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))


def tenant_workflows(s: Store, tenant: str | None) -> set | None:
    """Workflows a tenant may see; None means unscoped (the operator). Unknown tenant: an empty set."""
    if not tenant:
        return None
    return {w for w, spec in (s.get("workflows") or {}).items() if spec.get("tenant_id") == tenant}


def why(s: Store, wid: str, at: int, tenant: str | None = None) -> str:
    scope = tenant_workflows(s, tenant)
    if scope is not None and wid not in scope:
        return f"{wid} is not a workflow of {tenant}; nothing to show"
    out = []
    rows = s.q("SELECT * FROM config_versions WHERE workflow_id=? ORDER BY version", (wid,))
    live = None
    for r in rows:
        if r["applied_minute"] is None or r["applied_minute"] <= at:
            live = r
    if live is None:
        return f"no config version known for {wid} at {fmt(at)}"
    out.append(f"{wid} at {fmt(at)}: config v{live['version']} was live, set by {live['actor']} at "
               f"{fmt(live['applied_minute']) if live['applied_minute'] is not None else 'start'}")
    if live["changed_dims"]:
        out.append(f"  changed: {', '.join(_j(live['changed_dims']) or [])}")
    pid = live["proposal_id"]
    if live["actor"] == "supervisor":
        out.append("  This was a client supervisor's edit. It bypasses governance; we recorded it and did not "
                   "overwrite it.")
        for d in s.q("SELECT * FROM decisions WHERE workflow_id=? AND minute>=? AND kind IN "
                     "('supervisor_edit','experiment_interrupted','incident_opened') ORDER BY minute LIMIT 5",
                     (wid, live["applied_minute"])):
            out.append(f"  {fmt(d['minute'])} {d['kind']}: {d['summary']}")
    if not pid:
        return "\n".join(out)
    p = s.one("SELECT * FROM proposals WHERE proposal_id=?", (pid,))
    if not p:
        out.append(f"  proposal {pid} (not in our store)")
        return "\n".join(out)
    body = _j(p["body"])
    out.append("")
    out.append(f"WHY: {body['change_summary']}")
    for line in body["rationale"].split("\n"):
        out.append(f"  {line}")
    out.append(f"  impact_score {body['impact_score']} | affected_leads {body['affected_leads']} | path {p['path']}")
    for k, v in (body.get("diff") or {}).items():
        out.append(f"  diff {k}: {v}")
    out.append("")
    out.append("WHO / WHEN:")
    out.append(f"  {pid} prepared {fmt(p['created_minute'])}, submitted {fmt(p['submitted_minute'])}"
               + (f" as {p['approval_id']}" if p["approval_id"] else ""))
    if p["path"] == "approval":
        out.append(f"  approved by the {wid} client approver at {fmt(p['decided_minute'])}; applied "
                   f"{fmt(p['applied_minute'])} (apply_by {fmt(p['apply_by'])})")
    else:
        out.append(f"  auto-applied {fmt(p['applied_minute'])} within the workflow's auto_apply policy")
    owners = sorted({r["owner"] for r in s.q("SELECT owner FROM intents WHERE proposal_id=?", (pid,))})
    out.append("")
    out.append("EVIDENCE TRAIL (before the change):")
    for o in owners:
        for d in s.q("SELECT * FROM decisions WHERE ref=? AND minute<=? ORDER BY id", (o, p["applied_minute"] or at)):
            out.append(f"  {fmt(d['minute'])} [{o}] {d['kind']}: {d['summary']}")
    out.append("")
    out.append("WHAT HAPPENED NEXT:")
    for o in owners:
        for d in s.q("SELECT * FROM decisions WHERE ref=? AND minute>? ORDER BY id LIMIT 12",
                     (o, p["applied_minute"] or at)):
            out.append(f"  {fmt(d['minute'])} [{o}] {d['kind']}: {d['summary']}")
    nxt = s.q("SELECT * FROM config_versions WHERE workflow_id=? AND version>? ORDER BY version LIMIT 3",
              (wid, live["version"]))
    for r in nxt:
        out.append(f"  next: v{r['version']} by {r['actor']} at {fmt(r['applied_minute'])} "
                   f"({', '.join(_j(r['changed_dims']) or [])})")
    return "\n".join(out)


def view(s: Store, wid: str | None = None, tenant: str | None = None) -> str:
    wfs = s.get("workflows") or {}
    scope = tenant_workflows(s, tenant)
    now = s.get("last_minute", 0)
    out = [f"SUPERVISOR VIEW at {fmt(now)}" + (f" for {tenant}" if tenant else "")]
    for w in sorted(wfs):
        if (wid and w != wid) or (scope is not None and w not in scope):
            continue
        cv = s.one("SELECT * FROM config_versions WHERE workflow_id=? AND config IS NOT NULL ORDER BY version DESC "
                   "LIMIT 1", (w,))
        cfg = _j(cv["config"]) if cv else {}
        out.append("")
        out.append(f"== {w} ({wfs[w]['tenant_id']}) primary={wfs[w]['objective']['primary']} "
                   f"config v{cv['version'] if cv else '?'} prompt {cfg.get('prompt_version')}")
        for c, dims in (cfg.get("allocations") or {}).items():
            out.append(f"   {c}: " + " | ".join(f"{d}: {_dist(v)}" for d, v in dims.items()))
        if cfg.get("cohorts"):
            out.append("   cohorts: " + ", ".join(f"{c['id']}={c['where']}" for c in cfg["cohorts"]))
        out.append("  BEING TESTED:")
        for e in s.q("SELECT * FROM experiments WHERE workflow_id=? AND status IN ('planned','running','promoting',"
                     "'stopping') ORDER BY exp_id", (w,)):
            le = _j(e["last_eval"]) or {}
            arms = le.get("arms", {})
            ctrl = le.get("control")
            line = f"   {e['exp_id']} {e['dim']} [{e['status']}] incumbent {e['control']}"
            if ctrl:
                line += f" {ctrl[0]}/{ctrl[1]}"
            out.append(line)
            for a, c in arms.items():
                p = c["primary"]
                out.append(f"     {a}: {p['x_t']}/{p['n_t']} = {p['rate_t']} delta {p['delta']:+.3f} CS {p['cs']}")
        out.append("  WINNING (promoted, monitored for decay):")
        for e in s.q("SELECT * FROM experiments WHERE workflow_id=? AND status IN ('monitoring','rolling_back')", (w,)):
            pr = (_j(e["plan"]) or {}).get("promotion", {})
            mon = _j(e["monitor"]) or {}
            out.append(f"   {e['dim']}: {pr.get('winner')} (lift {pr.get('lift')} at promotion, recent "
                       f"{mon.get('lift_recent')}) [{e['status']}]")
        out.append("  WAITING FOR YOU:")
        for p in s.q("SELECT * FROM proposals WHERE workflow_id=? AND status IN ('prepared','pending','approved')",
                     (w,)):
            b = _j(p["body"])
            out.append(f"   {p['approval_id'] or p['proposal_id']} [{p['status']}] impact {b['impact_score']} "
                       f"affected {b['affected_leads']}: {b['change_summary']}")
        out.append("  INCIDENTS:")
        for i in s.q("SELECT * FROM incidents WHERE workflow_id=? ORDER BY opened_minute DESC LIMIT 5", (w,)):
            out.append(f"   {i['incident_id']} {i['kind']} {i['key']} [{i['status']}] opened {fmt(i['opened_minute'])}"
                       + (f" resolved {fmt(i['resolved_minute'])}" if i["resolved_minute"] else ""))
        out.append("  CHANGED TODAY:")
        day0 = (now // 1440) * 1440
        for r in s.q("SELECT * FROM config_versions WHERE workflow_id=? AND applied_minute>=? ORDER BY version",
                     (w, day0)):
            summ = ""
            if r["proposal_id"]:
                p = s.one("SELECT body FROM proposals WHERE proposal_id=?", (r["proposal_id"],))
                summ = _j(p["body"])["change_summary"] if p else ""
            out.append(f"   {fmt(r['applied_minute'])} v{r['version']} by {r['actor']} "
                       f"({', '.join(_j(r['changed_dims']) or [])}) {summ}")
    al = s.q("SELECT * FROM alerts WHERE resolved_minute IS NULL ORDER BY severity, minute")
    # a tenant sees alerts on its own workflows only; platform-wide alerts (workflow None) are the operator's
    al = [a for a in al if (not wid or a["workflow_id"] in (wid, None)) and
          (scope is None or a["workflow_id"] in scope)]
    out.append("")
    out.append(f"OPEN ALERTS ({len(al)}):")
    for a in al:
        out.append(f"  [{a['severity']}] {fmt(a['minute'])} {a['workflow_id'] or '-'}: {a['message']}")
    return "\n".join(out)


def health(s: Store) -> str:
    rows = s.q("SELECT * FROM metrics ORDER BY minute")
    if not rows:
        return "no metrics yet"
    out = ["LOOP HEALTH"]
    ev = sum(r["events"] for r in rows)
    ing = sum(r["ingest_s"] for r in rows)
    dec = sum(r["decide_s"] for r in rows)
    adv = sum(r["advance_s"] for r in rows)
    out.append(f"  events ingested {ev:,} in {ing:.1f}s -> {ev / ing if ing else 0:,.0f} events/s")
    out.append(f"  decision time {dec:.1f}s, simulator advance time {adv:.1f}s over {len(rows)} ticks")
    days = {}
    for r in rows:
        d = days.setdefault(r["minute"] // 1440, [0, 0.0, 0.0, 0, None, None])
        d[0] += r["events"]
        d[1] += r["ingest_s"] + r["decide_s"]
        d[2] += r["ingest_s"] + r["decide_s"] + r["advance_s"]
        d[3] += r["decisions"]
        d[4] = r["credits"] if d[4] is None else d[4]
        d[5] = r["credits"]
    out.append("  per simulated day: events | controller s | wall s incl. simulator | decisions | credits start->end")
    for day, d in sorted(days.items()):
        out.append(f"    D{day}: {d[0]:>9,} | {d[1]:7.1f} | {d[2]:7.1f} | {d[3]:5} | {d[4]} -> {d[5]}")
    lag = max((r["ingest_s"] + r["decide_s"] for r in rows), default=0)
    out.append(f"  worst tick decision lag (ingest+decide): {lag:.2f}s")
    rss = [r["rss_mb"] for r in rows if r["rss_mb"]]
    if rss:
        out.append(f"  peak memory {max(rss):.0f} MB")
    out.append(f"  API errors (cumulative) {rows[-1]['api_errors']}")
    pages = s.q("SELECT * FROM alerts WHERE severity='page' ORDER BY minute")
    warns = s.q("SELECT * FROM alerts WHERE severity='warn' ORDER BY minute")
    out.append(f"  alerts: {len(pages)} page, {len(warns)} warn "
               f"({sum(1 for a in pages if a['resolved_minute'] is None)} pages still open)")
    for a in pages:
        out.append(f"    PAGE {fmt(a['minute'])} {a['workflow_id'] or '-'}: {a['message']}"
                   + (f" (resolved {fmt(a['resolved_minute'])})" if a["resolved_minute"] else ""))
    return "\n".join(out)


def log_lines(s: Store, wid=None, kind=None, limit=50, tenant: str | None = None) -> str:
    sql, args = "SELECT * FROM decisions WHERE 1=1", []
    scope = tenant_workflows(s, tenant)
    if scope is not None:
        sql += f" AND workflow_id IN ({','.join('?' * len(scope)) or 'NULL'})"
        args += sorted(scope)
    if wid:
        sql += " AND workflow_id=?"
        args.append(wid)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = list(reversed(s.q(sql, args)))
    return "\n".join(f"{fmt(r['minute'])} {r['workflow_id'] or '-':12} {r['kind']:24} {r['summary']}" for r in rows)
