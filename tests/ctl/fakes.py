"""A minimal in-memory stand-in for the simulator's governance API, for unit tests of the controller."""
from __future__ import annotations

import copy

from controller.api import ApiError
from controller.util import changed_dims, fmt


def ts(minute: int) -> str:
    day, rem = divmod(minute, 1440)
    return f"2027-01-{4 + day:02d}T{rem // 60:02d}:{rem % 60:02d}:00+05:30"


BASE_CFG = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0},
                                                         "caller_id": {"D1": 0.5, "D2": 0.5}}},
            "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}


def workflow(wid="wf", auto=True, allowed=("caller_id", "schedule"), max_shift=0.5, quota=3, hours=(10, 19),
             latency=(30, 240)):
    return {"workflow_id": wid, "tenant_id": "tenant", "dialling_window": [9, 21],
            "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
            "options": {"welcome_message": ["W1", "W2"], "voice": ["V1", "V2"], "caller_id": ["D1", "D2", "D3"],
                        "prompt_version": ["P1", "P3"]},
            "governance": {"auto_apply": {"enabled": auto, "allowed_dims": list(allowed), "max_shift": max_shift},
                           "approver_hours": list(hours), "approver_latency_minutes": list(latency),
                           "proposal_ttl_hours": 24, "apply_window_hours": 2, "max_requests_per_24h": quota,
                           "base_reject_rate": 0.0, "min_validation_pass_rate": 0.97}}


class FakeSim:
    def __init__(self, wid="wf"):
        self.wid = wid
        self.minute = 600
        self.version = 1
        self.cfg = copy.deepcopy(BASE_CFG)
        self.history = [{"version": 1, "actor": "business_default", "changed_dims": [], "applied_at": None,
                         "proposal_id": None, "approval_id": None}]
        self.apps: dict[str, dict] = {}
        self.posts = 0
        self.puts = 0
        self.errors = 0
        self.calls = 0

    # config
    def config(self, wid):
        return {"workflow_id": wid, "version": self.version, "actor": self.history[-1]["actor"],
                "applied_at": ts(self.minute), "config": copy.deepcopy(self.cfg)}

    def config_history(self, wid):
        return copy.deepcopy(self.history)

    def _set(self, cfg, actor, pid=None, aid=None):
        dims = changed_dims(self.cfg, cfg)
        self.cfg = copy.deepcopy(cfg)
        self.version += 1
        self.history.append({"version": self.version, "actor": actor, "changed_dims": dims,
                             "applied_at": ts(self.minute), "proposal_id": pid, "approval_id": aid})
        return {"workflow_id": self.wid, "version": self.version, "changed_dims": dims}

    def supervisor_edit(self, cfg):
        return self._set(cfg, "supervisor")

    # governance
    def propose(self, body):
        self.posts += 1
        if any(a["proposal_id"] == body["proposal_id"] for a in self.apps.values()):
            raise ApiError(409, "duplicate_proposal_id", body["proposal_id"])
        if body["base_version"] != self.version:
            raise ApiError(409, "stale_base_version", {"current_version": self.version})
        aid = f"apr_{len(self.apps) + 1:05d}"
        a = {"approval_id": aid, "proposal_id": body["proposal_id"], "workflow_id": body["workflow_id"],
             "status": "pending", "base_version": body["base_version"], "created_at": ts(self.minute),
             "expires_at": ts(self.minute + 1440), "_cfg": body["proposed_config"]}
        self.apps[aid] = a
        return {k: v for k, v in a.items() if not k.startswith("_")}

    def decide(self, aid, status, reasons=()):
        a = self.apps[aid]
        a["status"] = status
        a["decided_at"] = ts(self.minute)
        a["reasons"] = list(reasons)
        if status == "approved":
            a["apply_by"] = ts(self.minute + 120)

    def approvals(self, workflow_id=None, status=None):
        return [{k: v for k, v in a.items() if not k.startswith("_")} for a in self.apps.values()
                if (not workflow_id or a["workflow_id"] == workflow_id) and (not status or a["status"] == status)]

    def approval(self, aid):
        return {k: v for k, v in self.apps[aid].items() if not k.startswith("_")}

    def apply(self, wid, aid):
        a = self.apps[aid]
        if a["status"] != "approved":
            raise ApiError(409, "not_approved", {"status": a["status"]})
        if a["base_version"] != self.version:
            raise ApiError(409, "stale_base_version", {"current_version": self.version})
        a["status"] = "applied"
        return self._set(a["_cfg"], "controller_approved", a["proposal_id"], aid)

    def auto_apply(self, wid, body):
        self.puts += 1
        if any(h["proposal_id"] == body["proposal_id"] for h in self.history):
            raise ApiError(409, "duplicate_proposal_id", body["proposal_id"])
        if body["base_version"] != self.version:
            raise ApiError(409, "stale_base_version", {"current_version": self.version})
        return self._set(body["proposed_config"], "controller_auto", body["proposal_id"])

    credits = 200.0
    pass_rates: dict = {}

    def state(self):
        return {"sim_minute": self.minute, "credits_remaining": self.credits, "ts": fmt(self.minute)}

    def validate(self, wid, prompt_version, n):
        cost = -(-n // 100)
        if cost > self.credits:
            raise ApiError(402, "no_credits", None)
        self.credits -= cost
        pr = self.pass_rates.get(prompt_version, 0.99)
        return {"validation_run_id": f"val_{prompt_version}_{int(self.credits)}", "workflow_id": wid,
                "prompt_version": prompt_version, "n": n, "failures": round(n * (1 - pr)), "pass_rate": pr,
                "credits_remaining": self.credits}

    def suggest_fixes(self, wid, problem):
        self.credits -= 3
        return {"suggestions": [{"prompt_version": "P3", "summary": "Grounded"}], "credits_remaining": self.credits}


class LossySim(FakeSim):
    """Requests land, but the response is lost: the client sees a transport error."""

    def propose(self, body):
        super().propose(body)
        raise ApiError(0, "transport", "connection reset after send")

    def auto_apply(self, wid, body):
        super().auto_apply(wid, body)
        raise ApiError(0, "transport", "connection reset after send")


class Recorder:
    """Owner callbacks: remembers what governance told it."""

    def __init__(self):
        self.applied, self.returned = [], []

    def on_applied(self, intent, minute, version):
        self.applied.append((intent["intent_id"], version))

    def on_returned(self, intent, minute, why, reasons):
        self.returned.append((intent["intent_id"], why))
