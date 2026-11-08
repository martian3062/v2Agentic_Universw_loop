"""Experiments (C6), winner promotion, decay detection and rollback (C7), on shared cohorts (C2).

Design
- One experiment per (workflow, dimension, cohort) at a time. Engagement-primary workflows test welcome_message
  and voice (pickup-primary workflows are served by the caller-ID policy). Welcome and voice run concurrently as
  a factorial design: the simulator samples each dimension independently per dial, so each comparison uses all
  traffic and the interaction can be checked.
- Fixed allocation for the life of an experiment, incumbent share by the square-root rule. Only calls that
  actually ran under a config version where that allocation was live are evidence.
- Sequential phase (hourly looks): always-valid mSPRT confidence sequences at alpha/k.
  * harm: a challenger whose sequence lies below 0 (or that breaks the guardrail) is dropped early; so is one
    that is significantly worse over the last 24h (a variant decaying mid-test);
  * winner: primary sequence above 0, every arm >= MIN_ARM_N, guardrail NON-INFERIORITY shown (the guardrail
    sequence's lower bound above -margin), and over the last 24h still at least half as far ahead as overall.
    A primary win without guardrail evidence is held and logged with the sample still needed.
- Interim (INTERIM_DAYS) and conclusion: segment analysis per single-feature segment with always-valid
  sequences, Bonferroni over every segment value x every original challenger (a family fixed in advance), so
  repeated looks keep the guarantee.
  * with a winner: segments where the winner is significantly worse are carved out and keep the incumbent;
  * without one: segments where a challenger is significantly better AND shows guardrail non-inferiority get
    their own cohort and that variant.
  Carved cohorts come from a single feature, so they never overlap each other.
- Error rates: workflow-level claims <= ALPHA, segment claims <= SEGMENT_ALPHA, per experiment, asymptotically
  (normal approximation with plug-in variances; decisions wait for >= MIN_ARM_N trials per arm, where the
  simulations in tests/ctl/test_stats.py show the rate is held). Restrictions added on top (recency, interaction,
  interim drops by point estimate) only remove claims, so they cannot raise these rates.
- Every promotion keeps a 10% holdout on the incumbent and is monitored for decay (see monitor()).
"""
from __future__ import annotations

import json
import logging
import math
import re

from . import settings as S
from .db import Store, atomic
from .estimates import DIM_COL, allocation_change
from .stats import dunnett_control_share, fixed_z, msprt, msprt_strat, fixed_strat, required_n
from .util import cohorts_inheriting, effective_dist, fmt, normalise, tv_distance

log = logging.getLogger("controller.exp")
ACTIVE = ("planned", "running", "promoting", "monitoring", "stopping", "rolling_back")
SAME_DIST = 0.005   # allocations are stored rounded to 4 decimals; anything closer than this is "unchanged"
DERISK_KINDS = ("experiment_drop_arm", "experiment_stop", "experiment_rollback")


def guardrail_of(wf: dict) -> tuple[str, str, float]:
    """(primary kpi, guardrail kpi, max drop). The objective says e.g. {"pickup_rate_max_drop": 0.02};
    we read the drop as absolute rate units (0.02 = 2 percentage points)."""
    prim = wf["objective"]["primary"]
    g = wf["objective"].get("guardrail", {})
    other = "pickup_rate" if prim == "engagement_rate" else "engagement_rate"
    return prim, other, float(g.get(f"{other}_max_drop", 0.02))


def shrunk_lift(promo: dict) -> float:
    """The lift decay is measured against: the lower bound of the sequence at promotion, or half the point
    estimate, whichever is larger (never more than the point estimate)."""
    lift = promo.get("lift") or 0.0
    lo = (promo.get("cs") or [lift])[0]
    return max(0.0, min(lift, max(lo, 0.5 * lift)))


def cohort_slug(feature: str, value: str) -> str:
    v = value.replace("+", "p")
    return f"{feature}_{re.sub(r'[^A-Za-z0-9]+', '_', v)}".strip("_")


def scope_of(cfg: dict, cohort: str, dim: str) -> list[str] | None:
    """Cohort ids whose `dim` comes from `cohort`'s allocation; None if the cohort no longer sets it."""
    if cohort == "default":
        return cohorts_inheriting(cfg, dim)
    if any(c["id"] == cohort for c in cfg.get("cohorts", [])) and dim in cfg.get("allocations", {}).get(cohort, {}):
        return [cohort]
    return None


