"""Leases: at most one controller process acts on a world, and on each workflow, at a time.

Every controller process has an owner id (host, pid, random suffix). Before it acts it must hold:
  - `world`: the right to advance simulated time (there is one clock, so one advancer);
  - `workflow:<id>`: the right to change that workflow's config.
A lease is a row with a heartbeat. The holder renews every tick. Another process may take a lease over when
  - the heartbeat is older than LEASE_TTL_S (the holder stopped renewing), or
  - the holder ran on this host and its pid is no longer alive (a killed controller is replaced at once,
    without waiting out the TTL).
Fencing: governance checks the workflow lease immediately before every write to the simulator, so a process
that was paused, lost its lease and woke up can never send a change. A process that finds a lease lost on
renewal stops acting (fail-stop) rather than risk two writers.

Today one process takes every lease. The same table is what shards would use to split workflows between
processes once the store is shared (see SUBMISSION.md section 8).
"""
from __future__ import annotations

import logging
import os
import secrets
import socket
import time

from . import settings as S
from .db import Store

log = logging.getLogger("controller.leases")


class LeaseLost(RuntimeError):
    """This process no longer holds a lease it needs; it must not act."""


def pid_alive(pid: int) -> bool:
    """True if a process with this pid exists on this host (best effort, no extra dependencies)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5           # access denied: it exists
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259                      # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Leases:
    def __init__(self, store: Store, ttl_s: float | None = None, owner: str | None = None):
        self.s = store
        self.ttl = S.LEASE_TTL_S if ttl_s is None else ttl_s
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self.owner = owner or f"{self.host}:{self.pid}:{secrets.token_hex(3)}"
        self.held: set[str] = set()

    # ------------------------------------------------------------ claim
    def _free(self, row, now: float) -> bool:
        if row is None or row["owner"] == self.owner:
            return True
        if now - row["heartbeat"] > self.ttl:
            return True
        return row["host"] == self.host and row["pid"] != self.pid and not pid_alive(row["pid"])

    def acquire(self, names: list[str]) -> dict[str, str]:
        """Take every free lease in `names`. Returns {name: holder} for the ones another live process holds."""
        blocked = {}
        now = time.time()
        with self.s:
            for name in names:
                row = self.s.one("SELECT * FROM leases WHERE name=?", (name,))
                if self._free(row, now):
                    self.s.x("INSERT INTO leases(name, owner, host, pid, heartbeat) VALUES(?,?,?,?,?) "
                             "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, host=excluded.host, "
                             "pid=excluded.pid, heartbeat=excluded.heartbeat",
                             (name, self.owner, self.host, self.pid, now))
                    if row is not None and row["owner"] != self.owner:
                        log.warning("took over lease %s from %s", name, row["owner"])
                    self.held.add(name)
                else:
                    blocked[name] = row["owner"]
        return blocked

    def acquire_or_wait(self, names: list[str], wait_s: float | None = None, poll_s: float = 2.0) -> None:
        """Block until every lease is ours, or give up after `wait_s` (a live controller already owns them)."""
        deadline = time.time() + (S.LEASE_WAIT_S if wait_s is None else wait_s)
        while True:
            blocked = self.acquire(names)
            if not blocked:
                return
            if time.time() >= deadline:
                raise LeaseLost("another controller is running against this store and world: "
                                + ", ".join(f"{n} held by {h}" for n, h in sorted(blocked.items())))
            log.warning("waiting for %d lease(s) held by another controller: %s", len(blocked),
                        sorted(set(blocked.values())))
            time.sleep(poll_s)

    # ------------------------------------------------------------ keep
    def renew(self) -> list[str]:
        """Refresh our heartbeats. Returns the leases we have lost (another process took them over)."""
        lost = []
        now = time.time()
        with self.s:
            for name in sorted(self.held):
                cur = self.s.x("UPDATE leases SET heartbeat=? WHERE name=? AND owner=?", (now, name, self.owner))
                if cur.rowcount == 0:
                    lost.append(name)
        self.held -= set(lost)
        return lost

    def holds(self, name: str) -> bool:
        """Fencing check, read from the store (not from memory) right before a write."""
        row = self.s.one("SELECT owner, heartbeat FROM leases WHERE name=?", (name,))
        return bool(row and row["owner"] == self.owner and time.time() - row["heartbeat"] <= self.ttl)

    def require(self, name: str):
        if not self.holds(name):
            raise LeaseLost(f"lease {name} is not held by {self.owner}; refusing to write")

    def release(self):
        with self.s:
            for name in self.held:
                self.s.x("DELETE FROM leases WHERE name=? AND owner=?", (name, self.owner))
        self.held.clear()
