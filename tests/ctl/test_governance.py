"""Governance lifecycle against a fake simulator: the paths a crash or a supervisor can force us down."""
import json

import pytest

from controller.api import ApiError
from controller.db import Store
from controller.governance import Governance

from .fakes import FakeSim, LossySim, Recorder, workflow

VOICE = [{"op": "set_alloc", "cohort": "default", "dim": "voice", "dist": {"V1": 0.5, "V2": 0.5}}]
CID = [{"op": "set_alloc", "cohort": "default", "dim": "caller_id", "dist": {"D1": 0.3, "D2": 0.7}}]


@pytest.fixture
def env(tmp_path):
    sim = FakeSim()
    wfs = {"wf": workflow()}
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, wfs)
    rec = Recorder()
    g.callbacks["t"] = rec
    return s, sim, g, rec, wfs, tmp_path


def tick(g, minute):
    g.reconcile(minute)
    g.submit(minute, "wf")


def test_approval_lifecycle_applies_once(env):
    s, sim, g, rec, *_ = env
    iid = g.file_intent(600, "wf", "k", "t:1", VOICE, summary="voice test", rationale="r", estimate={"affected_leads": 5})
    assert s.one("SELECT path FROM intents WHERE intent_id=?", (iid,))["path"] == "approval"  # customer-facing
    tick(g, 600)
    assert sim.posts == 1 and len(sim.apps) == 1
    sim.decide("apr_00001", "approved")
    tick(g, 615)
    tick(g, 630)
    assert sim.version == 2 and rec.applied == [(iid, 2)]
    assert s.one("SELECT status FROM proposals")["status"] == "applied"


def test_customer_facing_never_auto_but_derisk_may(env):
    s, sim, g, rec, wfs, _ = env
    wfs["wf"]["governance"]["auto_apply"]["allowed_dims"] = ["voice", "caller_id"]
    a = g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    b = g.file_intent(600, "wf", "k", "t:2", CID, summary="s", rationale="r")
    assert s.one("SELECT path FROM intents WHERE intent_id=?", (a,))["path"] == "approval"
    assert s.one("SELECT path FROM intents WHERE intent_id=?", (b,))["path"] == "auto"


def test_northwind_rollback_still_needs_a_named_human(tmp_path):
    """Even where the runtime policy would let a de-risking voice rollback auto-apply, Northwind's rule (a named
    human approval for anything a customer hears) wins."""
    sim = FakeSim()
    wf = workflow(allowed=("voice", "caller_id"), max_shift=1.0)
    wf["tenant_id"] = "northwind_life"
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": wf})
    g.callbacks["t"] = Recorder()
    iid = g.file_intent(600, "wf", "experiment_rollback", "t:1", VOICE, summary="roll back voice", rationale="r",
                        derisk=True, urgent=True)
    assert s.one("SELECT path FROM intents WHERE intent_id=?", (iid,))["path"] == "approval"
    g.submit(600, "wf")
    assert sim.puts == 0 and sim.posts == 1                      # one approval request, zero auto-applies
    wf["tenant_id"] = "other_tenant"                              # without the tenant rule, de-risking may auto-apply
    iid2 = g.file_intent(615, "wf", "experiment_rollback", "t:2", VOICE, summary="s", rationale="r", derisk=True)
    assert s.one("SELECT path FROM intents WHERE intent_id=?", (iid2,))["path"] == "auto"


def test_shift_beyond_policy_goes_to_a_human(env):
    s, sim, g, *_ = env
    big = [{"op": "set_alloc", "cohort": "default", "dim": "caller_id", "dist": {"D3": 1.0}}]
    iid = g.file_intent(600, "wf", "k", "t:1", big, summary="s", rationale="r")
    assert s.one("SELECT path FROM intents WHERE intent_id=?", (iid,))["path"] == "approval"


