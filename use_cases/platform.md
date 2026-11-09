# Platform reliability and scale

This use case exercises recovery and throughput in a self-contained benchmark runtime.

- Commit aggregates and their ingestion checkpoint in one transaction.
- Persist deterministic proposal IDs before sending; reconcile uncertain outcomes before retrying.
- Recover after hard process kills without duplicate applications, lost approvals or duplicate ingestion.
- Enforce world and workflow leases; stop acting when ownership is lost.
- Measure throughput, decision lag, memory and simulated-day duration, with a target below ten wall-clock minutes per day.
- Keep page-worthy alerts separate from diagnostic warnings and document the operator action for each.
