"""Serve a scenario, run the controller against it end to end, then grade against the no-op baseline.

    python scripts/run_scenario.py scenarios/public_01_dev.yaml            # -> reports/public_01_dev.md
    python scripts/run_scenario.py scenarios/public_03_load.yaml --no-grade --tag load

This is a developer harness. It starts `python -m sim` as a separate process and talks to the controller
through `./run.sh`'s entry point; the controller itself never sees the scenario file.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = {**os.environ, "PYTHONUTF8": "1"}   # the benchmark summary uses Unicode; Windows consoles default to cp1252


def wait_health(url: str, proc: subprocess.Popen, timeout: float = 300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            raise SystemExit(f"simulator exited with {proc.returncode}")
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit("simulator did not become healthy")


def sim_state(url: str) -> dict | None:
    try:
        return httpx.get(f"{url}/sim/state", timeout=10).json()
    except (httpx.HTTPError, ValueError):
        return None


def require_completed(rc: int, state: dict | None):
    """The controller must have exited cleanly AND the simulated world must have reached its end. Anything else
    stops the harness with a nonzero exit before a report could be mistaken for completed evidence."""
    problems = []
    if rc != 0:
        problems.append(f"controller exited with {rc}")
    if not state:
        problems.append("could not read the simulator's final state")
    elif not state.get("finished"):
        problems.append(f"scenario not finished (world at minute {state.get('sim_minute')} of {state.get('end_minute')})")
    if problems:
        raise SystemExit("run failed, not grading: " + "; ".join(problems))


def guard_outputs(paths: list[str], overwrite: bool):
    """Refuse to destroy earlier evidence (a run dir, a store, a report) unless --overwrite was given."""
    existing = [p for p in paths if os.path.exists(os.path.join(ROOT, p))]
    if existing and not overwrite:
        raise SystemExit("refusing to overwrite existing evidence: " + ", ".join(existing) +
                         "\nuse a different --tag, or pass --overwrite to replace it")
    for p in existing:
        full = os.path.join(ROOT, p)
        shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)


def serve(scenario: str, run_dir: str, port: int) -> subprocess.Popen:
    if os.path.exists(os.path.join(ROOT, run_dir)):
        raise SystemExit(f"{run_dir} exists; call guard_outputs first")
    return subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", scenario, "--run-dir", run_dir,
                             "--port", str(port)], cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--tag", default=None, help="name for run dir / db / report (default: scenario name)")
    ap.add_argument("--no-grade", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing run dir, store and report")
    a = ap.parse_args()
    name = a.tag or os.path.splitext(os.path.basename(a.scenario))[0]
    run_dir = os.path.join("runs", f"mine_{name}")
    db = os.path.join("state", f"{name}.db")
    guard_outputs([run_dir, db, db + "-wal", db + "-shm", os.path.join("reports", f"{name}.md")], a.overwrite)
    url = f"http://127.0.0.1:{a.port}"
    proc = serve(a.scenario, run_dir, a.port)
    try:
        wait_health(url, proc)
        t0 = time.time()
        rc = subprocess.call([sys.executable, "-m", "controller", "run", "--url", url, "--db", db], cwd=ROOT, env=ENV)
        print(f"controller exit {rc} in {time.time() - t0:.0f}s", flush=True)
        state = sim_state(url)
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    require_completed(rc, state)   # a failed or unfinished run is never graded as evidence
    if a.no_grade:
        return
    base = os.path.join("runs", f"base_{os.path.splitext(os.path.basename(a.scenario))[0]}")
    if not os.path.exists(os.path.join(ROOT, base, "_truth")):
        if os.path.exists(os.path.join(ROOT, base)):
            shutil.rmtree(os.path.join(ROOT, base))
        subprocess.check_call([sys.executable, "-m", "sim", "baseline", "--scenario", a.scenario, "--run-dir", base],
                              cwd=ROOT, env=ENV)
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    out = os.path.join("reports", f"{name}.md")
    subprocess.check_call([sys.executable, "-m", "sim", "grade", "--run", run_dir, "--baseline", base, "--out", out],
                          cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL)
    print(f"report: {out}")


if __name__ == "__main__":
    main()
