"""Leases: one controller process per world and per workflow; takeover only from the dead or the silent."""
import subprocess
import sys
import time

import pytest

from controller.db import Store
from controller.governance import Governance
from controller.leases import LeaseLost, Leases

from .fakes import FakeSim, Recorder, workflow

VOICE = [{"op": "set_alloc", "cohort": "default", "dim": "voice", "dist": {"V1": 0.5, "V2": 0.5}}]


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "c.db"))


def test_a_second_live_controller_is_kept_out(store):
    a, b = Leases(store, owner="A"), Leases(store, owner="B")
    assert a.acquire(["world", "workflow:wf"]) == {}
    blocked = b.acquire(["world", "workflow:wf"])
    assert blocked == {"world": "A", "workflow:wf": "A"}
    with pytest.raises(LeaseLost):
        b.acquire_or_wait(["world"], wait_s=0.2, poll_s=0.05)


def test_a_killed_controller_is_replaced_at_once(store):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with store:
        store.x("INSERT INTO leases VALUES('world','old-owner',?,?,?)", (Leases(store).host, dead.pid, time.time()))
    b = Leases(store, owner="B")
    assert b.acquire(["world"]) == {}            # fresh heartbeat, but its pid is gone: no waiting out the TTL
    assert b.holds("world")


def test_a_silent_holder_loses_its_lease_after_the_ttl(store):
    a = Leases(store, ttl_s=0.05, owner="A")
    a.acquire(["workflow:wf"])
    time.sleep(0.1)
    b = Leases(store, ttl_s=0.05, owner="B")
    assert b.acquire(["workflow:wf"]) == {}
    assert a.renew() == ["workflow:wf"]          # A finds out on its next renewal and must stop
    assert not a.holds("workflow:wf")


def test_governance_will_not_write_without_the_workflow_lease(store):
    sim = FakeSim()
    g = Governance(store, sim, {"wf": workflow()})
    g.callbacks["t"] = Recorder()
    mine, other = Leases(store, owner="me"), Leases(store, owner="other")
    other.acquire(["workflow:wf"])
    g.leases = mine
    g.file_intent(600, "wf", "k", "t:1", VOICE, summary="s", rationale="r")
    with pytest.raises(LeaseLost):
        g.submit(600, "wf")
    assert sim.posts == 0                        # fenced: nothing reached the simulator
