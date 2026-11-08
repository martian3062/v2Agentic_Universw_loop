"""Winner rule: a primary win is not enough; guardrail non-inferiority must be shown (regression from review)."""
import json

import pytest

from controller.db import Store
from controller.experiments import Experiments
from controller.governance import Governance
from controller.incidents import Incidents

from .fakes import FakeSim, workflow


def seed(s, rows, version=1):
    with s:
        for hour, wm, dials, conn, eng in rows:
            s.x("INSERT INTO agg_hour VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("wf", hour, "default", version, wm, "V1", "D1", "P1", dials, conn, eng, 0, conn))


@pytest.fixture
def env(tmp_path):
    sim = FakeSim()
    wf = workflow(allowed=())
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": wf})
    inc = Incidents(s, sim, g, {"wf": wf})
    exp = Experiments(s, sim, g, inc, {"wf": wf})
    plan = {"shares": {"W1": 0.5, "W2": 0.5}, "alpha": 0.05, "tau": 0.05, "min_arm_n": 300, "kpi": "engagement_rate",
            "guardrail": "pickup_rate", "guardrail_max_drop": 0.02, "scope_cohorts": ["default"],
            "expected_dist": {"W1": 0.5, "W2": 0.5}, "attempts": 0, "interim_days": 4}
    cfg = sim.config("wf")["config"]
    cfg["allocations"]["default"]["welcome_message"] = {"W1": 0.5, "W2": 0.5}
    sim.cfg = cfg
    with s:
        s.x("INSERT INTO experiments(exp_id,workflow_id,dim,cohort_id,kpi,control,arms,plan,status,created_minute,"
            "live_minute,live_version,versions) VALUES('exp-001','wf','welcome_message','default','engagement_rate',"
            "'W1','[\"W1\",\"W2\"]',?,'running',0,0,1,'[1]')", (json.dumps(plan),))
    return s, sim, g, exp


def run(s, exp, minute=600):
    exp.evaluate(minute, s.one("SELECT * FROM experiments WHERE exp_id='exp-001'"))
    return s.one("SELECT * FROM experiments WHERE exp_id='exp-001'")


def test_primary_win_without_guardrail_evidence_is_held(env):
    s, sim, g, exp = env
    # engagement 0.5 -> 0.7 on 500 connected each: a clear primary win. Pickup identical but on only 1,000 dials:
    # the guardrail interval is about +-9 pp, far wider than the 2 pp margin.
    seed(s, [(8, "W1", 1000, 500, 250), (8, "W2", 1000, 500, 350)])
    row = run(s, exp)
    assert row["status"] == "running"                                   # not promoted
    held = s.one("SELECT summary FROM decisions WHERE kind='promotion_held'")
    assert held and "non-inferiority is not shown" in held["summary"]
    assert not s.q("SELECT * FROM intents WHERE kind='experiment_promote'")


def test_allocation_change_with_a_time_trend_is_not_a_winner(env):
    """Both arms engage 50% under v1 (50/50 split), then both 90% under v2 (the interim moved the split to 20/80).
    Pooled, W2 has more of its traffic in the good period and 'wins' by ~13 pp; within each version they are
    identical. No promotion may be filed."""
    s, sim, g, exp = env
    with s:
        s.x("UPDATE experiments SET versions='[1,2]'")
    seed(s, [(2, "W1", 20000, 10000, 5000), (2, "W2", 20000, 10000, 5000)], version=1)
    seed(s, [(9, "W1", 8000, 4000, 3600), (9, "W2", 32000, 16000, 14400)], version=2)
    row = run(s, exp, minute=600)
    assert row["status"] != "promoting"            # (with this much data it correctly concludes futility instead)
    assert not s.q("SELECT * FROM intents WHERE kind='experiment_promote'")
    d = json.loads(row["last_eval"])["arms"]["W2"]["primary"]["delta"]
    assert abs(d) < 0.005, f"stratified difference should be ~0, got {d}"


def test_crash_between_intent_and_conclusion_leaves_neither(env, monkeypatch):
    s, sim, g, exp = env
    seed(s, [(8, "W1", 60000, 30000, 15000), (8, "W2", 60000, 30000, 21000)])
    real_save = exp._save

    def dying_save(exp_id, **cols):
        if cols.get("status") == "promoting":
            raise RuntimeError("killed after filing the promotion, before saving its conclusion")
        return real_save(exp_id, **cols)

    monkeypatch.setattr(exp, "_save", dying_save)
    with pytest.raises(RuntimeError):
        run(s, exp)
    assert not s.q("SELECT * FROM intents")                       # the intent rolled back with the state
    assert s.one("SELECT status FROM experiments")["status"] == "running"
    monkeypatch.setattr(exp, "_save", real_save)
    assert run(s, exp)["status"] == "promoting"                   # next tick decides again, consistently
    assert "conclusion" in json.loads(s.one("SELECT plan FROM experiments")["plan"])


def test_promotion_applied_without_saved_conclusion_is_reconstructed(env):
    """A store written by older code: the intent exists, the conclusion does not. The callback must complete."""
    s, sim, g, exp = env
    seed(s, [(8, "W1", 60000, 30000, 15000), (8, "W2", 60000, 30000, 21000)])
    iid = g.file_intent(600, "wf", "experiment_promote", "exp:exp-001",
                        [{"op": "set_alloc", "cohort": "default", "dim": "welcome_message",
                          "dist": {"W1": 0.1, "W2": 0.9}}], summary="promote W2", rationale="r")
    intent = s.one("SELECT * FROM intents WHERE intent_id=?", (iid,))
    exp.on_applied(intent, 700, 2)
    row = s.one("SELECT * FROM experiments")
    assert row["status"] == "monitoring" and json.loads(row["plan"])["promotion"]["winner"] == "W2"
    assert s.one("SELECT 1 FROM decisions WHERE kind='conclusion_reconstructed'")


def test_same_cohort_name_in_two_workflows_is_two_cohorts(tmp_path):
    s = Store(str(tmp_path / "c.db"))
    with s:
        for wid in ("wf_a", "wf_b"):
            s.x("INSERT OR IGNORE INTO cohorts VALUES(?,?,?,?,?,?)",
                ("lang_ta", wid, '{"lang": ["ta"]}', "exp:x", 0, "proposed"))
    assert {r["workflow_id"] for r in s.q("SELECT workflow_id FROM cohorts WHERE cohort_id='lang_ta'")} == \
        {"wf_a", "wf_b"}


def test_old_cohort_table_is_migrated(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE cohorts (cohort_id TEXT PRIMARY KEY, workflow_id TEXT, where_json TEXT, origin TEXT, "
                "created_minute INTEGER, status TEXT)")
    old.execute("INSERT INTO cohorts VALUES('lang_ta','wf_a','{}','x',0,'live')")
    old.commit()
    old.close()
    s = Store(path)
    with s:
        s.x("INSERT OR IGNORE INTO cohorts VALUES('lang_ta','wf_b','{}','x',0,'proposed')")
    assert len(s.q("SELECT * FROM cohorts")) == 2


def test_primary_win_with_guardrail_evidence_is_promoted(env):
    s, sim, g, exp = env
    # same engagement lift, pickup identical on 60,000 dials per arm: guardrail lower bound about -0.9 pp
    seed(s, [(8, "W1", 60000, 30000, 15000), (8, "W2", 60000, 30000, 21000)])
    row = run(s, exp)
    assert row["status"] == "promoting"
    assert s.one("SELECT * FROM intents WHERE kind='experiment_promote'")
