"""Crash-recovery test (C13): kill -9 the controller at awkward moments, restart it, check it finishes correctly.

    python scripts/crash_test.py scenarios/public_02_incidents.yaml [--kills 8] [--seed 1] [--overwrite]

Kill points:
  - during history bootstrap (before the first advance),
  - while an approved change is waiting to be applied (the 2h apply window is the dangerous one),
  - at random simulated times.
After the run it checks, from the simulator's own records and from our store:
  1. the scenario finished;
  2. no proposal_id was applied twice (config history);
  3. every approval the simulator holds is known to our store with the same status, none lapsed;
  4. every controller-made config version is in our audit store with its proposal;
  5. every event chunk was ingested exactly once.
Writes reports/crash_recovery.md.
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import subprocess
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_scenario import ENV, ROOT, guard_outputs, serve, wait_health  # noqa: E402


def fmt(m):
    return f"D{m // 1440} {(m % 1440) // 60:02d}:{m % 60:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--kills", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--overwrite", action="store_true", help="replace an existing crash run, store and reports")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    name = os.path.splitext(os.path.basename(a.scenario))[0]
    run_dir = os.path.join("runs", f"crash_{name}")
    db = os.path.join("state", f"crash_{name}.db")
    guard_outputs([run_dir, db, db + "-wal", db + "-shm", os.path.join("reports", f"crash_{name}_grade.md"),
                   os.path.join("reports", f"crash_recovery_{name}.md")], a.overwrite)
    url = f"http://127.0.0.1:{a.port}"
    srv = serve(a.scenario, run_dir, a.port)
    log = []
    try:
        wait_health(url, srv)
        c = httpx.Client(base_url=url, timeout=30)
        end = c.get("/sim/state").json()["end_minute"]
        targets = sorted(rng.sample(range(600, end - 600, 15), max(0, a.kills - 3)))
        plan = ["bootstrap", "approved", "approved"] + [f"minute:{t}" for t in targets]
        cmd = [sys.executable, "-m", "controller", "run", "--url", url, "--db", db, "--log-level", "WARNING"]
        starts = 0
        while True:
            proc = subprocess.Popen(cmd, cwd=ROOT, env=ENV)
            starts += 1
            point = plan.pop(0) if plan else None
            t0 = time.time()
            killed = None
            while proc.poll() is None:
                time.sleep(0.05)
                if point is None:
                    continue
                st = c.get("/sim/state").json()
                if point == "bootstrap" and time.time() - t0 > 1.5 and st["sim_minute"] == 0:
                    killed = "during history bootstrap"
                elif point == "approved" and c.get("/approvals", params={"status": "approved"}).json():
                    killed = "with an approved change not yet applied"
                elif point.startswith("minute:") and st["sim_minute"] >= int(point.split(":")[1]):
                    killed = f"at {fmt(st['sim_minute'])}"
                if killed:
                    proc.kill()          # SIGKILL on Linux, TerminateProcess on Windows: no cleanup runs
                    proc.wait()
                    log.append(f"kill #{len(log) + 1}: {killed} (world at {fmt(st['sim_minute'])})")
                    print(log[-1], flush=True)
                    break
            if not killed:
                if proc.returncode != 0:
                    log.append(f"controller exited with {proc.returncode} (not killed)")
                break
        st = c.get("/sim/state").json()
        checks = verify(c, os.path.join(ROOT, db), st)
        checks.insert(1, ("controller's final run exited cleanly", proc.returncode == 0,
                          f"exit code {proc.returncode}"))
    finally:
        srv.terminate()
        srv.wait(timeout=30)
    base = os.path.join("runs", f"base_{name}")
    if not os.path.exists(os.path.join(ROOT, base, "_truth")):
        subprocess.check_call([sys.executable, "-m", "sim", "baseline", "--scenario", a.scenario, "--run-dir", base],
                              cwd=ROOT, env=ENV)
    out = os.path.join("reports", f"crash_{name}_grade.md")
    subprocess.check_call([sys.executable, "-m", "sim", "grade", "--run", run_dir, "--baseline", base, "--out", out],
                          cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL)
    write_report(name, a, starts, log, checks, out)


def verify(c, db_path, st) -> list[tuple[str, bool, str]]:
    s = sqlite3.connect(db_path)
    s.row_factory = sqlite3.Row
    out = [("scenario finished", bool(st["finished"]), fmt(st["sim_minute"]))]
    wfs = [w["workflow_id"] for w in c.get("/workflows").json()]
    dup, made, missing = [], 0, []
    for w in wfs:
        hist = c.get(f"/workflows/{w}/config/history").json()
        pids = [h["proposal_id"] for h in hist if h.get("proposal_id")]
        dup += [p for p in set(pids) if pids.count(p) > 1]
        for h in hist:
            if h["actor"].startswith("controller"):
                made += 1
                r = s.execute("SELECT proposal_id FROM config_versions WHERE workflow_id=? AND version=?",
                              (w, h["version"])).fetchone()
                if not r or r["proposal_id"] != h["proposal_id"]:
                    missing.append(f"{w} v{h['version']}")
    out.append(("no proposal applied twice", not dup, f"{made} controller changes, duplicates: {dup or 'none'}"))
    out.append(("every controller change is in our audit store", not missing,
                f"missing: {missing or 'none'}"))
    apps = c.get("/approvals").json()
    mism, lapsed = [], [x["approval_id"] for x in apps if x["status"] == "lapsed"]
    # 'stale' is ours: approved, but the config moved on before we could apply it (409 stale_base_version), so the
    # simulator still says approved (or lapsed once its window passes). We re-propose on the new base instead.
    same = {("approved", "stale"), ("lapsed", "stale")}
    for x in apps:
        r = s.execute("SELECT status FROM proposals WHERE approval_id=?", (x["approval_id"],)).fetchone()
        if not r or (r["status"] != x["status"] and (x["status"], r["status"]) not in same):
            mism.append(f"{x['approval_id']} sim={x['status']} ours={r['status'] if r else None}")
    out.append(("every approval reconciled, none lost", not mism, f"{len(apps)} approvals; mismatches: {mism or 'none'}"))
    out.append(("no approved change lapsed", not lapsed, f"lapsed: {lapsed or 'none'}"))
    listed = {ch["chunk"]: ch["events"] for kind in ("history", "events")
              for ch in c.get("/events/chunks", params={"kind": kind}).json()}
    ours = {r["chunk"]: r["events"] for r in s.execute("SELECT chunk, events FROM chunks")}
    out.append(("every chunk ingested exactly once", listed == ours,
                f"{len(listed)} chunks listed, {len(ours)} ingested"))
    return out


def write_report(name, a, starts, log, checks, grade_path):
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    grade = open(os.path.join(ROOT, grade_path), encoding="utf-8").read().split("## Changes applied")[0]
    lines = [f"# Crash-recovery test: {name}", "",
             f"`python scripts/crash_test.py scenarios/{name}.yaml --kills {a.kills} --seed {a.seed}`", "",
             f"The controller was started {starts} times and hard-killed {len(log)} times "
             "(SIGKILL / TerminateProcess: no shutdown code runs).", "", "## Kills", ""]
    lines += [f"- {x}" for x in log]
    lines += ["", "## Invariants after the run", "", "| check | result | detail |", "|---|---|---|"]
    lines += [f"| {n} | {'PASS' if ok else 'FAIL'} | {d} |" for n, ok, d in checks]
    lines += ["", "## Outcome of the crashed run (grader)", "", grade.split("\n", 1)[1].strip(), "",
              f"Full grade: `{grade_path}`"]
    p = os.path.join(ROOT, "reports", f"crash_recovery_{name}.md")
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(f"{'PASS' if ok else 'FAIL'}  {n}: {d}" for n, ok, d in checks))
    print(f"report: {p}")
    if not all(ok for _, ok, _ in checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
