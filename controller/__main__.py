"""CLI: run the controller, or query its store.

    python -m controller run    --url http://127.0.0.1:8700 [--db state/controller.db]
    python -m controller why    --workflow nw_renewal --at "D3 14:00" [--tenant northwind_life]
    python -m controller view   [--workflow ks_cancel] [--tenant kestrel_finance]
    python -m controller health
    python -m controller log    [--workflow X] [--tenant T] [--kind applied] [--limit 50]
    python -m controller replay --proposal <proposal_id> [--cancel]

--tenant scopes a read to one client: a supervisor sees only their own workflows, decisions and alerts, never
another client's, and none of the platform-wide (operator) alerts. Without it the operator sees everything.
"""
import argparse
import logging
import os
import sys

DEFAULT_DB = os.environ.get("CONTROLLER_DB", "state/controller.db")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", required=True)
    r.add_argument("--db", default=DEFAULT_DB)
    r.add_argument("--log-level", default=os.environ.get("CONTROLLER_LOG", "INFO"))
    w = sub.add_parser("why")
    w.add_argument("--db", default=DEFAULT_DB)
    w.add_argument("--workflow", required=True)
    w.add_argument("--at", required=True, help="'D3 14:00' or a sim minute")
    w.add_argument("--tenant")
    v = sub.add_parser("view")
    v.add_argument("--db", default=DEFAULT_DB)
    v.add_argument("--workflow")
    v.add_argument("--tenant")
    h = sub.add_parser("health")
    h.add_argument("--db", default=DEFAULT_DB)
    lg = sub.add_parser("log")
    lg.add_argument("--db", default=DEFAULT_DB)
    lg.add_argument("--workflow")
    lg.add_argument("--tenant")
    lg.add_argument("--kind")
    lg.add_argument("--limit", type=int, default=50)
    rp = sub.add_parser("replay", help="operator action on a dead-lettered request")
    rp.add_argument("--db", default=DEFAULT_DB)
    rp.add_argument("--proposal", required=True)
    rp.add_argument("--cancel", action="store_true", help="cancel it: once the next lookup confirms it never landed, its intents go "
                         "back to their owners (if it did land, it is adopted instead)")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        logging.basicConfig(level=a.log_level.upper(), stream=sys.stderr,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        from .leases import LeaseLost
        from .loop import Controller
        try:
            Controller(a.url, a.db).run()
        except LeaseLost as e:
            sys.exit(f"controller stopped: {e}")
        return
    from . import audit
    from .db import Store
    from .util import parse_when
    if not os.path.exists(a.db):
        sys.exit(f"no store at {a.db}")
    s = Store(a.db)
    if a.cmd == "why":
        print(audit.why(s, a.workflow, parse_when(a.at), tenant=a.tenant))
    elif a.cmd == "view":
        print(audit.view(s, a.workflow, tenant=a.tenant))
    elif a.cmd == "health":
        print(audit.health(s))
    elif a.cmd == "log":
        print(audit.log_lines(s, a.workflow, a.kind, a.limit, tenant=a.tenant))
    elif a.cmd == "replay":
        from .governance import replay
        print(replay(s, a.proposal, s.get("last_minute", 0), cancel=a.cancel))


if __name__ == "__main__":
    main()