class Experiments:
    def __init__(self, store: Store, sim, gov, incidents, workflows: dict):
        self.s, self.sim, self.gov, self.inc, self.wfs = store, sim, gov, incidents, workflows
        gov.callbacks["exp"] = self

    # ------------------------------------------------------------ queries
    def active(self, wid: str, dim: str | None = None, cohort: str | None = None):
        sql = f"SELECT * FROM experiments WHERE workflow_id=? AND status IN {ACTIVE}"
        args = [wid]
        if dim:
            sql += " AND dim=?"
            args.append(dim)
        if cohort:
            sql += " AND cohort_id=?"
            args.append(cohort)
        return self.s.q(sql + " ORDER BY exp_id", args)

    def _save(self, exp_id: str, **cols):
        sets = ", ".join(f"{k}=?" for k in cols)
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in cols.values()]
        with self.s:
            self.s.x(f"UPDATE experiments SET {sets} WHERE exp_id=?", (*vals, exp_id))

    def _get(self, exp_id: str):
        return self.s.one("SELECT * FROM experiments WHERE exp_id=?", (exp_id,))

    def _rec(self, minute, exp, kind, summary, data=None, version=None):
        with self.s:
            self.s.record(minute, exp["workflow_id"], kind, f"{exp['exp_id']} ({exp['dim']}"
                          + (f" in {exp['cohort_id']}" if exp["cohort_id"] != "default" else "") + f"): {summary}",
                          data, version=version, ref=f"exp:{exp['exp_id']}")

    def counts(self, exp, kpi: str, since_hour: int | None = None) -> dict:
        plan = json.loads(exp["plan"])
        versions = json.loads(exp["versions"])
        cohorts = plan["scope_cohorts"]
        if not versions or not cohorts:
            return {}
        col = DIM_COL[exp["dim"]]
        num, den = ("conn", "dials") if kpi == "pickup_rate" else ("eng", "conn")
        start = exp["live_minute"] // 60
        if since_hour is not None:
            start = max(start, since_hour)
        rows = self.s.q(f"SELECT {col} AS v, SUM({num}) x, SUM({den}) n FROM agg_hour WHERE workflow_id=? AND hour>=? "
                        f"AND config_version IN ({','.join('?' * len(versions))}) "
                        f"AND cohort_id IN ({','.join('?' * len(cohorts))}) GROUP BY {col}",
                        (exp["workflow_id"], start, *versions, *cohorts))
        return {r["v"]: (r["x"] or 0, r["n"] or 0) for r in rows}

    def strata(self, exp, kpi: str, since_hour: int | None = None) -> dict:
        """{(config_version, day): {arm: (successes, trials)}}. Within one version and day the assignment
        probabilities are constant, so arms are only ever compared inside a stratum (see stats._mh)."""
        plan = json.loads(exp["plan"])
        versions = json.loads(exp["versions"])
        cohorts = plan["scope_cohorts"]
        if not versions or not cohorts:
            return {}
        col = DIM_COL[exp["dim"]]
        num, den = ("conn", "dials") if kpi == "pickup_rate" else ("eng", "conn")
        start = exp["live_minute"] // 60
        if since_hour is not None:
            start = max(start, since_hour)
        rows = self.s.q(f"SELECT config_version ver, hour/24 day, {col} AS v, SUM({num}) x, SUM({den}) n FROM agg_hour "
                        f"WHERE workflow_id=? AND hour>=? AND config_version IN ({','.join('?' * len(versions))}) "
                        f"AND cohort_id IN ({','.join('?' * len(cohorts))}) GROUP BY ver, day, {col}",
                        (exp["workflow_id"], start, *versions, *cohorts))
        out: dict = {}
        for r in rows:
            out.setdefault((r["ver"], r["day"]), {})[r["v"]] = (r["x"] or 0, r["n"] or 0)
        return out

    @staticmethod
    def _pairs(strata: dict, arm: str, control: str) -> list:
        return [(*s.get(arm, (0, 0)), *s.get(control, (0, 0))) for s in strata.values()]

    # ------------------------------------------------------------ tick
    def tick(self, minute: int):
        for wid in self.wfs:
            for exp in self.active(wid):
                self._track_versions(minute, exp)
            if minute % S.EXPERIMENT_EVAL_EVERY:
                continue
            for exp in self.active(wid):
                exp = self._get(exp["exp_id"])
                if exp["status"] == "running":
                    self.evaluate(minute, exp)
                elif exp["status"] == "monitoring":
                    self.monitor(minute, exp)
                elif exp["status"] == "planned":
                    self._retry_plan(minute, exp)
                else:
                    self._retry_pending(minute, exp)
            self.maybe_start(minute, wid)

    def _track_versions(self, minute: int, exp):
        """Count a new config version as evidence only if this experiment's allocation is unchanged in it."""
        if exp["status"] == "planned":
            return
        wid = exp["workflow_id"]
        version, cfg = self.gov.current(wid)
        versions = json.loads(exp["versions"])
        plan = json.loads(exp["plan"])
        scope = scope_of(cfg, exp["cohort_id"], exp["dim"])
        actual = effective_dist(cfg, exp["cohort_id"], exp["dim"])
        if version in versions:
            if scope and set(scope) != set(plan["scope_cohorts"]):
                plan["scope_cohorts"] = scope
                self._save(exp["exp_id"], plan=plan)
            return
        if scope and tv_distance(actual, plan["expected_dist"]) < SAME_DIST:
            versions.append(version)
            plan["scope_cohorts"] = scope
            self._save(exp["exp_id"], versions=versions, plan=plan)
            return
        if any(i["status"] == "in_proposal" for i in self.gov.open_intents(f"exp:{exp['exp_id']}")):
            return  # our own change is in flight; on_applied registers the version
        row = self.s.one("SELECT actor FROM config_versions WHERE workflow_id=? AND version=?", (wid, version))
        actor = row["actor"] if row else "unknown"
        self._end(minute, exp, "interrupted",
                  f"allocation changed outside this experiment (v{version}, by {actor}); we stop counting and do not "
                  "revert it", {"found": actual, "expected": plan["expected_dist"], "actor": actor})

    @atomic
    def _end(self, minute: int, exp, status: str, summary: str, data=None, winner=None):
        self.gov.cancel_owner_intents(minute, f"exp:{exp['exp_id']}", f"experiment {status}")
        self._save(exp["exp_id"], status=status, ended_minute=minute, result=data or {}, winner=winner)
        self._rec(minute, exp, f"experiment_{status}", summary, data)

    # ------------------------------------------------------------ start
    def _challengers_for(self, wid: str, dim: str, control: str, minute: int) -> tuple[list, int] | None:
        """Re-run rules: after no winner, only arms that looked promising get one more round; after a decay
        rollback the decayed variant is retired; after two rounds the dimension is settled."""
        wf = self.wfs[wid]
        excluded = set(self.s.get(f"retired:{wid}:{dim}", []))
        if dim == "caller_id":
            excluded |= self.inc.quarantined(wid)
        last = self.s.one("SELECT * FROM experiments WHERE workflow_id=? AND dim=? AND cohort_id='default' "
                          "ORDER BY exp_id DESC LIMIT 1", (wid, dim))
        rnd = 1
        pool = [o for o in wf["options"][dim] if o != control and o not in excluded]
        if last:
            if last["ended_minute"] is not None and minute - last["ended_minute"] < S.REJECTION_COOLDOWN_HOURS * 60:
                return None
            lp = json.loads(last["plan"])
            rnd = lp.get("round", 1) + 1
            if last["status"] == "ended_no_winner":
                promising = (json.loads(last["result"] or "{}")).get("promising", [])
                pool = [o for o in pool if o in promising]
            if rnd > 2 and last["status"] != "interrupted":
                return None
        return (pool, rnd) if pool else None

    def maybe_start(self, minute: int, wid: str):
        wf = self.wfs[wid]
        prim, guard, margin = guardrail_of(wf)
        recent_sup = {d for e in self.s.get(f"supervisor_edits:{wid}", [])
                      if minute - e["minute"] < S.SUPERVISOR_RESPECT_HOURS * 60 for d in e["dims"]}
        dims = [d for d in S.EXPERIMENT_DIMS_BY_PRIMARY.get(prim, ()) if not self.active(wid, d, "default")
                and not self.inc.owns_dim(wid, d) and d not in recent_sup]
        if not dims:
            return
        _, cfg = self.gov.current(wid)
        for dim in dims:
            cur = effective_dist(cfg, "default", dim)
            if not cur:
                continue
            control = max(cur, key=cur.get)
            got = self._challengers_for(wid, dim, control, minute)
            if not got:
                continue
            challengers, rnd = got
            needs_human = dim in S.CUSTOMER_FACING_DIMS or self.gov.choose_path(wid, cfg, cfg, [dim], False) != "auto"
            if needs_human and not self.gov.can_request(wid, minute, urgent=False)[0]:
                continue
            cs = dunnett_control_share(len(challengers))
            shares = {control: cs, **{c: (1 - cs) / len(challengers) for c in challengers}}
            self._plan(minute, wid, dim, prim, guard, margin, control, challengers, shares, cfg, rnd)

    @atomic
    def _plan(self, minute, wid, dim, prim, guard, margin, control, challengers, shares, cfg, rnd):
        n = self.s.next_seq("experiment")
        exp_id = f"exp-{n:03d}"
        alpha = S.ALPHA / len(challengers)
        base = self.s.get(f"baseline:{wid}") or {}
        pr = (base.get("engagement") if prim == "engagement_rate" else base.get("pickup")) or [1, 2]
        p = pr[0] / pr[1] if pr[1] else 0.5
        ratio = shares[control] / shares[challengers[0]]
        plan = {"shares": normalise(shares), "alpha": alpha, "tau": S.TAU, "min_arm_n": S.MIN_ARM_N, "kpi": prim,
                "guardrail": guard, "guardrail_max_drop": margin, "scope_cohorts": cohorts_inheriting(cfg, dim),
                "expected_dist": normalise(shares), "control_share": round(shares[control], 3), "round": rnd,
                "planning": {f"n_per_arm_for_{int(e * 100)}pp": required_n(p, e, alpha, S.TAU, ratio)
                             for e in (0.05, 0.08, 0.12)},
                "interim_days": S.INTERIM_DAYS, "attempts": 0}
        with self.s:
            self.s.x("INSERT INTO experiments(exp_id,workflow_id,dim,cohort_id,kpi,control,arms,plan,status,"
                     "created_minute,versions) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (exp_id, wid, dim, "default", prim, control, json.dumps([control] + challengers),
                      json.dumps(plan), "planned", minute, "[]"))
        exp = self._get(exp_id)
        self._rec(minute, exp, "experiment_planned",
                  f"round {rnd}: test {', '.join(challengers)} vs incumbent {control} on {prim} (guardrail {guard} "
                  f"max drop {margin}); alpha {alpha:.3f} per comparison; incumbent share {shares[control]:.0%}", plan)
        self._file_start(minute, exp)

    @atomic
    def _file_start(self, minute: int, exp):
        wid, dim = exp["workflow_id"], exp["dim"]
        plan = json.loads(exp["plan"])
        _, cfg = self.gov.current(wid)
        est = allocation_change(self.s, wid, minute, dim, plan["scope_cohorts"], effective_dist(cfg, "default", dim),
                                plan["shares"], plan["kpi"], exploration=True)
        arms = json.loads(exp["arms"])
        iid = self.gov.file_intent(
            minute, wid, "experiment_start", f"exp:{exp['exp_id']}",
            [{"op": "set_alloc", "cohort": "default", "dim": dim, "dist": plan["shares"]}],
            summary=f"start experiment {exp['exp_id']}: {dim} {', '.join(arms[1:])} vs {arms[0]}",
            rationale=(f"Controlled test on the default population. Incumbent {arms[0]} keeps "
                       f"{plan['control_share']:.0%}. We promote only if an always-valid test on {plan['kpi']} "
                       f"excludes zero at alpha {plan['alpha']:.3f}, the variant is still ahead over the last "
                       f"{S.RECENT_HOURS}h, and {plan['guardrail']} does not drop more than "
                       f"{plan['guardrail_max_drop']}. Harmful arms are dropped as soon as the test shows harm. "
                       f"At a planned interim after {plan['interim_days']} days, arms estimated worse leave and "
                       f"segment winners get their own cohort. Planning: {plan['planning']}"),
            evidence={"experiment": exp["exp_id"], "plan": plan}, estimate=est)
        if iid is None:
            self._end(minute, exp, "abandoned", "start would not change the config")

    @atomic
    def _retry_plan(self, minute: int, exp):
        if self.gov.open_intents(f"exp:{exp['exp_id']}"):
            return
        plan = json.loads(exp["plan"])
        if plan.get("retry_after") and minute < plan["retry_after"]:
            return
        if plan["attempts"] >= S.MAX_ATTEMPTS_PER_INTENT:
            self._end(minute, exp, "abandoned", f"start request returned {plan['attempts']} times; not retrying")
            return
        if self.gov.can_request(exp["workflow_id"], minute, urgent=False)[0]:
            self._file_start(minute, exp)

    # ------------------------------------------------------------ sequential evaluation
    def _comparisons(self, exp, since_hour=None, mode: str = "seq", alpha: float | None = None):
        """Every arm vs the incumbent on both KPIs, compared within (config version, day) strata and combined
        (Mantel-Haenszel). mode 'seq': always-valid sequences (decisions); 'fixed': fixed-sample tests (recency
        restrictions, which can only remove claims)."""
        plan = json.loads(exp["plan"])
        arms = [a for a in json.loads(exp["arms"]) if a not in plan.get("dropped", [])]
        control = exp["control"]
        prim = self.strata(exp, plan["kpi"], since_hour)
        guard = self.strata(exp, plan["guardrail"], since_hour)
        al = plan["alpha"] if alpha is None else alpha
        xc = sum(s.get(control, (0, 0))[0] for s in prim.values())
        nc = sum(s.get(control, (0, 0))[1] for s in prim.values())
        out = {}
        for a in arms:
            if a == control:
                continue
            pp, gp = self._pairs(prim, a, control), self._pairs(guard, a, control)
            if mode == "seq":
                # the guardrail sequence mixes over effects on the scale of its margin: it exists to bound drops
                # of that size
                tau_g = max(plan["guardrail_max_drop"], 0.01)
                out[a] = {"primary": msprt_strat(pp, al, plan["tau"]), "guardrail": msprt_strat(gp, al, tau_g)}
            else:
                out[a] = {"primary": fixed_strat(pp, al), "guardrail": fixed_strat(gp, al)}
        return plan, arms, out, (xc, nc)

    @atomic
    def evaluate(self, minute: int, exp):
        if self.gov.open_intents(f"exp:{exp['exp_id']}"):
            return
        plan, arms, comps, (xc, nc) = self._comparisons(exp)
        margin = plan["guardrail_max_drop"]
        snap = {a: {"primary": c["primary"].as_dict(), "guardrail": c["guardrail"].as_dict()} for a, c in comps.items()}
        self._save(exp["exp_id"], last_eval={"minute": minute, "control": [xc, nc], "arms": snap})
        enough = nc >= plan["min_arm_n"] and all(c["primary"].n_t >= plan["min_arm_n"] for c in comps.values())
        harmful = [a for a, c in comps.items() if c["primary"].n_t >= plan["min_arm_n"] and
                   (c["primary"].worse or (c["guardrail"].worse and c["guardrail"].delta < -margin))]
        # recency harm: an arm that was fine early but is now clearly worse (a variant decaying mid-test) is dropped
        # even though its cumulative sequence is still propped up by its early days. Strict level: hourly looks.
        _, _, recent, _ = self._comparisons(exp, since_hour=minute // 60 - S.RECENT_HOURS, mode="fixed",
                                            alpha=S.RECENT_HARM_ALPHA)
        for a, c in recent.items():
            if a not in harmful and c["primary"].n_t >= plan["min_arm_n"] // 2 and c["primary"].worse:
                harmful.append(a)
                comps[a]["recent"] = c["primary"]
                snap[a]["recent"] = c["primary"].as_dict()
        primary_winners = [a for a, c in comps.items() if enough and c["primary"].better and a not in harmful]
        # Non-inferiority on the guardrail must be shown, not merely "not disproven": the always-valid lower bound of
        # (challenger - incumbent) on the guardrail KPI must sit above -margin.
        winners = [a for a in primary_winners if comps[a]["guardrail"].cs_low > -margin]
        for a in set(primary_winners) - set(winners):
            g = comps[a]["guardrail"]
            held = plan.setdefault("held", {})
            if minute - held.get(f"guard:{a}", -10**9) >= 6 * 60:
                held[f"guard:{a}"] = minute
                self._save(exp["exp_id"], plan=plan)
                p = g.x_c / g.n_c if g.n_c else 0.5
                need = required_n(p, margin, plan["alpha"], max(margin, 0.01), 1.0)
                self._rec(minute, exp, "promotion_held",
                          f"{a} wins on {plan['kpi']} but {plan['guardrail']} non-inferiority is not shown yet: "
                          f"lower bound {g.cs_low:+.3f} vs -{margin} margin on {g.n_t} trials (about {need} per arm "
                          "needed if there is no real effect); still testing", {"guardrail": g.as_dict()})
        if winners:
            _, _, recent, _ = self._comparisons(exp, since_hour=minute // 60 - S.RECENT_HOURS, mode="fixed")
            # still winning = the last day's lead is at least half the overall lead (a fading variant fails this)
            holding = [a for a in winners if recent.get(a) and
                       recent[a]["primary"].delta >= 0.5 * comps[a]["primary"].delta]
            for a in set(winners) - set(holding):
                held = plan.setdefault("held", {})
                if minute - held.get(a, -10**9) >= 6 * 60:
                    held[a] = minute
                    self._save(exp["exp_id"], plan=plan)
                    r = recent.get(a, {}).get("primary")
                    self._rec(minute, exp, "promotion_held",
                              f"{a} wins overall (delta {comps[a]['primary'].delta:+.3f}) but not in the last "
                              f"{S.RECENT_HOURS}h (delta {r.delta if r else 0:+.3f} on {r.n_t if r else 0} calls); "
                              "not promoting a variant that may already be fading", None)
            winners = holding
        age_days = (minute - exp["live_minute"]) / 1440
        # Routine requests are only filed when they can go out now, so the evidence is fresh when a human reads it;
        # after a rejection they wait out the cooldown. Harm is never held back.
        sendable = self.gov.can_request(exp["workflow_id"], minute, urgent=False)[0] and \
            minute >= plan.get("retry_after", 0)
        if harmful:
            self._drop(minute, exp, plan, harmful, snap)
        elif winners and sendable:
            best = max(winners, key=lambda a: comps[a]["primary"].delta)
            self._conclude(minute, exp, plan, comps, snap, best, f"{best} wins on {plan['kpi']}")
        elif enough and sendable and all(c["primary"].cs_high < S.FUTILITY_EFFECT for c in comps.values()):
            self._conclude(minute, exp, plan, comps, snap, None,
                           f"futility: no challenger can plausibly beat the incumbent by {S.FUTILITY_EFFECT:.0%}")
        elif age_days >= S.INTERIM_DAYS and not plan.get("interim_done") and sendable:
            self._interim(minute, exp, plan, comps, snap)

    @atomic
    def _interim(self, minute, exp, plan, comps, snap):
        """Planned interim look. Always-valid sequences stay valid when arms are dropped adaptively, so the
        experiment continues on the same data: arms estimated worse than the incumbent leave, segment winners
        get their cohort, the rest keep running until a winner, harm or futility."""
        wid, dim, control = exp["workflow_id"], exp["dim"], exp["control"]
        _, _, recent, _ = self._comparisons(exp, since_hour=minute // 60 - S.RECENT_HOURS, mode="fixed")
        # a loser is behind the incumbent overall, or behind over the last day (it may be fading)
        losers = [a for a, c in comps.items() if c["primary"].delta <= 0 or
                  (a in recent and recent[a]["primary"].n_t >= 50 and recent[a]["primary"].delta < 0)]
        keep = [a for a in comps if a not in losers]
        if not keep:   # nothing left to test: conclude (segment winners, if any, still get their cohort there)
            self._conclude(minute, exp, plan, comps, snap, None,
                           f"interim after {S.INTERIM_DAYS} days: every challenger is estimated worse than {control}")
            return
        segs = self._segments(exp, plan, None, list(comps))
        if not losers and not segs:
            plan["interim_done"] = True
            self._save(exp["exp_id"], plan=plan)
            self._rec(minute, exp, "interim", f"all challengers still estimated ahead of {control}; continuing", snap)
            return
        cs = dunnett_control_share(len(keep))
        dist = normalise({control: cs, **{a: (1 - cs) / len(keep) for a in keep}})
        ops = [{"op": "set_alloc", "cohort": exp["cohort_id"], "dim": dim, "dist": dist}]
        _, cfg = self.gov.current(wid)
        carved = self._carve_ops(minute, exp, segs, cfg, ops)
        est = allocation_change(self.s, wid, minute, dim, plan["scope_cohorts"],
                                effective_dist(cfg, exp["cohort_id"], dim), dist, plan["kpi"], exploration=True)
        parts = []
        if losers:
            parts.append("Arms estimated worse than the incumbent leave the test: " + "; ".join(
                f"{a} delta {snap[a]['primary']['delta']:+.3f}" for a in losers) +
                f". Still testing {', '.join(keep)} against {control} on the same always-valid sequence.")
        parts += self._carve_text(carved, control)
        self.gov.file_intent(minute, wid, "experiment_interim", f"exp:{exp['exp_id']}", ops,
                             summary=f"{exp['exp_id']}: interim, {dim}"
                                     + (f" drop {', '.join(losers)}" if losers else "")
                                     + ("; cohorts " + ", ".join(f"{c['cohort']}->{c['variant']}" for c in carved)
                                        if carved else ""),
                             rationale=" ".join(parts), evidence={"experiment": exp["exp_id"], "analysis": snap,
                                                                 "segments": carved},
                             estimate=est, derisk=not carved)
        plan["pending_drop"] = losers
        plan["conclusion"] = {"winner": None, "carved": carved, "interim": True}
        self._save(exp["exp_id"], plan=plan)
        self._rec(minute, exp, "interim", " ".join(parts), {"analysis": snap, "segments": carved})

    def _carve_ops(self, minute, exp, segs, cfg, ops) -> list[dict]:
        wid, dim, control = exp["workflow_id"], exp["dim"], exp["control"]
        existing = {c["id"] for c in cfg.get("cohorts", [])}
        carved = []
        for sg in segs:
            cid = cohort_slug(sg["feature"], sg["value"])
            if cid not in existing:
                ops.append({"op": "add_cohort", "cohort": {"id": cid, "where": {sg["feature"]: [sg["value"]]}}})
                existing.add(cid)
            cdist = {control: 1.0} if sg["kind"] == "carve_out" else \
                {sg["variant"]: 1 - S.HOLDOUT_SHARE, control: S.HOLDOUT_SHARE}
            ops.append({"op": "set_alloc", "cohort": cid, "dim": dim, "dist": normalise(cdist)})
            carved.append(dict(sg, cohort=cid, dist=normalise(cdist)))
            with self.s:
                self.s.x("INSERT OR IGNORE INTO cohorts VALUES(?,?,?,?,?,?)",
                         (cid, wid, json.dumps({sg["feature"]: [sg["value"]]}), f"exp:{exp['exp_id']}", minute,
                          "proposed"))
        return carved

    @staticmethod
    def _carve_text(carved, control) -> list[str]:
        out = []
        for sg in carved:
            c = sg["comparison"]
            if sg["kind"] == "carve_out":
                out.append(f"Cohort {sg['cohort']} keeps {control}: {sg['against']} is significantly worse there "
                           f"(delta {c['delta']:+.3f}, CI {c['cs']}).")
            else:
                out.append(f"Cohort {sg['cohort']} gets {sg['variant']}: {c['x_t']}/{c['n_t']} vs {c['x_c']}/{c['n_c']}, "
                           f"delta {c['delta']:+.3f}, always-valid CS {c['cs']} (Bonferroni over {sg['family']} "
                           f"segment comparisons); guardrail lower bound {sg['guardrail']['cs'][0]:+.3f} above the "
                           f"-margin.")
        return out

    @atomic
    def _drop(self, minute, exp, plan, harmful, snap):
        dist = dict(plan["expected_dist"])
        for a in harmful:
            dist[exp["control"]] = dist.get(exp["control"], 0) + dist.pop(a, 0)
        dist = normalise(dist)
        wid = exp["workflow_id"]
        _, cfg = self.gov.current(wid)
        est = allocation_change(self.s, wid, minute, exp["dim"], plan["scope_cohorts"],
                                effective_dist(cfg, exp["cohort_id"], exp["dim"]), dist, plan["kpi"])
        why = "; ".join(f"{a}: {plan['kpi']} delta {snap[a]['primary']['delta']:+.3f} CS {snap[a]['primary']['cs']}, "
                        f"{plan['guardrail']} delta {snap[a]['guardrail']['delta']:+.3f}" for a in harmful)
        self.gov.file_intent(minute, wid, "experiment_drop_arm", f"exp:{exp['exp_id']}",
                             [{"op": "set_alloc", "cohort": exp["cohort_id"], "dim": exp["dim"], "dist": dist}],
                             summary=f"{exp['exp_id']}: stop harmful {exp['dim']} arm(s) {', '.join(harmful)}",
                             rationale=f"Always-valid test shows harm: {why}. Their traffic returns to the incumbent "
                                       f"{exp['control']}.", evidence={"experiment": exp["exp_id"], "analysis": snap},
                             estimate=est, urgent=True, derisk=True)
        plan["pending_drop"] = harmful
        self._save(exp["exp_id"], plan=plan)
        self._rec(minute, exp, "harm_detected", why, snap)

    # ------------------------------------------------------------ conclusion: segments, then one request
    def _segment_table(self, exp, arms: list[str]) -> dict:
        """{(feature, value): {"totals": {arm: [x, n, gx, gn]}, "strata": {(version, day): {arm: [...]}}}}.
        Totals gate the minimum sample; comparisons use the strata (see stats._mh)."""
        plan = json.loads(exp["plan"])
        versions = json.loads(exp["versions"])
        coh = plan["scope_cohorts"]
        col = DIM_COL[exp["dim"]]
        num, den = ("conn", "dials") if plan["kpi"] == "pickup_rate" else ("eng", "conn")
        gnum, gden = ("eng", "conn") if plan["kpi"] == "pickup_rate" else ("conn", "dials")
        rows = self.s.q(
            f"SELECT g.lang, g.city_tier, g.age_band, g.value_band, a.config_version ver, a.day day, a.{col} v, "
            f"SUM(a.{num}) x, SUM(a.{den}) n, SUM(a.{gnum}) gx, SUM(a.{gden}) gn FROM agg_seg a "
            f"JOIN segs g ON g.seg_id=a.seg_id "
            f"WHERE a.workflow_id=? AND a.day>=? AND a.config_version IN ({','.join('?' * len(versions))}) "
            f"AND a.cohort_id IN ({','.join('?' * len(coh))}) AND a.{col} IN ({','.join('?' * len(arms))}) "
            f"GROUP BY 1,2,3,4,5,6,7", (exp["workflow_id"], exp["live_minute"] // 1440, *versions, *coh, *arms))
        table = {}
        for r in rows:
            vals = (r["x"] or 0, r["n"] or 0, r["gx"] or 0, r["gn"] or 0)
            for f in S.SEGMENT_FEATURES:
                if r[f] is None:
                    continue
                seg = table.setdefault((f, r[f]), {"totals": {}, "strata": {}})
                for c in (seg["totals"].setdefault(r["v"], [0, 0, 0, 0]),
                          seg["strata"].setdefault((r["ver"], r["day"]), {}).setdefault(r["v"], [0, 0, 0, 0])):
                    for i, x in enumerate(vals):
                        c[i] += x
        return table

    def _segment_family(self, exp) -> int:
        """Bonferroni family fixed in advance, independent of the data: every single-feature segment value of this
        workflow x every challenger the experiment started with. Valid however many times segments are looked at."""
        r = self.s.one(
            "SELECT (SELECT COUNT(DISTINCT lang) FROM segs WHERE workflow_id=?) + "
            "(SELECT COUNT(DISTINCT city_tier) FROM segs WHERE workflow_id=?) + "
            "(SELECT COUNT(DISTINCT age_band) FROM segs WHERE workflow_id=?) + "
            "(SELECT COUNT(DISTINCT value_band) FROM segs WHERE workflow_id=?) AS n", (exp["workflow_id"],) * 4)
        return max(1, (r["n"] or 1) * max(1, len(json.loads(exp["arms"])) - 1))

    def _segments(self, exp, plan, winner: str | None, challengers: list[str]) -> list[dict]:
        """Segment analysis with always-valid sequences at alpha / family (Bonferroni over every segment x every
        original challenger), so it stays valid whether it runs at the interim, at conclusion, or both. A segment
        winner must also show guardrail non-inferiority inside the segment; one that wins on the primary KPI but
        cannot show it is recorded as 'insufficient guardrail evidence' and keeps the incumbent."""
        control = exp["control"]
        cands = [winner] if winner else challengers
        table = self._segment_table(exp, [control] + cands)
        usable = {k: d for k, d in table.items()
                  if control in d["totals"] and d["totals"][control][1] >= S.SEGMENT_MIN_ARM_N}
        m = self._segment_family(exp)
        alpha = S.SEGMENT_ALPHA / m
        margin = plan["guardrail_max_drop"]
        found, insufficient = [], []
        for (f, v), d in sorted(usable.items()):
            tot = d["totals"]
            for a in cands:
                if a not in tot or tot[a][1] < S.SEGMENT_MIN_ARM_N:
                    continue
                strata = d["strata"].values()
                pp = [(*st.get(a, [0, 0, 0, 0])[:2], *st.get(control, [0, 0, 0, 0])[:2]) for st in strata]
                gp = [(*st.get(a, [0, 0, 0, 0])[2:], *st.get(control, [0, 0, 0, 0])[2:]) for st in strata]
                c = msprt_strat(pp, alpha, plan["tau"])
                g = msprt_strat(gp, alpha, max(margin, 0.01))
                xt, nt, xc, nc = c.x_t, c.n_t, c.x_c, c.n_c
                if winner and c.worse:
                    found.append({"feature": f, "value": v, "variant": control, "against": a, "kind": "carve_out",
                                  "comparison": c.as_dict(), "gain": -c.delta * nt})
                elif not winner and c.better:
                    if g.cs_low > -margin:
                        found.append({"feature": f, "value": v, "variant": a, "kind": "segment_winner",
                                      "comparison": c.as_dict(), "guardrail": g.as_dict(), "gain": c.delta * nc,
                                      "counts": [xt, nt], "control_counts": [xc, nc]})
                    else:
                        insufficient.append({"segment": f"{f}={v}", "variant": a, "delta": round(c.delta, 4),
                                             "guardrail_lower_bound": round(g.cs_low, 4), "margin": margin})
        if insufficient:
            self._rec(self.s.get("last_minute", 0), exp, "segment_insufficient_guardrail",
                      f"{len(insufficient)} segment win(s) on {plan['kpi']} not promoted: guardrail non-inferiority "
                      f"not shown inside the segment ({insufficient[:3]})", insufficient)
        if not found:
            return []
        by_feature = {}
        for s in found:
            best = by_feature.setdefault(s["feature"], {})
            if s["value"] not in best or s["gain"] > best[s["value"]]["gain"]:
                best[s["value"]] = s
        feat = max(by_feature, key=lambda f: sum(s["gain"] for s in by_feature[f].values()))
        picked = sorted(by_feature[feat].values(), key=lambda s: -s["gain"])[:S.MAX_CARVED_COHORTS]
        for s in picked:
            s["alpha"] = alpha
            s["family"] = m
        return picked

    @atomic
    def _conclude(self, minute, exp, plan, comps, snap, winner, reason):
        wid, dim, control = exp["workflow_id"], exp["dim"], exp["control"]
        if winner:
            inter = self._interaction(exp, winner)
            bad = [i for i in inter if i["z"] <= -3]
            if bad:
                held = plan.setdefault("held", {})
                if minute - held.get(f"inter:{winner}", -10**9) >= 6 * 60:
                    held[f"inter:{winner}"] = minute
                    self._save(exp["exp_id"], plan=plan)
                    self._rec(minute, exp, "promotion_held", f"{winner} wins overall but hurts with {bad}", inter)
                return
        else:
            inter = []
        challengers = [a for a in comps]
        segs = self._segments(exp, plan, winner, challengers)
        if winner:
            dist = {winner: 1 - S.HOLDOUT_SHARE, control: S.HOLDOUT_SHARE}
        else:
            dist = {control: 1.0}
        dist = normalise(dist)
        ops = [{"op": "set_alloc", "cohort": exp["cohort_id"], "dim": dim, "dist": dist}]
        _, cfg = self.gov.current(wid)
        carved = self._carve_ops(minute, exp, segs, cfg, ops)
        _, _, recent, _ = self._comparisons(exp, since_hour=minute // 60 - S.RECENT_HOURS, mode="fixed")
        promising = [a for a, c in comps.items() if c["primary"].delta > 0 and c["primary"].cs_high >= S.FUTILITY_EFFECT
                     and not (a in recent and recent[a]["primary"].n_t >= 50 and recent[a]["primary"].delta < 0)]
        seg_winners = [c for c in carved if c["kind"] == "segment_winner"]
        est = allocation_change(self.s, wid, minute, dim, plan["scope_cohorts"],
                                effective_dist(cfg, exp["cohort_id"], dim), dist, plan["kpi"],
                                exploration=not winner)
        if seg_winners:
            est["gain_leads_per_day_low"] = round(est.get("gain_leads_per_day_low", 0) + sum(
                max(0.0, s["comparison"]["cs"][0]) * s["control_counts"][1] /
                max(1.0, (minute - exp["live_minute"]) / 1440) for s in seg_winners), 2)
        parts = []
        if winner:
            c, g = comps[winner]["primary"], comps[winner]["guardrail"]
            parts.append(f"{winner} beats {control} on {plan['kpi']}: {c.x_t}/{c.n_t}={c.x_t / c.n_t:.3f} vs "
                         f"{c.x_c}/{c.n_c}={c.x_c / c.n_c:.3f}, delta {c.delta:+.3f}, always-valid CS "
                         f"[{c.cs_low:+.3f}, {c.cs_high:+.3f}] at alpha {plan['alpha']:.3f}; still ahead over the last "
                         f"{S.RECENT_HOURS}h. Guardrail {plan['guardrail']} delta {g.delta:+.3f} (max drop "
                         f"{plan['guardrail_max_drop']}). Incumbent keeps {S.HOLDOUT_SHARE:.0%} as a holdout so decay "
                         "can be told apart from a global shift.")
        else:
            parts.append(f"{reason}; the default population returns to {control}.")
        parts += self._carve_text(carved, control)
        if inter:
            parts.append(f"Interaction check vs the other dimension: {inter}.")
        kind = "experiment_promote" if (winner or seg_winners) else "experiment_stop"
        summary = (f"{exp['exp_id']}: promote {dim} {winner} (was {control})" if winner else
                   f"{exp['exp_id']}: end {dim} test, back to {control}")
        if carved:
            summary += "; cohorts " + ", ".join(f"{c['cohort']}->{max(c['dist'], key=c['dist'].get)}" for c in carved)
        self.gov.file_intent(minute, wid, kind, f"exp:{exp['exp_id']}", ops, summary=summary,
                             rationale=" ".join(parts),
                             evidence={"experiment": exp["exp_id"], "analysis": snap, "segments": carved,
                                       "interaction": inter}, estimate=est, derisk=not (winner or seg_winners))
        conclusion = {"winner": winner, "dist": dist, "carved": carved, "reason": reason, "promising": promising}
        if winner:
            c = comps[winner]["primary"]
            conclusion.update({"winner_counts": [c.x_t, c.n_t], "control_counts": [c.x_c, c.n_c],
                               "lift": round(c.delta, 4), "cs": [round(c.cs_low, 4), round(c.cs_high, 4)]})
        plan["conclusion"] = conclusion
        if winner:
            plan["promotion"] = conclusion
        self._save(exp["exp_id"], status="promoting" if kind == "experiment_promote" else "stopping", plan=plan,
                   winner=winner, result={"reason": reason, "analysis": snap, "promising": promising})
        self._rec(minute, exp, "experiment_winner" if winner else "experiment_concluded",
                  reason + (f"; segment results: {[(c['cohort'], c['kind'], c['variant']) for c in carved]}"
                            if carved else ""), {"analysis": snap, "segments": carved})

    def _interaction(self, exp, winner: str) -> list[dict]:
        """Winner vs incumbent within each level of the other experimented dimension (factorial check)."""
        other = {"welcome_message": "voice", "voice": "welcome_message"}.get(exp["dim"])
        if not other:
            return []
        plan = json.loads(exp["plan"])
        versions = json.loads(exp["versions"])
        col, ocol = DIM_COL[exp["dim"]], DIM_COL[other]
        coh = plan["scope_cohorts"]
        rows = self.s.q(
            f"SELECT {ocol} o, {col} v, SUM(eng) x, SUM(conn) n FROM agg_hour WHERE workflow_id=? AND hour>=? "
            f"AND config_version IN ({','.join('?' * len(versions))}) AND cohort_id IN ({','.join('?' * len(coh))}) "
            f"AND {col} IN (?,?) GROUP BY {ocol}, {col}",
            (exp["workflow_id"], exp["live_minute"] // 60, *versions, *coh, winner, exp["control"]))
        by = {}
        for r in rows:
            by.setdefault(r["o"], {})[r["v"]] = (r["x"] or 0, r["n"] or 0)
        out = []
        for o, d in sorted(by.items()):
            if winner in d and exp["control"] in d and min(d[winner][1], d[exp["control"]][1]) >= S.SEGMENT_MIN_ARM_N:
                z = fixed_z(*d[winner], *d[exp["control"]])
                out.append({"other_dim": other, "level": o, "z": round(z, 2),
                            "delta": round(d[winner][0] / d[winner][1] - d[exp["control"]][0] / d[exp["control"]][1], 4)})
        return out

    # ------------------------------------------------------------ monitoring / decay
    @atomic
    def monitor(self, minute: int, exp):
        owner = f"exp:{exp['exp_id']}"
        if self.gov.open_intents(owner):
            return
        plan = json.loads(exp["plan"])
        promo = plan["promotion"]
        winner, control = promo["winner"], exp["control"]
        since = max(minute // 60 - S.DECAY_WINDOW_HOURS, promo.get("minute", minute) // 60)
        recent = self.counts(exp, plan["kpi"], since_hour=since)
        xr, nr = recent.get(winner, (0, 0))
        xh, nh = recent.get(control, (0, 0))
        xe, ne = promo["winner_counts"]
        mon = {"minute": minute, "winner_recent": [xr, nr], "holdout_recent": [xh, nh]}
        if nr < S.DECAY_MIN_N:
            self._save(exp["exp_id"], monitor=mon)
            return
        # A winner is promoted when its estimate is high, so its promoted lift is biased upward (winner's curse).
        # Decay is judged against a shrunk lift, so ordinary regression to the mean is not mistaken for decay.
        lift0 = shrunk_lift(promo)
        xc0, nc0 = promo["control_counts"]
        ref = min(0.99, xc0 / nc0 + lift0) if nc0 else xe / ne
        z_self = (xr / nr - ref) / math.sqrt(max(ref * (1 - ref), 1e-6) / nr + (xe / ne) * (1 - xe / ne) / max(ne, 1))
        lift_recent = (xr / nr - xh / nh) if nh else None
        harm = msprt(xr, nr, xh, nh, plan["alpha"], plan["tau"]) if nh else None
        cusum = self._cusum(exp, plan, promo)
        # both signals: the drift-adjusted CUSUM, and a real drop in the winner's own rate (a small holdout alone is
        # too noisy to roll back on)
        decayed = cusum >= S.DECAY_CUSUM_H and z_self <= -S.DECAY_SELF_Z and \
            (lift_recent is None or lift_recent < 0.5 * lift0)
        if exp["cohort_id"] != "default":
            decayed = False   # a carved cohort's holdout is too small to show lost lift; roll back on harm only
        harmful = harm is not None and harm.worse
        mon.update({"z_vs_experiment": round(z_self, 2), "lift_at_promotion": promo["lift"], "cusum": round(cusum, 2),
                    "lift_recent": round(lift_recent, 4) if lift_recent is not None else None})
        self._save(exp["exp_id"], monitor=mon)
        if not (decayed or harmful):
            return
        wid, dim = exp["workflow_id"], exp["dim"]
        _, cfg = self.gov.current(wid)
        dist = {control: 1.0}
        est = allocation_change(self.s, wid, minute, dim, plan["scope_cohorts"],
                                effective_dist(cfg, exp["cohort_id"], dim), dist, plan["kpi"])
        reason = (f"{winner} {plan['kpi']} last {S.DECAY_WINDOW_HOURS}h {xr}/{nr}={xr / nr:.3f} vs {xe / ne:.3f} when "
                  f"promoted (CUSUM {cusum:.1f} >= {S.DECAY_CUSUM_H}, z={z_self:.1f}); lift over holdout {control} now "
                  f"{'n/a' if lift_recent is None else f'{lift_recent:+.3f}'} vs {promo['lift']:+.3f} at promotion")
        if harmful:
            reason += f"; now significantly worse than the holdout (CS {harm.as_dict()['cs']})"
        self.gov.file_intent(minute, wid, "experiment_rollback", owner,
                             [{"op": "set_alloc", "cohort": exp["cohort_id"], "dim": dim, "dist": dist}],
                             summary=f"{exp['exp_id']}: roll back {dim} {winner} -> {control}"
                                     + (f" in {exp['cohort_id']}" if exp["cohort_id"] != "default" else "")
                                     + " (winner decayed)",
                             rationale=reason, evidence={"experiment": exp["exp_id"], "monitor": mon},
                             estimate=est, urgent=True, derisk=True)
        self._save(exp["exp_id"], status="rolling_back")
        self._rec(minute, exp, "decay_detected", reason, mon)

    def _cusum(self, exp, plan, promo) -> float:
        """Drift-adjusted Bernoulli CUSUM on the promoted variant, hour by hour since promotion, recomputed from
        aggregates (stateless, so restart-safe). Each hour, H1 ("the lift is gone") is the holdout's trailing-24h
        rate and H0 is that rate plus the lift we promoted on, so a population-wide drift moves both hypotheses
        together and does not look like decay. Each hour adds x*log(p1/p0) + (n-x)*log((1-p1)/(1-p0)), floored at 0."""
        xe, ne = promo["winner_counts"]
        xc, nc = promo["control_counts"]
        if not ne or not nc:
            return 0.0
        lift = shrunk_lift(promo)
        if lift <= 0:
            return 0.0
        versions = json.loads(exp["versions"])
        coh = plan["scope_cohorts"]
        col = DIM_COL[exp["dim"]]
        num, den = ("conn", "dials") if plan["kpi"] == "pickup_rate" else ("eng", "conn")
        rows = self.s.q(f"SELECT hour, {col} v, SUM({num}) x, SUM({den}) n FROM agg_hour WHERE workflow_id=? "
                        f"AND hour>=? AND {col} IN (?,?) AND config_version IN ({','.join('?' * len(versions))}) "
                        f"AND cohort_id IN ({','.join('?' * len(coh))}) GROUP BY hour, {col} ORDER BY hour",
                        (exp["workflow_id"], promo.get("minute", 0) // 60 - 24, promo["winner"], exp["control"],
                         *versions, *coh))
        win, hold = {}, {}
        for r in rows:
            (win if r["v"] == promo["winner"] else hold)[r["hour"]] = (r["x"] or 0, r["n"] or 0)
        s = 0.0
        start = promo.get("minute", 0) // 60
        for h in sorted(k for k in win if k >= start):
            hx = sum(v[0] for k, v in hold.items() if h - 24 <= k <= h)
            hn = sum(v[1] for k, v in hold.items() if h - 24 <= k <= h)
            # shrink the holdout's trailing rate toward the promotion-time control rate when it has little data
            p1 = (hx + 200 * xc / nc) / (hn + 200)
            p1 = min(max(p1, 0.01), 0.98)
            p0 = min(p1 + lift, 0.99)
            x, n = win[h]
            s = max(0.0, s + x * math.log(p1 / p0) + (n - x) * math.log((1 - p1) / (1 - p0)))
        return s

    @atomic
    def _retry_pending(self, minute: int, exp):
        """A promote/stop/rollback request came back (rejected, expired, stale). Re-decide on fresh data."""
        if self.gov.open_intents(f"exp:{exp['exp_id']}"):
            return
        plan = json.loads(exp["plan"])
        if plan.get("retry_after") and minute < plan["retry_after"]:
            return
        prev = {"promoting": "running", "stopping": "running", "rolling_back": "monitoring"}[exp["status"]]
        if exp["cohort_id"] != "default" and prev == "running":
            prev = "monitoring"
        self._save(exp["exp_id"], status=prev)
        exp = self._get(exp["exp_id"])
        if prev == "running":
            self.evaluate(minute, exp)
        else:
            self.monitor(minute, exp)

    # ------------------------------------------------------------ governance callbacks
    def on_applied(self, intent, minute: int, version: int):
        exp = self._get(intent["owner"].split(":", 1)[1])
        if not exp:
            return
        plan = json.loads(exp["plan"])
        versions = json.loads(exp["versions"])
        ops = json.loads(intent["ops"])
        dist = next(o["dist"] for o in ops if o["op"] == "set_alloc" and o["cohort"] == exp["cohort_id"])
        plan["expected_dist"] = normalise(dist)
        _, cfg = self.gov.current(exp["workflow_id"], refresh=True)
        plan["scope_cohorts"] = scope_of(cfg, exp["cohort_id"], exp["dim"]) or plan["scope_cohorts"]
        kind = intent["kind"]
        if kind == "experiment_start":
            self._save(exp["exp_id"], status="running", live_minute=minute, live_version=version, versions=[version],
                       plan=plan)
            self._rec(minute, exp, "experiment_live", f"live in v{version}", {"shares": plan["shares"]}, version)
        elif kind in ("experiment_drop_arm", "experiment_interim"):
            plan["dropped"] = sorted(set(plan.get("dropped", [])) | set(plan.pop("pending_drop", [])))
            if exp["cohort_id"] == "default" and plan["dropped"]:
                key = f"retired:{exp['workflow_id']}:{exp['dim']}"   # had a fair test; not re-tested later
                with self.s:
                    self.s.set(key, sorted(set(self.s.get(key, [])) | set(plan["dropped"])))
            if kind == "experiment_interim":
                plan["interim_done"] = True
                for c in plan.get("conclusion", {}).get("carved", []):
                    with self.s:
                        self.s.x("UPDATE cohorts SET status='live' WHERE cohort_id=? AND workflow_id=?",
                                 (c["cohort"], exp["workflow_id"]))
                    if c["kind"] == "segment_winner":
                        self._spawn_child(minute, exp, c, version, cfg)
            self._save(exp["exp_id"], versions=versions + [version], plan=plan)
            self._rec(minute, exp, "arm_dropped" if kind == "experiment_drop_arm" else "interim_applied",
                      f"dropped {plan['dropped']} in v{version}", None, version)
        elif kind == "experiment_promote":
            concl = plan.get("conclusion") or self._reconstruct_conclusion(minute, exp, plan, dist)
            for c in concl.get("carved", []):
                with self.s:
                    self.s.x("UPDATE cohorts SET status='live' WHERE cohort_id=? AND workflow_id=?",
                             (c["cohort"], exp["workflow_id"]))
                if c["kind"] == "segment_winner":
                    self._spawn_child(minute, exp, c, version, cfg)
            if concl.get("winner"):
                plan["promotion"]["minute"] = minute
                self._save(exp["exp_id"], status="monitoring", versions=versions + [version], plan=plan)
                self._rec(minute, exp, "experiment_promoted", f"{concl['winner']} live in v{version}", concl, version)
            else:
                self._save(exp["exp_id"], plan=plan)
                self._end(minute, self._get(exp["exp_id"]), "ended_no_winner",
                          f"no overall winner; default back on {exp['control']}, segment winners live in v{version}",
                          json.loads(exp["result"] or "{}"))
        elif kind == "experiment_stop":
            self._save(exp["exp_id"], plan=plan)
            self._end(minute, self._get(exp["exp_id"]), "ended_no_winner",
                      f"traffic back on {exp['control']} in v{version}", json.loads(exp["result"] or "{}"))
        elif kind == "experiment_rollback":
            if exp["cohort_id"] == "default":
                key = f"retired:{exp['workflow_id']}:{exp['dim']}"
                with self.s:
                    self.s.set(key, sorted(set(self.s.get(key, [])) | {plan["promotion"]["winner"]}))
            self._save(exp["exp_id"], plan=plan)
            self._end(minute, self._get(exp["exp_id"]), "rolled_back",
                      f"{plan['promotion']['winner']} retired after decay; {exp['control']} restored in v{version}",
                      json.loads(exp["monitor"] or "{}"), winner=plan["promotion"]["winner"])

    def _reconstruct_conclusion(self, minute: int, exp, plan: dict, dist: dict) -> dict:
        """A promotion whose conclusion was never saved (a store written before decisions became atomic). Rebuild
        it from what is live and the current data, so the callback completes instead of failing on every replay."""
        winner = max(dist, key=dist.get) if dist else None
        if winner == exp["control"]:
            winner = None
        concl = {"winner": winner, "carved": [], "reconstructed": True}
        if winner:
            _, _, comps, _ = self._comparisons(exp)
            c = comps.get(winner, {}).get("primary")
            if c:
                concl.update(winner_counts=[c.x_t, c.n_t], control_counts=[c.x_c, c.n_c], lift=round(c.delta, 4),
                             cs=[round(c.cs_low, 4), round(c.cs_high, 4)])
                plan["promotion"] = dict(concl)
            else:
                concl["winner"] = None
        plan["conclusion"] = concl
        self._rec(minute, exp, "conclusion_reconstructed",
                  f"promotion applied without a saved conclusion; rebuilt from live config (winner {concl['winner']})",
                  concl)
        return concl

    def _spawn_child(self, minute: int, parent, seg: dict, version: int, cfg: dict):
        """A segment winner becomes its own monitored promotion, scoped to its cohort."""
        n = self.s.next_seq("experiment")
        exp_id = f"exp-{n:03d}"
        pplan = json.loads(parent["plan"])
        promo = {"winner": seg["variant"], "dist": seg["dist"], "minute": minute, "winner_counts": seg["counts"],
                 "control_counts": seg["control_counts"], "lift": seg["comparison"]["delta"],
                 "cs": seg["comparison"]["cs"], "from": parent["exp_id"]}
        plan = {"kpi": pplan["kpi"], "guardrail": pplan["guardrail"], "guardrail_max_drop": pplan["guardrail_max_drop"],
                "alpha": pplan["alpha"], "tau": pplan["tau"], "scope_cohorts": [seg["cohort"]],
                "expected_dist": seg["dist"], "promotion": promo, "round": pplan.get("round", 1), "attempts": 0}
        with self.s:
            self.s.x("INSERT INTO experiments(exp_id,workflow_id,dim,cohort_id,kpi,control,arms,plan,status,"
                     "created_minute,live_minute,live_version,versions,winner) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (exp_id, parent["workflow_id"], parent["dim"], seg["cohort"], pplan["kpi"], parent["control"],
                      json.dumps([parent["control"], seg["variant"]]), json.dumps(plan), "monitoring", minute,
                      minute, version, json.dumps([version]), seg["variant"]))
        self._rec(minute, self._get(exp_id), "experiment_promoted",
                  f"segment winner {seg['variant']} live for cohort {seg['cohort']} in v{version} (from "
                  f"{parent['exp_id']})", promo, version)

    def on_returned(self, intent, minute: int, why: str, reasons: list):
        exp = self._get(intent["owner"].split(":", 1)[1])
        if not exp or exp["status"] not in ACTIVE:
            return
        plan = json.loads(exp["plan"])
        plan["attempts"] = plan.get("attempts", 0) + (0 if why == "stale" else 1)
        plan.pop("pending_drop", None)
        if why == "stale":
            plan["retry_after"] = minute
        elif why == "rejected":
            plan["retry_after"] = minute + S.RETRY_AFTER_REJECTION_MIN.get(intent["kind"], 240)
        else:  # expired / lapsed / failed: an expired request is a rejection; re-queue with fresh data
            plan["retry_after"] = minute + 60
        self._save(exp["exp_id"], plan=plan)
        self._rec(minute, exp, "experiment_request_returned",
                  f"{intent['kind']} {why}: {'; '.join(reasons)}; will re-decide on fresh data after "
                  f"{fmt(plan['retry_after'])}")
