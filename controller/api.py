"""HTTP client for the simulator. The only door to the world.

Retries are deliberate per call type:
- GETs are retried on connection errors and 5xx.
- POST /sim/advance is retried only after checking /sim/state, so a lost response never advances twice.
- POST /approvals and PUT /config are retried with the same proposal_id; a 409 duplicate tells the caller
  to reconcile instead of double-submitting.
- LLM and validation calls are never retried: a retry would spend credits twice.
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("controller.api")


class ApiError(Exception):
    def __init__(self, status: int, error: str, detail=None):
        super().__init__(f"{status} {error}: {detail}")
        self.status, self.error, self.detail = status, error, detail


class Sim:
    def __init__(self, url: str, timeout: float = 120.0, retries: int = 5):
        self.c = httpx.Client(base_url=url.rstrip("/"), timeout=timeout)
        self.retries = retries
        self.calls = 0
        self.errors = 0

    # ------------------------------------------------------------ plumbing
    def _send(self, method: str, path: str, *, retry: bool, **kw):
        delay = 0.5
        for attempt in range(self.retries + 1):
            self.calls += 1
            try:
                r = self.c.request(method, path, **kw)
            except httpx.TransportError as e:
                self.errors += 1
                if not retry or attempt == self.retries:
                    raise ApiError(0, "transport", str(e)) from e
                log.warning("transport error on %s %s: %s; retrying", method, path, e)
                time.sleep(delay)
                delay = min(delay * 2, 10)
                continue
            if r.status_code >= 500 and retry and attempt < self.retries:
                self.errors += 1
                log.warning("%s on %s %s; retrying", r.status_code, method, path)
                time.sleep(delay)
                delay = min(delay * 2, 10)
                continue
            return r
        raise AssertionError("unreachable")

    def _json(self, method: str, path: str, *, retry: bool = True, **kw):
        r = self._send(method, path, retry=retry, **kw)
        if r.status_code >= 400:
            self.errors += 1
            try:
                body = r.json()
            except ValueError:
                body = {"error": "http", "detail": r.text[:300]}
            raise ApiError(r.status_code, body.get("error") or "http", body.get("detail"))
        return r.json()

    # ------------------------------------------------------------ world
    def state(self):
        return self._json("GET", "/sim/state")

    def advance(self, minutes: int, expect_from: int):
        """Advance exactly once. If the response is lost, check whether the world already moved."""
        try:
            return self._json("POST", "/sim/advance", json={"minutes": minutes}, retry=False)
        except ApiError as e:
            if e.status not in (0, 500, 502, 503, 504):
                raise
            for _ in range(self.retries):
                time.sleep(1)
                try:
                    st = self.state()
                except ApiError:
                    continue
                if st["sim_minute"] != expect_from:
                    log.warning("advance response lost but world moved to %s", st["sim_minute"])
                    return {**st, "new_chunks": None}
                return self._json("POST", "/sim/advance", json={"minutes": minutes}, retry=False)
            raise

    def chunks(self, kind: str | None = None, after: int | None = None):
        params = {}
        if kind:
            params["kind"] = kind
        if after is not None:
            params["after"] = after
        return self._json("GET", "/events/chunks", params=params)

    def chunk_text(self, n: int) -> str:
        r = self._send("GET", f"/events/chunks/{n}", retry=True)
        if r.status_code >= 400:
            raise ApiError(r.status_code, "chunk", r.text[:200])
        return r.text

    def workflows(self):
        return self._json("GET", "/workflows")

    def workflow(self, wid: str):
        return self._json("GET", f"/workflows/{wid}")

    def config(self, wid: str):
        return self._json("GET", f"/workflows/{wid}/config")

    def config_history(self, wid: str):
        return self._json("GET", f"/workflows/{wid}/config/history")

    # ------------------------------------------------------------ governance
    def propose(self, body: dict):
        return self._json("POST", "/approvals", json=body, retry=True)

    def approvals(self, workflow_id: str | None = None, status: str | None = None):
        params = {k: v for k, v in (("workflow_id", workflow_id), ("status", status)) if v}
        return self._json("GET", "/approvals", params=params)

    def approval(self, aid: str):
        return self._json("GET", f"/approvals/{aid}")

    def apply(self, wid: str, aid: str):
        return self._json("POST", f"/workflows/{wid}/apply", json={"approval_id": aid}, retry=True)

    def auto_apply(self, wid: str, body: dict):
        return self._json("PUT", f"/workflows/{wid}/config", json=body, retry=True)

    # ------------------------------------------------------------ credits
    def suggest_fixes(self, wid: str, problem: str):
        return self._json("POST", "/llm/suggest-fixes", json={"workflow_id": wid, "problem": problem}, retry=False)

    def validate(self, wid: str, prompt_version: str, n: int):
        return self._json("POST", "/validation/runs",
                          json={"workflow_id": wid, "prompt_version": prompt_version, "n": n}, retry=False)
