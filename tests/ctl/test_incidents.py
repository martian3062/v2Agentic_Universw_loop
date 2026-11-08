"""Hallucination incident lifecycle and credit policy (regressions from review)."""
import json

import pytest

from controller import settings as S
from controller.api import ApiError
from controller.db import Store
from controller.governance import Governance
from controller.incidents import Incidents

from .fakes import FakeSim, workflow


def agg(s, hour, prompt, conn, flags, eng=None, dials=None, wm="W1"):
    dials = dials or conn * 4
    eng = conn // 2 if eng is None else eng
    with s:
        s.x("INSERT INTO agg_hour VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET dials=dials+excluded.dials,"
            " conn=conn+excluded.conn, eng=eng+excluded.eng, flags=flags+excluded.flags, qa_n=qa_n+excluded.qa_n",
            ("wf", hour, "default", 1, wm, "V1", "D1", prompt, dials, conn, eng, flags, conn))


@pytest.fixture
def env(tmp_path):
    sim = FakeSim()
    sim.pass_rates = {"P3": 0.99, "P1": 0.99, "P4": 0.95}
    sim.credits = 200.0
    wf = workflow()
    wf["options"]["prompt_version"] = ["P1", "P2", "P3", "P4"]
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": wf})
    inc = Incidents(s, sim, g, {"wf": wf})
    with s:
        s.set("baseline:wf", {"pickup_by_hod": {}, "pickup": [250, 1000], "engagement": [500, 1000],
                              "flag": [14, 1000]})
    return s, sim, g, inc


def set_prompt(sim, s, g, prompt, actor):
    cfg = sim.config("wf")["config"]
    cfg["prompt_version"] = prompt
    sim._set(cfg, actor)
    with s:
        s.x("INSERT OR REPLACE INTO config_versions(workflow_id,version,actor,applied_minute) VALUES(?,?,?,?)",
            ("wf", sim.version, actor, sim.minute))
    g.invalidate("wf")


def open_incident(s, inc, data_over=None, status="verifying"):
    data = {"prompt": "P2", "flags": 30, "connected": 400, "rate": 0.075, "baseline_rate": 0.014, "z": 8.0,
            "credits_spent": 4, "tried": [], "validated": {"P3": {"validation_run_id": "v1", "n": 400,
                                                                  "pass_rate": 0.99}}}
    data.update(data_over or {})
    with s:
        s.x("INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?)",
            ("inc-0001", "wf", "hallucination", "P2", status, 0, None, json.dumps(data)))
    return s.one("SELECT * FROM incidents WHERE incident_id='inc-0001'")


def test_failed_fix_is_not_resolved_as_someone_elses_change(env):
    s, sim, g, inc = env
    set_prompt(sim, s, g, "P3", "controller_approved")          # our fix P3 went live at minute 600
    for h in range(10, 14):
        agg(s, h, "P3", conn=100, flags=8)                        # ...and still hallucinates: 8% vs 1.4%
    for h in range(-50, -40):
        agg(s, h, "P1", conn=100, flags=1)                        # P1: known good in production
    row = open_incident(s, inc, {"chosen": "P3", "applied_minute": 600})
    sim.minute = 14 * 60
    inc.progress_hallucination(sim.minute, row)                   # verification fails
    row = s.one("SELECT * FROM incidents WHERE incident_id='inc-0001'")
    assert row["status"] == "retry" and json.loads(row["data"])["tried"] == ["P3"]
    inc.progress_hallucination(sim.minute, row)                   # the old code resolved here ("someone else")
    row = s.one("SELECT * FROM incidents WHERE incident_id='inc-0001'")
    assert row["status"] == "proposed", "a bad prompt we applied must get a replacement, not a resolution"
    intent = s.one("SELECT * FROM intents WHERE owner='inc:inc-0001' AND status='ready'")
    assert json.loads(intent["ops"]) == [{"op": "set_prompt", "value": "P1"}]
    assert intent["summary"].startswith("switch prompt P3 -> P1")


