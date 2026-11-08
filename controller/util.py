"""Time and config helpers shared by every module."""
from __future__ import annotations

import copy
import datetime as dt
import json
import re

D0 = dt.date(2027, 1, 4)          # D0 00:00 IST, from docs/SIMULATOR.md §1
AGENT_DIMS = ("welcome_message", "voice", "caller_id")
_date_cache: dict[str, int] = {}


def minute_of(ts: str) -> int:
    """'2027-01-04T10:15:00+05:30' -> simulated minute (negative before D0). All timestamps are IST."""
    d = ts[:10]
    off = _date_cache.get(d)
    if off is None:
        off = (dt.date.fromisoformat(d) - D0).days * 1440
        _date_cache[d] = off
    return off + int(ts[11:13]) * 60 + int(ts[14:16])


def fmt(minute: int | None) -> str:
    if minute is None:
        return "-"
    day, rem = divmod(minute, 1440)
    return f"D{day} {rem // 60:02d}:{rem % 60:02d}"


def parse_when(s: str) -> int:
    """'D3 14:00' or an integer minute."""
    s = s.strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    m = re.fullmatch(r"D(-?\d+)\s+(\d{1,2}):(\d{2})", s)
    if not m:
        raise ValueError(f"bad time {s!r}; use 'D3 14:00'")
    return int(m.group(1)) * 1440 + int(m.group(2)) * 60 + int(m.group(3))


def hour_of_day(minute: int) -> int:
    return (minute % 1440) // 60


def dumps(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------- config helpers

def normalise(dist: dict) -> dict:
    tot = sum(v for v in dist.values() if v > 0)
    return {k: v / tot for k, v in sorted(dist.items()) if v > 0} if tot > 0 else {}


def tv_distance(a: dict, b: dict) -> float:
    a, b = normalise(a), normalise(b)
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b))


def effective_dist(cfg: dict, cohort: str, dim: str) -> dict:
    al = cfg.get("allocations", {})
    d = al.get(cohort, {}).get(dim)
    if d is None:
        d = al.get("default", {}).get(dim, {})
    return normalise(d)


def cohorts_inheriting(cfg: dict, dim: str) -> list[str]:
    """Cohort ids whose `dim` comes from the default allocation (including 'default' itself)."""
    out = ["default"]
    for c in cfg.get("cohorts", []):
        if dim not in cfg.get("allocations", {}).get(c["id"], {}):
            out.append(c["id"])
    return out


def round_dist(d: dict) -> dict:
    """Round to 4 decimals and make the values sum to exactly 1.0 in floating point. The simulator normalises
    what we send; if our values already sum to 1.0, that is a no-op, so a dimension we did not touch is
    byte-identical when we send the config back (otherwise the auto-apply check sees a phantom change)."""
    d = normalise(d)
    out = {k: round(v, 4) for k, v in d.items() if round(v, 4) > 0}
    if not out:
        return out
    big = max(out, key=out.get)
    for _ in range(8):
        rest = sum(v for k, v in out.items() if k != big)
        out[big] = 1.0 - rest
        if sum(out.values()) == 1.0:
            break
        out[big] = round(out[big], 4)
    return dict(sorted(out.items()))


def apply_ops(base: dict, ops: list[dict]) -> dict:
    """Ops are data, so an intent survives a restart and can be replayed onto a newer base."""
    cfg = copy.deepcopy(base)
    for op in ops:
        kind = op["op"]
        if kind == "set_alloc":
            cfg.setdefault("allocations", {}).setdefault(op["cohort"], {})[op["dim"]] = round_dist(op["dist"])
        elif kind == "set_prompt":
            cfg["prompt_version"] = op["value"]
        elif kind == "add_cohort":
            cohorts = [c for c in cfg.get("cohorts", []) if c["id"] != op["cohort"]["id"]]
            cohorts.insert(min(op.get("position", len(cohorts)), len(cohorts)), op["cohort"])
            cfg["cohorts"] = cohorts
        else:
            raise ValueError(f"unknown op {kind}")
    return cfg


def changed_dims(a: dict, b: dict) -> list[str]:
    dims = []
    if a.get("cohorts", []) != b.get("cohorts", []):
        dims.append("cohorts")
    ids = {"default"} | {c["id"] for c in a.get("cohorts", [])} | {c["id"] for c in b.get("cohorts", [])}
    for dim in AGENT_DIMS:
        if any(effective_dist(a, c, dim) != effective_dist(b, c, dim) for c in ids):
            dims.append(dim)
    if a.get("prompt_version") != b.get("prompt_version"):
        dims.append("prompt_version")
    if a.get("schedule") != b.get("schedule"):
        dims.append("schedule")
    return dims


def max_shift(a: dict, b: dict) -> float:
    ids = {"default"} | {c["id"] for c in a.get("cohorts", [])} | {c["id"] for c in b.get("cohorts", [])}
    return max((tv_distance(effective_dist(a, c, d), effective_dist(b, c, d)) for c in ids for d in AGENT_DIMS),
               default=0.0)


def config_diff(a: dict, b: dict) -> dict:
    """A small human-readable diff: path -> {from, to}."""
    out = {}
    ids = ["default"] + sorted({c["id"] for c in a.get("cohorts", [])} | {c["id"] for c in b.get("cohorts", [])})
    for c in ids:
        for d in AGENT_DIMS:
            x, y = effective_dist(a, c, d), effective_dist(b, c, d)
            if x != y:
                out[f"allocations.{c}.{d}"] = {"from": round_dist(x), "to": round_dist(y)}
    if a.get("prompt_version") != b.get("prompt_version"):
        out["prompt_version"] = {"from": a.get("prompt_version"), "to": b.get("prompt_version")}
    if a.get("cohorts", []) != b.get("cohorts", []):
        out["cohorts"] = {"from": a.get("cohorts", []), "to": b.get("cohorts", [])}
    if a.get("schedule") != b.get("schedule"):
        out["schedule"] = {"changed": True}
    return out
