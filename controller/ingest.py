"""Event ingestion (C1) and lead/feature joins (C2).

Each chunk is applied in one transaction together with its checkpoint row, so a crash replays the whole
chunk or none of it: aggregates are never double-counted. Chunk numbers are global and increasing.

We keep aggregates, not raw calls: hourly by (cohort, config version, variant) for detection and
experiments, daily by person segment for "who does this work for". That is what makes the load scenario
(~850k events/day) fit in a few seconds of CPU per simulated day.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict

from .db import Store
from .util import minute_of

log = logging.getLogger("controller.ingest")

LEAD_RETENTION_DAYS = 6   # cadence is at most 3 campaign days; keep a margin for min-gap spill-over

try:  # optional speed-up; the controller works without it
    import orjson as _fast_json

    def _loads(s):
        return _fast_json.loads(s)
except ImportError:  # pragma: no cover
    _loads = json.loads


class Ingestor:
    def __init__(self, store: Store, sim):
        self.s, self.sim = store, sim
        self.segs: dict[tuple, int] = {}
        self.leads: dict[str, int] = {}
        self.lead_day = -10**9
        self.listeners = []          # callables(event) for config_changed / approval_decided
        self._load_caches()

    def _load_caches(self):
        for r in self.s.q("SELECT * FROM segs"):
            self.segs[(r["workflow_id"], r["lang"], r["city_tier"], r["age_band"], r["value_band"])] = r["seg_id"]
        last = self.s.get("last_event_minute")
        if last is not None:
            day = last // 1440
            for r in self.s.q("SELECT lead_id, seg_id FROM leads WHERE day >= ?", (day - LEAD_RETENTION_DAYS,)):
                self.leads[r["lead_id"]] = r["seg_id"]
            self.lead_day = day

    def last_chunk(self) -> int:
        r = self.s.one("SELECT MAX(chunk) AS m FROM chunks")
        return r["m"] if r and r["m"] is not None else -1

    # ------------------------------------------------------------ public
    def catch_up(self) -> int:
        """Ingest every chunk after our checkpoint. Returns number of events ingested."""
        total = 0
        last = self.last_chunk()
        # The listing defaults to live events; history chunks must be asked for by kind (they precede D0).
        pending = [] if self.s.get("history_done") else self.sim.chunks(kind="history")
        pending += self.sim.chunks(kind="events", after=last if last >= 0 else None)
        for ch in sorted(pending, key=lambda c: c["chunk"]):
            if ch["chunk"] <= self.last_chunk():
                continue
            total += self.ingest_chunk(ch)
        if not self.s.get("history_done"):
            with self.s:
                self.s.set("history_done", True)
        return total

    def ingest_chunk(self, ch: dict) -> int:
        t0 = time.perf_counter()
        text = self.sim.chunk_text(ch["chunk"])
        agg_h: dict[tuple, list] = defaultdict(lambda: [0, 0, 0, 0, 0])
        agg_s: dict[tuple, list] = defaultdict(lambda: [0, 0, 0])
        daily: dict[tuple, list] = defaultdict(lambda: [0, 0, 0, 0, 0])
        new_leads, new_segs, campaigns, specials = [], [], [], []
        last_minute = None
        segs, leads = self.segs, self.leads
        n = 0
        for line in text.splitlines():
            if not line:
                continue
            e = _loads(line)
            n += 1
            t = e["type"]
            if t == "call":
                m = minute_of(e["ts"])
                last_minute = m
                wf = e["workflow_id"]
                v = e["variant"]
                c, g = e["connected"], e["engaged"]
                qa = e.get("qa") or None
                fl = 1 if (qa and qa.get("hallucination_flag")) else 0
                hour, day = m // 60, m // 1440
                a = agg_h[(wf, hour, e["cohort_id"], e["config_version"], v["welcome_message"], v["voice"],
                           v["caller_id"], v["prompt_version"])]
                a[0] += 1
                a[1] += c
                a[2] += g
                a[3] += fl
                a[4] += 1 if qa else 0
                seg = leads.get(e["lead_id"])
                if seg is None:
                    r = self.s.one("SELECT seg_id FROM leads WHERE lead_id=?", (e["lead_id"],))
                    seg = r["seg_id"] if r else -1
                    leads[e["lead_id"]] = seg
                b = agg_s[(wf, day, seg, e["cohort_id"], e["config_version"], v["welcome_message"], v["voice"],
                           v["caller_id"])]
                b[0] += 1
                b[1] += c
                b[2] += g
                d = daily[(wf, day)]
                d[1] += 1
                d[2] += c
                d[3] += g
                if e["attempt_no"] == 1:
                    d[4] += 1
            elif t == "lead":
                wf = e["workflow_id"]
                f = e["features"]
                key = (wf, f.get("lang"), f.get("city_tier"), f.get("age_band"), f.get("value_band"))
                sid = segs.get(key)
                if sid is None:
                    sid = self._new_seg(key)
                    new_segs.append(key)
                day = minute_of(e["ts"]) // 1440
                leads[e["lead_id"]] = sid
                new_leads.append((e["lead_id"], sid, day))
            elif t == "campaign_created":
                m = minute_of(e["ts"])
                campaigns.append((e["campaign_id"], e["workflow_id"], m, e["leads"], e["config_version_at_creation"]))
                daily[(e["workflow_id"], m // 1440)][0] += e["leads"]
            else:
                specials.append(e)

        with self.s:
            s = self.s
            if new_leads:
                s.db.executemany("INSERT OR IGNORE INTO leads(lead_id,seg_id,day) VALUES(?,?,?)", new_leads)
            if campaigns:
                s.db.executemany("INSERT OR IGNORE INTO campaigns VALUES(?,?,?,?,?)", campaigns)
            if agg_h:
                s.db.executemany(
                    "INSERT INTO agg_hour VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                    "dials=dials+excluded.dials, conn=conn+excluded.conn, eng=eng+excluded.eng, "
                    "flags=flags+excluded.flags, qa_n=qa_n+excluded.qa_n",
                    [(*k, *v) for k, v in agg_h.items()])
            if agg_s:
                s.db.executemany(
                    "INSERT INTO agg_seg VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                    "dials=dials+excluded.dials, conn=conn+excluded.conn, eng=eng+excluded.eng",
                    [(*k, *v) for k, v in agg_s.items()])
            if daily:
                s.db.executemany(
                    "INSERT INTO daily VALUES(?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                    "uploaded=uploaded+excluded.uploaded, dials=dials+excluded.dials, conn=conn+excluded.conn, "
                    "eng=eng+excluded.eng, leads_dialled=leads_dialled+excluded.leads_dialled",
                    [(*k, *v) for k, v in daily.items()])
            for e in specials:
                for fn in self.listeners:
                    fn(e)
            if last_minute is not None:
                s.set("last_event_minute", max(last_minute, s.get("last_event_minute", last_minute)))
            s.x("INSERT INTO chunks VALUES(?,?,?,?,?,?,?)",
                (ch["chunk"], ch["kind"], ch["events"], ch["last_event_id"], ch["until_minute"], time.time(),
                 time.perf_counter() - t0))
        self._evict(ch["until_minute"])
        return n

    def _new_seg(self, key) -> int:
        with self.s:
            self.s.x("INSERT OR IGNORE INTO segs(workflow_id,lang,city_tier,age_band,value_band) VALUES(?,?,?,?,?)",
                     key)
            r = self.s.one("SELECT seg_id FROM segs WHERE workflow_id=? AND lang IS ? AND city_tier IS ? "
                           "AND age_band IS ? AND value_band IS ?", key)
        self.segs[key] = r["seg_id"]
        return r["seg_id"]

    def _evict(self, until_minute: int):
        day = until_minute // 1440
        if day <= self.lead_day:
            return
        self.lead_day = day
        cutoff = day - LEAD_RETENTION_DAYS
        if len(self.leads) > 50_000:
            keep = {r["lead_id"] for r in self.s.q("SELECT lead_id FROM leads WHERE day >= ?", (cutoff,))}
            self.leads = {k: v for k, v in self.leads.items() if k in keep}