def test_inconclusive_engagement_drop_keeps_verifying(env):
    s, sim, g, inc = env
    set_prompt(sim, s, g, "P3", "controller_approved")
    for h in range(10, 14):
        agg(s, h, "P3", conn=80, flags=1, eng=36)                 # flags fine; engagement 45% vs 50% on 320 calls
    row = open_incident(s, inc, {"chosen": "P3", "applied_minute": 600})
    sim.minute = 14 * 60
    inc.progress_hallucination(sim.minute, row)
    row = s.one("SELECT * FROM incidents WHERE incident_id='inc-0001'")
    assert row["status"] == "verifying" and row["resolved_minute"] is None
    assert s.one("SELECT 1 FROM decisions WHERE kind='fix_inconclusive'")


def test_a_noisy_drop_beyond_the_margin_is_not_verified_yet(env):
    """3 pp below baseline on 350 calls is not significant (z ~ -1.1), but it is beyond the 2 pp margin, so the
    fix stays under verification instead of being declared verified on too little data."""
    s, sim, g, inc = env
    set_prompt(sim, s, g, "P3", "controller_approved")
    agg(s, 10, "P3", conn=350, flags=3, eng=164)                  # 46.9% vs 50%: drop 3.1 pp
    row = open_incident(s, inc, {"chosen": "P3", "applied_minute": 600})
    sim.minute = 14 * 60
    inc.progress_hallucination(sim.minute, row)
    row = s.one("SELECT * FROM incidents WHERE incident_id='inc-0001'")
    assert row["status"] == "verifying" and row["resolved_minute"] is None


def test_verification_uses_the_workflows_own_engagement_margin(tmp_path):
    """A pickup-primary workflow allowing only a 0.5 pp engagement drop: a statistically clear 1.5 pp drop must
    not be 'verified' (the old code compared against a hard-coded 2 pp)."""
    sim = FakeSim()
    wf = workflow()
    wf["objective"] = {"primary": "pickup_rate", "guardrail": {"engagement_rate_max_drop": 0.005}}
    wf["options"]["prompt_version"] = ["P1", "P2", "P3"]
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": wf})
    inc = Incidents(s, sim, g, {"wf": wf})
    with s:
        s.set("baseline:wf", {"pickup_by_hod": {}, "pickup": [25000, 100000], "engagement": [50000, 100000],
                              "flag": [1400, 100000]})
    set_prompt(sim, s, g, "P3", "controller_approved")
    agg(s, 10, "P3", conn=80000, flags=800, eng=38800)              # flags fine; engagement 48.5% vs 50.0%
    row = open_incident(s, inc, {"chosen": "P3", "applied_minute": 600})
    sim.minute = 14 * 60
    inc.progress_hallucination(sim.minute, row)
    row = s.one("SELECT * FROM incidents WHERE incident_id='inc-0001'")
    assert row["status"] == "retry" and row["resolved_minute"] is None
    regressed = s.one("SELECT summary FROM decisions WHERE kind='fix_regressed'")
    assert regressed and "0.5% margin" in regressed["summary"]


def test_someone_elses_new_prompt_is_verified_not_trusted(env):
    s, sim, g, inc = env
    row = open_incident(s, inc, status="open")
    set_prompt(sim, s, g, "P4", "supervisor")                     # a supervisor swaps in P4 mid-incident
    inc.progress_hallucination(sim.minute, row)
    row = s.one("SELECT * FROM incidents WHERE incident_id='inc-0001'")
    d = json.loads(row["data"])
    assert row["status"] == "verifying" and d["chosen"] == "P4" and d["chosen_by"] == "supervisor"
    assert row["resolved_minute"] is None


def test_returned_fix_never_abandons_the_incident(env):
    s, sim, g, inc = env
    row = open_incident(s, inc, {"chosen": "P3"}, status="proposed")
    intent = {"owner": "inc:inc-0001", "kind": "prompt_fix", "intent_id": "x"}
    for i in range(5):
        inc.on_returned(intent, 600 + i, "rejected", ["approver declined (judgement call)"])
    row = s.one("SELECT * FROM incidents WHERE incident_id='inc-0001'")
    assert row["status"] == "retry"
    assert s.one("SELECT severity FROM alerts WHERE alert_key='incident_stuck:inc-0001'")["severity"] == "page"


