"""A store must only be resumed against the world it was written in (regression from review)."""
import json

from controller.loop import open_store


class World:
    """Just the read-only endpoints open_store uses."""

    def __init__(self, history=None, minute=0, events_chunks=(), approvals=()):
        self.minute = minute
        self.history = history or [{"version": 1, "actor": "business_default", "proposal_id": None}]
        self.events_chunks = list(events_chunks)
        self.apps = list(approvals)

    def state(self):
        return {"scenario": "public_01_dev", "end_minute": 11520, "sim_minute": self.minute, "finished": False}

    def chunks(self, kind=None, after=None):
        if kind == "history":
            return [{"chunk": 0, "kind": "history", "events": 100, "last_event_id": 100}]
        return self.events_chunks

    def config_history(self, wid):
        return self.history

    def approvals(self, workflow_id=None, status=None):
        return self.apps


def first_run(path):
    """A run that bootstrapped and auto-applied v2 at D0 00:00, then died at minute 0."""
    w = World(history=[{"version": 1, "actor": "business_default", "proposal_id": None},
                       {"version": 2, "actor": "controller_auto", "proposal_id": "ctl-aaaa-wf-0001"}])
    s = open_store(path, w)
    with s:
        s.set("bootstrapped", True)
        s.set("last_minute", 0)
        s.set("workflows", {"wf": {}})
        s.x("INSERT INTO chunks VALUES(0,'history',100,100,0,0,0)")
        s.x("INSERT INTO config_versions(workflow_id,version,actor,proposal_id,applied_minute,config) "
            "VALUES('wf',2,'controller_auto','ctl-aaaa-wf-0001',0,?)", (json.dumps({}),))
    s.db.close()
    return w


def test_fresh_restart_of_the_same_scenario_does_not_inherit_old_state(tmp_path):
    path = str(tmp_path / "c.db")
    first_run(path)
    fresh = World()                                     # same scenario, same fingerprint, minute 0, only v1
    s = open_store(path, fresh)
    assert s.get("bootstrapped") is None                # a new store, not the old one
    assert not s.q("SELECT * FROM config_versions")
    assert list(tmp_path.glob("c.db.*.old"))            # the old store is archived, not deleted


def test_restart_in_the_same_world_resumes(tmp_path):
    path = str(tmp_path / "c.db")
    w = first_run(path)
    s = open_store(path, w)                             # the world still has v2 with our proposal_id
    assert s.get("bootstrapped") is True
    assert s.one("SELECT proposal_id FROM config_versions WHERE version=2")["proposal_id"] == "ctl-aaaa-wf-0001"


def test_ingested_chunk_missing_from_the_world_means_a_different_world(tmp_path):
    path = str(tmp_path / "c.db")
    w = World(events_chunks=[{"chunk": 1, "kind": "events", "events": 10, "last_event_id": 110}], minute=60)
    s = open_store(path, w)
    with s:
        s.x("INSERT INTO chunks VALUES(1,'events',10,110,60,0,0)")
        s.set("last_minute", 60)
    s.db.close()
    s = open_store(path, World(minute=60))              # same minute, but this world never produced chunk 1
    assert not s.q("SELECT * FROM chunks")