def test_crash_between_prepare_and_send_does_not_duplicate(env):
    s, sim, g, rec, wfs, tmp = env
    g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    p = g._prepare(600, "wf", "approval", g._ready("wf", "approval"), False)   # written down, then "crash"
    assert p["status"] == "prepared" and sim.posts == 0
    g2 = Governance(Store(str(tmp / "c.db")), sim, wfs)                        # restart
    g2.callbacks["t"] = rec
    g2.reconcile(615)
    assert sim.posts == 1 and len(sim.apps) == 1
    # and if the request had landed before the crash, reconciliation adopts it instead of re-sending
    g2.file_intent(615, "wf", "k", "t:2", CID, summary="s", rationale="r")
    p2 = g2._prepare(615, "wf", "approval", g2._ready("wf", "auto"), False)
    sim.propose(g2._wire_body(p2))
    g3 = Governance(Store(str(tmp / "c.db")), sim, wfs)
    g3.reconcile(630)
    assert len(sim.apps) == 2
    assert s.one("SELECT status FROM proposals WHERE proposal_id=?", (p2["proposal_id"],))["status"] == "pending"


def test_supervisor_edit_makes_approval_stale_and_returns_intent(env):
    s, sim, g, rec, *_ = env
    iid = g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    tick(g, 600)
    cfg = sim.config("wf")["config"]
    cfg["prompt_version"] = "P3"
    sim.supervisor_edit(cfg)
    sim.decide("apr_00001", "approved")
    tick(g, 615)
    assert rec.returned == [(iid, "stale")]
    assert sim.cfg["prompt_version"] == "P3"   # never overwritten


def test_quota_keeps_a_slot_for_incidents(env):
    s, sim, g, rec, wfs, _ = env
    wfs["wf"]["governance"]["max_requests_per_24h"] = 2
    assert g.can_request("wf", 600, urgent=False)[0]
    g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    tick(g, 600)
    sim.decide("apr_00001", "rejected", ["approver declined (judgement call)"])
    tick(g, 615)
    ok, why = g.can_request("wf", 630, urgent=False)
    assert not ok and "quota" in why
    assert g.can_request("wf", 630, urgent=True)[0]


