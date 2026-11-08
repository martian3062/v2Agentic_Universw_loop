"""End-to-end smoke test: the fully wired controller against the real simulator, in-process (no server, no port).

Unit tests exercise modules one at a time; this catches wiring breaks between them (a method lost from a class,
a callback not registered, a lease not taken) that only show when everything runs together. Two simulated days
of public_01 take a few seconds.
"""
import pytest
from fastapi.testclient import TestClient

import controller.api as api
from controller.loop import Controller

DAYS = 2


class _Stop(Exception):
    pass


@pytest.fixture
def wired(tmp_path, monkeypatch):
    from sim import server
    server.init_world("scenarios/public_01_dev.yaml", str(tmp_path / "run"))
    client = TestClient(server.app, base_url="http://sim")
    monkeypatch.setattr(api.httpx, "Client", lambda base_url, timeout: client)
    return Controller("http://sim", str(tmp_path / "c.db"))


def test_controller_runs_end_to_end_without_a_failing_step(wired):
    ctrl = wired
    real_advance = ctrl.sim.advance

    def advance(minutes, expect_from):
        if expect_from >= DAYS * 1440:
            raise _Stop
        return real_advance(minutes, expect_from)

    ctrl.sim.advance = advance
    with pytest.raises(_Stop):
        ctrl.run()
    s = ctrl.s
    failing = s.q("SELECT * FROM alerts WHERE alert_key LIKE 'step_failed:%' OR alert_key LIKE 'reconcile_failed:%'")
    assert not failing, [dict(r) for r in failing]
    assert s.get("bootstrapped") is True
    assert s.one("SELECT COUNT(*) n FROM chunks")["n"] > 1                     # history and live chunks ingested
    assert s.one("SELECT COUNT(*) n FROM decisions WHERE kind='requested'")["n"] >= 1   # governance sent requests
    assert s.one("SELECT COUNT(*) n FROM decisions WHERE kind='applied'")["n"] >= 1     # and applied changes
    assert s.one("SELECT COUNT(*) n FROM decisions WHERE kind='experiment_live'")["n"] >= 1
    held = {r["name"] for r in s.q("SELECT name FROM leases WHERE owner=?", (ctrl.leases.owner,))}
    assert "world" in held and {f"workflow:{w}" for w in ctrl.wfs} <= held  # this process owns what it acts on
