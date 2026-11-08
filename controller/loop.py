"""The control loop. One iteration per simulator tick:

    ingest new chunks -> reconcile in-flight requests (apply what was approved) -> detect incidents ->
    evaluate experiments -> submit batched requests -> record loop metrics -> advance

Restart safety: everything above reads its state from the store. Killing the process between any two steps
loses at most the work of the current step, which is redone idempotently on restart (chunks are ingested
atomically; requests carry a proposal_id written down before sending).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time

from . import settings as S
from .api import ApiError, Sim
from .callerid import CallerIds
from .db import Store
from .experiments import Experiments
from .governance import Governance
from .incidents import Incidents, baselines
from .ingest import Ingestor
from .util import fmt

log = logging.getLogger("controller")


def _rss_mb() -> float | None:
    try:
        import resource  # Linux / macOS
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(r / 1024 if os.uname().sysname != "Darwin" else r / 1024 / 1024, 1)
    except Exception:  # Windows: best effort without extra dependencies
        try:
            import ctypes
            import ctypes.wintypes as wt

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t),
                            ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wt.HANDLE
            k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
            k32.K32GetProcessMemoryInfo.restype = wt.BOOL
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            if not k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return None
            return round(pmc.PeakWorkingSetSize / 1024 / 1024, 1)
        except Exception:
            return None


def continuity(s: Store, sim, st: dict) -> str | None:
    """None if this store's records are consistent with the world the simulator is serving now, else the first
    mismatch. A name-and-size fingerprint cannot tell two runs of the same scenario apart (a fresh restart sits at
    minute 0 exactly like an old store that crashed at minute 0), so the store is checked against the simulator's
    own records: every chunk we ingested must be listed with the same ids, every config version we recorded must
    be in the config history with the same actor and proposal_id, and every approval we hold must exist."""
    if s.get("last_minute", -1) > st["sim_minute"]:
        return f"store last saw minute {s.get('last_minute')}, the world is at {st['sim_minute']}"
    listed = {c["chunk"]: c for kind in ("history", "events") for c in sim.chunks(kind=kind)}
    for r in s.q("SELECT chunk, events, last_event_id FROM chunks"):
        c = listed.get(r["chunk"])
        if not c or c["events"] != r["events"] or c["last_event_id"] != r["last_event_id"]:
            return f"chunk {r['chunk']} in the store is not in this world"
    hist: dict[str, dict] = {}
    for r in s.q("SELECT workflow_id, version, actor, proposal_id FROM config_versions WHERE version > 1"):
        if r["workflow_id"] not in hist:
            hist[r["workflow_id"]] = {h["version"]: h for h in sim.config_history(r["workflow_id"])}
        h = hist[r["workflow_id"]].get(r["version"])
        if not h or (r["actor"] and h["actor"] != r["actor"]) or (r["proposal_id"] and h.get("proposal_id") !=
                                                                   r["proposal_id"]):
            return f"{r['workflow_id']} v{r['version']} ({r['actor']}) in the store is not in this world's history"
    ours = {r["approval_id"]: r["proposal_id"] for r in s.q("SELECT approval_id, proposal_id FROM proposals "
                                                            "WHERE approval_id IS NOT NULL")}
    if ours:
        theirs = {a["approval_id"]: a["proposal_id"] for a in sim.approvals()}
        for aid, pid in ours.items():
            if theirs.get(aid) != pid:
                return f"approval {aid} ({pid}) in the store is not in this world"
    return None


def open_store(db_path: str, sim: Sim) -> Store:
    """Resume if the DB belongs to this world; archive it and start fresh if it belongs to another one."""
    st = sim.state()
    hist = sim.chunks(kind="history")
    fp = {"scenario": st["scenario"], "end_minute": st["end_minute"],
          "history_last_event_id": max((c["last_event_id"] for c in hist), default=0)}
    if os.path.exists(db_path):
        s = Store(db_path)
        old = s.get("fingerprint")
        why = "different scenario fingerprint" if old != fp else continuity(s, sim, st)
        if why is None:
            log.info("resuming from %s (last seen %s, world at %s)", db_path, fmt(s.get("last_minute", -1)),
                     fmt(st["sim_minute"]))
            return s
        s.db.close()
        arch = f"{db_path}.{int(time.time())}.old"
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                shutil.move(db_path + suffix, arch + suffix)
        log.warning("store %s belongs to another world (%s); archived to %s", db_path, why, arch)
    s = Store(db_path)
    with s:
        s.set("fingerprint", fp)
        s.set("created_wall", time.time())
    return s


class Controller:
    def __init__(self, url: str, db_path: str):
        self.sim = Sim(url)
        self.s = open_store(db_path, self.sim)
        wfs = self.s.get("workflows")
        if not wfs:
            wfs = {w["workflow_id"]: w for w in self.sim.workflows()}
            with self.s:
                self.s.set("workflows", wfs)
                for w in wfs.values():
                    self.s.x("INSERT OR REPLACE INTO workflows VALUES(?,?,?)",
                             (w["workflow_id"], w["tenant_id"], json.dumps(w)))
        self.wfs = wfs
        self.gov = Governance(self.s, self.sim, wfs)
        self.ing = Ingestor(self.s, self.sim)
        self.ing.listeners.append(self.gov.on_event)
        self.inc = Incidents(self.s, self.sim, self.gov, wfs)
        self.cid = CallerIds(self.s, self.sim, self.gov, self.inc, wfs)
        self.exp = Experiments(self.s, self.sim, self.gov, self.inc, wfs)
        self._last_decisions = self._decision_count()
        self._day_wall = {}
        self._failing: dict[str, int] = {}

    def _decision_count(self) -> int:
        return self.s.one("SELECT COUNT(*) n FROM decisions")["n"]

    def bootstrap(self, minute: int):
        if self.s.get("bootstrapped"):
            return
        n = self.ing.catch_up()
        for wid in self.wfs:
            b = baselines(self.s, wid)
            self.gov.current(wid, refresh=True)
            with self.s:
                self.s.record(minute, wid, "baseline",
                              f"history baseline: pickup {b['pickup'][0] / max(b['pickup'][1], 1):.3f} on "
                              f"{b['pickup'][1]} dials, engagement {b['engagement'][0] / max(b['engagement'][1], 1):.3f}, "
                              f"hallucination flags {b['flag'][0] / max(b['flag'][1], 1):.4f}",
                              {k: v for k, v in b.items() if k != "pickup_by_hod"})
        with self.s:
            self.s.set("bootstrapped", True)
        log.info("bootstrapped from history: %d events", n)

    def step_minutes(self, minute: int, end: int) -> int:
        lo = min(min(w["dialling_window"][0], w["governance"]["approver_hours"][0]) for w in self.wfs.values())
        hi = max(max(w["dialling_window"][1], w["governance"]["approver_hours"][1] +
                     w["governance"]["apply_window_hours"]) for w in self.wfs.values())
        mod = minute % 1440
        if lo * 60 <= mod < hi * 60:
            step = S.TICK_ACTIVE
        else:
            to_lo = (lo * 60 - mod) % 1440
            step = max(S.TICK_ACTIVE, min(S.TICK_IDLE, to_lo))
        return max(S.TICK_ACTIVE, min(step, end - minute))

    def run(self):
        st = self.sim.state()
        self.bootstrap(st["sim_minute"])
        while True:
            minute = st["sim_minute"]
            t0 = time.perf_counter()
            events = self.ing.catch_up()      # not guarded: without data every later step is blind
            t1 = time.perf_counter()
            self._guard(minute, "reconcile", self.gov.reconcile, minute)
            self._guard(minute, "caller_id", self.cid.tick, minute)
            self._guard(minute, "incidents", self.inc.tick, minute)
            self._guard(minute, "experiments", self.exp.tick, minute)
            for wid in self.wfs:
                self._guard(minute, f"submit:{wid}", self.gov.submit, minute, wid)
            t2 = time.perf_counter()
            with self.s:
                self.s.set("last_minute", minute)
            if st["finished"]:
                self._metrics(minute, events, t1 - t0, t2 - t1, 0.0, st)
                self._finish(minute)
                return
            step = self.step_minutes(minute, st["end_minute"])
            try:
                self.sim.advance(step, expect_from=minute)
            except ApiError as e:
                if e.error == "finished":
                    st = self.sim.state()
                    continue
                raise
            t3 = time.perf_counter()
            self._metrics(minute, events, t1 - t0, t2 - t1, t3 - t2, st)
            st = self.sim.state()

    def _guard(self, minute: int, name: str, fn, *args):
        """A bug in one module must not stop the loop: the failed step changed nothing (every step is one
        transaction), it pages, and the next tick tries again. Repeated failures stay one open alert."""
        try:
            fn(*args)
            if self._failing.pop(name, None):
                with self.s:
                    self.s.resolve_alert(f"step_failed:{name}", minute)
        except ApiError as e:
            log.warning("%s: simulator error %s; retrying next tick", name, e)
        except Exception as e:  # noqa: BLE001 - deliberate: isolate faults per step
            log.exception("%s failed at %s", name, fmt(minute))
            self._failing[name] = self._failing.get(name, 0) + 1
            if self.s.db.in_transaction:
                self.s.db.execute("ROLLBACK")
                self.s._depth = 0
            with self.s:
                self.s.alert(f"step_failed:{name}", minute, "page", None,
                             f"controller step {name} failing ({self._failing[name]}x): {type(e).__name__}: {e}")

    def _metrics(self, minute, events, ingest_s, decide_s, advance_s, st):
        n = self._decision_count()
        pend = self.s.one("SELECT COUNT(*) n FROM proposals WHERE status IN ('prepared','pending','approved')")["n"]
        credits = st.get("credits_remaining")
        day = minute // 1440
        self._day_wall.setdefault(day, 0.0)
        self._day_wall[day] += ingest_s + decide_s + advance_s
        with self.s:
            self.s.x("INSERT OR REPLACE INTO metrics VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (minute, time.time(), events, round(ingest_s, 4), round(decide_s, 4), round(advance_s, 4),
                      credits, pend, n - self._last_decisions, _rss_mb(), self.sim.errors))
            if credits is not None and credits < S.CREDIT_RESERVE:
                self.s.alert("credits_low", minute, "page", None,
                             f"credits {credits} below reserve {S.CREDIT_RESERVE}: incident response at risk")
            if self._day_wall[day] > S.WALL_SECONDS_PER_SIM_DAY_BUDGET:
                self.s.alert(f"slow_day:{day}", minute, "warn", None,
                             f"D{day} took {self._day_wall[day]:.0f}s wall clock (> {S.WALL_SECONDS_PER_SIM_DAY_BUDGET}s)")
        self._last_decisions = n
        if minute % 1440 == 0 or events > 0 and minute % 240 == 0:
            log.info("%s events=%d ingest=%.2fs decide=%.2fs credits=%s in-flight=%s", fmt(minute), events, ingest_s,
                     decide_s, credits, self.gov.describe(minute))

    def _finish(self, minute: int):
        with self.s:
            self.s.record(minute, None, "run_finished", f"scenario finished at {fmt(minute)}",
                          {"decisions": self._decision_count(), "api_calls": self.sim.calls,
                           "api_errors": self.sim.errors})
        log.info("finished at %s; %d decisions recorded in %s", fmt(minute), self._decision_count(), self.s.path)