def test_outside_approver_hours_nothing_is_sent(env):
    s, sim, g, *_ = env
    g.file_intent(20 * 60, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    tick(g, 20 * 60)
    assert sim.posts == 0
    assert not g.can_request("wf", 9 * 60, urgent=True)[0]


def test_already_applied_is_reconciled_not_reapplied(env):
    s, sim, g, rec, wfs, tmp = env
    iid = g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    tick(g, 600)
    sim.decide("apr_00001", "approved")
    sim.apply("wf", "apr_00001")                 # applied, but we crashed before recording it
    g2 = Governance(Store(str(tmp / "c.db")), sim, wfs)
    g2.callbacks["t"] = rec
    g2.reconcile(630)
    g2.reconcile(645)
    assert sim.version == 2 and rec.applied == [(iid, 2)]


def test_auto_apply_crash_is_reconciled_from_config_history(env):
    s, sim, g, rec, wfs, tmp = env
    iid = g.file_intent(600, "wf", "k", "t:1", CID, summary="s", rationale="r")
    p = g._prepare(600, "wf", "auto", g._ready("wf", "auto"), False)
    sim.auto_apply("wf", {"proposal_id": p["proposal_id"], "base_version": 1,
                          "proposed_config": g._wire_body(p)["proposed_config"]})   # landed, then crash
    g2 = Governance(Store(str(tmp / "c.db")), sim, wfs)
    g2.callbacks["t"] = rec
    g2.reconcile(615)
    assert sim.puts == 1 and sim.version == 2 and rec.applied == [(iid, 2)]


class Exploding(Recorder):
    """An owner whose transition writes to the store and then dies, as a crash between the two would."""

    def __init__(self, store, fail_times=1):
        super().__init__()
        self.s, self.left = store, fail_times

    def on_applied(self, intent, minute, version):
        self.s.set("owner_state", "running")
        if self.left:
            self.left -= 1
            raise RuntimeError("killed between recording the apply and the owner's transition")
        super().on_applied(intent, minute, version)


def test_apply_and_owner_transition_are_atomic_and_replayed(env):
    s, sim, g, rec, wfs, tmp = env
    owner = Exploding(s)
    g.callbacks["t"] = owner
    iid = g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    tick(g, 600)
    sim.decide("apr_00001", "approved")
    g.reconcile(615)                      # apply lands in the simulator, the owner's transition blows up
    assert sim.version == 2
    assert s.one("SELECT status FROM proposals")["status"] == "approved"       # nothing half-recorded
    assert s.one("SELECT status FROM intents WHERE intent_id=?", (iid,))["status"] == "in_proposal"
    assert s.get("owner_state") is None                                         # owner's write rolled back too
    g.reconcile(630)                      # replay: simulator says applied -> record + transition, once
    assert s.one("SELECT status FROM proposals")["status"] == "applied"
    assert owner.applied == [(iid, 2)] and s.get("owner_state") == "running"
    assert sim.version == 2               # never applied twice


def test_lost_approval_response_is_reconciled_not_failed(tmp_path):
    sim = LossySim()
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": workflow()})
    g.callbacks["t"] = Recorder()
    g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    g.submit(600, "wf")
    assert len(sim.apps) == 1                                                   # it landed
    assert s.one("SELECT status FROM proposals")["status"] == "prepared"        # we don't know: not 'failed'
    g.reconcile(615)
    p = s.one("SELECT status, approval_id FROM proposals")
    assert p["status"] == "pending" and p["approval_id"] == "apr_00001"
    assert len(sim.apps) == 1                                                   # adopted, not re-sent


def test_lost_auto_apply_response_is_reconciled_from_history(tmp_path):
    sim = LossySim()
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": workflow()})
    rec = Recorder()
    g.callbacks["t"] = rec
    iid = g.file_intent(600, "wf", "k", "t:1", CID, summary="s", rationale="r")
    g.submit(600, "wf")
    assert sim.version == 2 and s.one("SELECT status FROM proposals")["status"] == "prepared"
    g.reconcile(615)
    assert s.one("SELECT status FROM proposals")["status"] == "applied" and rec.applied == [(iid, 2)]
    assert sim.puts == 1


class UnreachableSim(FakeSim):
    """Every request fails before reaching the simulator: nothing lands, the client sees a transport error."""

    def propose(self, body):
        self.posts += 1
        raise ApiError(0, "transport", "connection refused")


def test_uncertain_requests_back_off_then_dead_letter_then_replay(tmp_path):
    from controller import settings as S
    from controller.governance import replay
    sim = UnreachableSim()
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": workflow()})
    g.callbacks["t"] = Recorder()
    iid = g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    g.submit(600, "wf")
    sends_at = [600]
    for minute in range(615, 1400, 15):          # looked up every tick, re-sent only on its backoff slot
        before = sim.posts
        g.reconcile(minute)
        if sim.posts > before:
            sends_at.append(minute)
    assert len(sends_at) == S.REQUEST_MAX_ATTEMPTS
    gaps = [b - a for a, b in zip(sends_at, sends_at[1:])]
    assert gaps == [15, 30, 60, 120, 120, 120, 120]
    p = s.one("SELECT * FROM proposals")
    assert p["status"] == "dead"
    assert s.one("SELECT severity FROM alerts WHERE alert_key=?", (f"dead:{p['proposal_id']}",))["severity"] == "page"
    assert s.one("SELECT status FROM intents WHERE intent_id=?", (iid,))["status"] == "in_proposal"   # parked
    g.reconcile(1500)
    assert sim.posts == S.REQUEST_MAX_ATTEMPTS                                  # dead means no more sends
    # the operator fixes the network and replays: same proposal_id, fresh attempts, it goes out once
    sim.__class__ = FakeSim
    assert "back to prepared" in replay(s, p["proposal_id"], 1500)
    g.reconcile(1515)
    p = s.one("SELECT * FROM proposals")
    assert p["status"] == "pending" and p["approval_id"] == "apr_00001" and len(sim.apps) == 1


def test_operator_can_cancel_a_dead_request(tmp_path):
    from controller import settings as S
    from controller.governance import replay
    sim = UnreachableSim()
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": workflow()})
    rec = Recorder()
    g.callbacks["t"] = rec
    iid = g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    g.submit(600, "wf")
    for minute in range(615, 1400, 15):
        g.reconcile(minute)
    pid = s.one("SELECT proposal_id FROM proposals")["proposal_id"]
    assert sim.posts == S.REQUEST_MAX_ATTEMPTS
    assert "cancel requested" in replay(s, pid, 1500, cancel=True)
    assert s.one("SELECT resolved_minute FROM alerts WHERE alert_key=?", (f"dead:{pid}",))["resolved_minute"] == 1500
    assert s.one("SELECT status FROM intents WHERE intent_id=?", (iid,))["status"] == "in_proposal"  # not yet
    g.reconcile(1515)                       # the lookup confirms it never landed: now it is cancelled
    assert s.one("SELECT status FROM proposals")["status"] == "cancelled"
    assert s.one("SELECT status FROM intents WHERE intent_id=?", (iid,))["status"] == "returned"
    assert rec.returned == [(iid, "cancelled")]                                 # the owner heard back
    assert sim.posts == S.REQUEST_MAX_ATTEMPTS


class LandsOnLastSendSim(FakeSim):
    """Sends fail before reaching the simulator, except the last one before dead-lettering: it lands, but its
    response is lost."""

    def propose(self, body):
        from controller import settings as S
        if self.posts + 1 < S.REQUEST_MAX_ATTEMPTS:
            self.posts += 1
            raise ApiError(0, "transport", "connection refused")
        super().propose(body)
        raise ApiError(0, "transport", "connection reset after send")


def _dead_letter(tmp_path, sim):
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": workflow()})
    rec = Recorder()
    g.callbacks["t"] = rec
    iid = g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    g.submit(600, "wf")
    for minute in range(615, 1400, 15):
        g.reconcile(minute)
        if s.one("SELECT status FROM proposals")["status"] == "dead":
            break                                       # stop at the tick it was dead-lettered
    return s, g, rec, iid, s.one("SELECT * FROM proposals")


def test_a_dead_request_whose_last_send_landed_is_still_found_and_applied(tmp_path):
    """Regression (review): dead-lettering used to stop reconciliation, so an approval created by the final send
    (response lost) was never tracked or applied. Dead now means no more sends, never no more lookups."""
    from controller import settings as S
    sim = LandsOnLastSendSim()
    s, g, rec, iid, p = _dead_letter(tmp_path, sim)
    assert p["status"] == "dead" and len(sim.apps) == 1                        # it landed; we could not know
    g.reconcile(1410)                                                           # the next lookup finds it
    p = s.one("SELECT * FROM proposals")
    assert p["status"] == "pending" and p["approval_id"] == "apr_00001"
    assert s.one("SELECT resolved_minute FROM alerts WHERE alert_key=?", (f"dead:{p['proposal_id']}",))[
        "resolved_minute"] == 1410
    assert s.q("SELECT * FROM decisions WHERE kind='request_landed'")
    sim.minute = 1420
    sim.decide("apr_00001", "approved")
    g.reconcile(1425)
    g.reconcile(1440)
    assert s.one("SELECT status FROM proposals")["status"] == "applied" and rec.applied == [(iid, 2)]
    assert sim.posts == S.REQUEST_MAX_ATTEMPTS and len(sim.apps) == 1          # adopted, never re-sent


def test_cancelling_a_dead_request_that_landed_adopts_it_instead(tmp_path):
    """An operator's cancel cannot orphan a live approval: the loop checks the simulator before cancelling."""
    from controller.governance import replay
    sim = LandsOnLastSendSim()
    s, g, rec, iid, p = _dead_letter(tmp_path, sim)
    assert p["status"] == "dead"
    replay(s, p["proposal_id"], 1405, cancel=True)      # the operator acts before the next lookup
    g.reconcile(1410)
    assert s.one("SELECT status FROM proposals")["status"] == "pending"
    assert s.one("SELECT status FROM intents WHERE intent_id=?", (iid,))["status"] == "in_proposal"
    assert not rec.returned
    assert "cancel is moot" in s.one("SELECT summary FROM decisions WHERE kind='request_landed'")["summary"]


def test_definite_refusal_still_fails_and_returns(env):
    s, sim, g, rec, *_ = env

    def refuse(body):
        raise ApiError(422, "invalid_config", ["bad"])

    sim.propose = refuse
    iid = g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    g.submit(600, "wf")
    assert s.one("SELECT status FROM proposals")["status"] == "failed"
    assert rec.returned == [(iid, "failed")]


def test_batching_merges_compatible_intents_into_one_request(env):
    s, sim, g, *_ = env
    g.file_intent(600, "wf", "k", "t:1", VOICE, summary="voice", rationale="r")
    g.file_intent(600, "wf", "k", "t:2", [{"op": "set_alloc", "cohort": "default", "dim": "welcome_message",
                                         "dist": {"W1": 0.5, "W2": 0.5}}], summary="welcome", rationale="r")
    tick(g, 600)
    assert sim.posts == 1
    diff = json.loads(s.one("SELECT body FROM proposals")["body"])["diff"]
    assert set(diff) == {"allocations.default.voice", "allocations.default.welcome_message"}