def test_credit_reserve_is_enforced(env):
    s, sim, g, inc = env
    sim.credits = S.CREDIT_RESERVE + 2.0
    data = {"credits_spent": 0, "validated": {}}
    assert inc.may_validate(data, 4)                   # an incident's first validation may dip into the reserve
    data["charges"] = [{"kind": "validation", "status": "lost", "cost": 4}]
    assert not inc.may_validate(data, 4)               # any further spend may not (a lost one counts as charged)
    sim.credits = S.CREDIT_RESERVE + 10.0
    assert inc.may_validate(data, 4)                   # above the reserve: fine
    data["credits_spent"] = S.HAL_CREDIT_CEILING - 2
    assert not inc.may_validate(data, 4)               # the per-incident ceiling always holds


class LostValidation(FakeSim):
    """Validation consumes credits, then the response is lost."""

    def validate(self, wid, prompt_version, n):
        super().validate(wid, prompt_version, n)
        raise ApiError(0, "transport", "connection reset after send")


class UnsentValidation(FakeSim):
    """Validation fails before reaching the simulator: nothing is charged."""

    def validate(self, wid, prompt_version, n):
        raise ApiError(0, "transport", "connection refused")


def _incident_with_known_good(s, inc):
    for h in range(-50, -40):
        agg(s, h, "P1", conn=100, flags=1)       # P1 known good -> one candidate to validate
    return open_incident(s, inc, {"validated": {}, "credits_spent": 0}, status="open")


def test_lost_validation_responses_stay_inside_the_ceiling(tmp_path):
    sim = LostValidation()
    sim.credits = 200.0
    wf = workflow()
    wf["options"]["prompt_version"] = ["P1", "P2", "P3"]
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": wf})
    inc = Incidents(s, sim, g, {"wf": wf})
    with s:
        s.set("baseline:wf", {"pickup_by_hod": {}, "pickup": [250, 1000], "engagement": [500, 1000],
                              "flag": [14, 1000]})
    _incident_with_known_good(s, inc)
    for tick in range(8):                        # eight ticks, every response lost
        data = json.loads(s.one("SELECT data FROM incidents WHERE incident_id='inc-0001'")["data"])
        inc._choose_fix(600 + 15 * tick, "wf", "inc-0001", data)
    data = json.loads(s.one("SELECT data FROM incidents WHERE incident_id='inc-0001'")["data"])
    consumed = 200.0 - sim.credits
    assert consumed <= S.HAL_CREDIT_CEILING                        # was 32 against a 25 ceiling
    assert data["credits_spent"] == consumed                       # recorded spending matches what was charged
    assert all(c["status"] == "lost" for c in data["charges"])


def test_unsent_validation_is_refunded(tmp_path):
    sim = UnsentValidation()
    sim.credits = 200.0
    wf = workflow()
    wf["options"]["prompt_version"] = ["P1", "P2", "P3"]
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": wf})
    inc = Incidents(s, sim, g, {"wf": wf})
    with s:
        s.set("baseline:wf", {"pickup_by_hod": {}, "pickup": [250, 1000], "engagement": [500, 1000],
                              "flag": [14, 1000]})
    _incident_with_known_good(s, inc)
    data = json.loads(s.one("SELECT data FROM incidents WHERE incident_id='inc-0001'")["data"])
    inc._choose_fix(600, "wf", "inc-0001", data)
    data = json.loads(s.one("SELECT data FROM incidents WHERE incident_id='inc-0001'")["data"])
    assert sim.credits == 200.0 and data["credits_spent"] == 0 and data["charges"][0]["status"] == "refunded"


def test_llm_suggestions_are_not_bought_below_the_reserve(env):
    s, sim, g, inc = env
    sim.credits = S.CREDIT_RESERVE + 2.0              # suggest-fixes costs 3
    row = open_incident(s, inc, {"validated": {}}, status="open")
    assert inc._choose_fix(600, "wf", "inc-0001", json.loads(row["data"])) is None
    assert sim.credits == S.CREDIT_RESERVE + 2.0      # no suggestion bought below the reserve...
    assert s.one("SELECT severity FROM alerts WHERE alert_key='no_fix:inc-0001'")["severity"] == "page"  # ...a human is
