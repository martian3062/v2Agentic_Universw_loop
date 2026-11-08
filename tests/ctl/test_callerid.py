"""Caller-ID detection compares numbers within cohort-and-hour strata (regression from review)."""
import pytest

from controller.callerid import CallerIds
from controller.db import Store
from controller.governance import Governance
from controller.incidents import Incidents

from .fakes import FakeSim, workflow


@pytest.fixture
def env(tmp_path):
    sim = FakeSim()
    wf = workflow()
    s = Store(str(tmp_path / "c.db"))
    g = Governance(s, sim, {"wf": wf})
    inc = Incidents(s, sim, g, {"wf": wf})
    cid = CallerIds(s, sim, g, inc, {"wf": wf})
    with s:
        s.set("baseline:wf", {"pickup_by_hod": {}, "pickup": [250, 1000], "engagement": [500, 1000],
                              "flag": [14, 1000]})
    return s, cid


def put(s, hour, num, dials, conn):
    with s:
        s.x("INSERT INTO agg_hour VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("wf", hour, "default", 1, "W1", "V1", num, "P1", dials, conn, conn // 2, 0, conn))


def test_same_pickup_per_hour_different_hourly_mix_is_not_a_bad_number(env):
    """Both numbers pick up 40% in hour 10 and 10% in hour 15. D1 happens to dial mostly in the bad hour.
    Pooled over hours D1 looks terrible (13% vs 37%: the reviewer measured z = -37); within each hour it is
    identical. No incident must open."""
    s, cid = env
    put(s, 10, "D1", 100, 40)
    put(s, 10, "D2", 900, 360)
    put(s, 15, "D1", 900, 90)
    put(s, 15, "D2", 100, 10)
    rows = s.q("SELECT cid, cohort_id, hour, SUM(dials) d, SUM(conn) c FROM agg_hour GROUP BY cid, cohort_id, hour")
    a = cid._assess(rows, "D1", {"pickup_by_hod": {}, "pickup": [250, 1000]}, 0)
    assert abs(a["z"]) < 1 and abs(a["rel"] - 1) < 0.01
    cid.detect(16 * 60, "wf")
    assert not s.q("SELECT * FROM incidents")


def test_a_number_that_is_worse_within_each_hour_is_caught(env):
    s, cid = env
    put(s, 10, "D1", 400, 40)        # 10% vs 40% in the same hour
    put(s, 10, "D2", 400, 160)
    put(s, 15, "D1", 400, 12)        # 3% vs 12%
    put(s, 15, "D2", 400, 48)
    cid.detect(16 * 60, "wf")
    inc = s.one("SELECT * FROM incidents")
    assert inc and inc["key"] == "D1"
