"""Impact score, affected leads and dry runs (C10). Honest means: computed from calls that actually happened,
with the uncertainty stated, and with anything we have no data for labelled as an assumption.

- affected_leads: expected leads per day whose experience changes. For welcome/voice that is connected calls
  in scope x share of traffic that moves; for caller ID it is new leads dialled per day x share that moves.
- dry_run: replay the proposed allocation on the last 7 days of observed per-variant rates in the same scope.
  Allocation is randomised per dial, so observed per-variant rates are unbiased for that population.
- gain_leads_per_day_low: lower 95% bound of extra concluded leads per day (never negative). The impact
  score is 1 - exp(-gain/50), so it ranks requests by conservative business value.
"""
from __future__ import annotations

import math

from .db import Store
from .util import normalise, tv_distance

DIM_COL = {"welcome_message": "wm", "voice": "voice", "caller_id": "cid", "prompt_version": "prompt"}


def volume(s: Store, wid: str, minute: int, days: int = 3) -> dict:
    """Average daily volume over the last complete days (falls back to history)."""
    today = minute // 1440
    rows = s.q("SELECT * FROM daily WHERE workflow_id=? AND day < ? AND dials > 0 ORDER BY day DESC LIMIT ?",
               (wid, today, days))
    if not rows:
        rows = s.q("SELECT * FROM daily WHERE workflow_id=? AND dials > 0 ORDER BY day DESC LIMIT ?", (wid, days))
    if not rows:
        return {"dials": 0.0, "conn": 0.0, "eng": 0.0, "new_leads": 0.0, "uploaded": 0.0}
    k = len(rows)
    return {"dials": sum(r["dials"] for r in rows) / k, "conn": sum(r["conn"] for r in rows) / k,
            "eng": sum(r["eng"] for r in rows) / k, "new_leads": sum(r["leads_dialled"] for r in rows) / k,
            "uploaded": sum(r["uploaded"] for r in rows) / k}


def scope_share(s: Store, wid: str, cohorts: list[str], since_hour: int) -> float:
    r = s.one("SELECT SUM(dials) AS d FROM agg_hour WHERE workflow_id=? AND hour>=?", (wid, since_hour))
    tot = r["d"] or 0
    if not tot:
        return 1.0
    q = ",".join("?" * len(cohorts))
    r2 = s.one(f"SELECT SUM(dials) AS d FROM agg_hour WHERE workflow_id=? AND hour>=? AND cohort_id IN ({q})",
               (wid, since_hour, *cohorts))
    return (r2["d"] or 0) / tot


def variant_counts(s: Store, wid: str, dim: str, cohorts: list[str], kpi: str, since_hour: int,
                   versions: list[int] | None = None) -> dict[str, tuple[int, int]]:
    """{value: (successes, trials)} where trials are dials for pickup and connected calls for engagement."""
    col = DIM_COL[dim]
    num, den = ("conn", "dials") if kpi == "pickup_rate" else ("eng", "conn")
    q = ",".join("?" * len(cohorts))
    sql = (f"SELECT {col} AS v, SUM({num}) AS x, SUM({den}) AS n FROM agg_hour WHERE workflow_id=? AND hour>=? "
           f"AND cohort_id IN ({q})")
    args = [wid, since_hour, *cohorts]
    if versions:
        sql += f" AND config_version IN ({','.join('?' * len(versions))})"
        args += versions
    sql += f" GROUP BY {col}"
    return {r["v"]: (r["x"] or 0, r["n"] or 0) for r in s.q(sql, args)}


def allocation_change(s: Store, wid: str, minute: int, dim: str, cohorts: list[str], cur: dict, new: dict,
                      kpi: str, exploration: bool = False) -> dict:
    since = minute // 60 - 7 * 24
    counts = variant_counts(s, wid, dim, cohorts, kpi, since)
    vol = volume(s, wid, minute)
    share = scope_share(s, wid, cohorts, since)
    moved = tv_distance(cur, new)
    cur, new = normalise(cur), normalise(new)
    incumbent = max(cur, key=cur.get) if cur else None
    ref = counts.get(incumbent, (0, 0))
    ref_rate = ref[0] / ref[1] if ref[1] else None
    replay, assumed = {}, []
    for v in sorted(set(cur) | set(new)):
        x, n = counts.get(v, (0, 0))
        replay[v] = {"observed_rate": round(x / n, 4) if n else None, "n": n,
                     "share_now": round(cur.get(v, 0), 3), "share_proposed": round(new.get(v, 0), 3)}
        if not n:
            assumed.append(v)

    def expect(dist):
        mu, var = 0.0, 0.0
        for v, w in dist.items():
            x, n = counts.get(v, (0, 0))
            if n:
                p = x / n
                mu += w * p
                var += w * w * p * (1 - p) / n
            elif ref_rate is not None:
                mu += w * ref_rate
        return mu, var

    mu_c, var_c = expect(cur)
    mu_n, var_n = expect(new)
    delta = mu_n - mu_c
    half = 1.96 * math.sqrt(var_c + var_n)
    per_day = vol["conn"] if kpi == "engagement_rate" else vol["dials"] * (vol["eng"] / vol["conn"] if vol["conn"]
                                                                            else 0.5)
    per_day *= share
    gain_low = 0.0 if exploration else max(0.0, (delta - half) * per_day)
    if dim == "caller_id":
        affected = vol["new_leads"] * share * moved
    else:
        affected = vol["conn"] * share * moved
    return {
        "affected_leads": int(round(affected)),
        "gain_leads_per_day_low": round(gain_low, 2),
        "dry_run": {
            "method": f"replay of proposed {dim} allocation on observed per-variant {kpi} in the last 7 days, "
                      f"same cohort scope ({', '.join(cohorts)}); allocation is randomised per dial",
            "per_variant": replay,
            f"expected_{kpi}_now": round(mu_c, 4),
            f"expected_{kpi}_proposed": round(mu_n, 4),
            "expected_delta_95ci": [round(delta - half, 4), round(delta + half, 4)],
            "assumed_equal_to_incumbent_no_data": assumed,
            "share_of_traffic_moved": round(moved, 3),
            "share_of_workflow_in_scope": round(share, 3),
            "kind": "exploration (value is information, not expected lift)" if exploration else "exploitation",
        },
    }
