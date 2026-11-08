"""Ingestion is exactly-once per chunk, even across a crash in the middle of a chunk."""
import json

import pytest

from controller.db import Store
from controller.ingest import Ingestor


def _call(i, minute, connected=True, engaged=False, flag=False):
    hh, mm = divmod(minute, 60)
    return {"type": "call", "ts": f"2027-01-04T{hh:02d}:{mm:02d}:00+05:30", "tenant_id": "t", "workflow_id": "wf",
            "campaign_id": "wf:c1", "lead_id": f"wf:l{i}", "person_id": f"wf:p{i}", "attempt_no": 1,
            "campaign_day_index": 0, "config_version": 1, "cohort_id": "default",
            "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
            "connected": connected, "engaged": engaged, "disposition": "x", "duration_s": 1, "turns": 1,
            "qa": {"hallucination_flag": flag, "flow_adherence": 0.9} if connected else None, "event_id": i}


class ChunkSim:
    def __init__(self):
        lead = {"type": "lead", "ts": "2027-01-04T08:00:00+05:30", "workflow_id": "wf", "campaign_id": "wf:c1",
                "lead_id": "wf:l1", "person_id": "wf:p1",
                "features": {"lang": "hi", "city_tier": "1", "age_band": "31-45", "value_band": "low"}, "event_id": 0}
        self.texts = {
            1: "\n".join(json.dumps(e) for e in [lead, _call(1, 600, True, True), _call(2, 610, False)]),
            2: "\n".join(json.dumps(e) for e in [_call(3, 700, True, False, True),
                                                 {"type": "config_changed", "ts": "2027-01-04T11:40:00+05:30",
                                                  "workflow_id": "wf", "version": 2, "actor": "supervisor",
                                                  "changed_dims": ["voice"], "event_id": 4}])}

    def chunks(self, kind=None, after=None):
        if kind == "history":
            return []
        return [{"chunk": n, "kind": "events", "events": 3, "last_event_id": n, "until_minute": 600 + n}
                for n in sorted(self.texts) if after is None or n > after]

    def chunk_text(self, n):
        return self.texts[n]


def totals(s):
    r = s.one("SELECT SUM(dials) d, SUM(conn) c, SUM(eng) e, SUM(flags) f FROM agg_hour")
    return (r["d"], r["c"], r["e"], r["f"])


def test_catch_up_twice_counts_once(tmp_path):
    s = Store(str(tmp_path / "c.db"))
    ing = Ingestor(s, ChunkSim())
    ing.catch_up()
    ing.catch_up()
    assert totals(s) == (3, 2, 1, 1)
    seg = s.one("SELECT COUNT(*) n FROM agg_seg WHERE seg_id >= 0")["n"]
    assert seg >= 1   # the call was joined to its lead's features


def test_crash_mid_chunk_replays_whole_chunk(tmp_path):
    s = Store(str(tmp_path / "c.db"))
    sim = ChunkSim()
    ing = Ingestor(s, sim)

    def boom(e):
        raise RuntimeError("killed mid-chunk")

    ing.listeners.append(boom)
    with pytest.raises(RuntimeError):
        ing.catch_up()
    assert totals(s) == (2, 1, 1, 0)      # chunk 1 committed, chunk 2 rolled back entirely
    ing2 = Ingestor(Store(str(tmp_path / "c.db")), sim)   # restart without the fault
    ing2.catch_up()
    assert totals(s) == (3, 2, 1, 1)
