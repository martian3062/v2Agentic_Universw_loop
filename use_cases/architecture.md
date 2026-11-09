# Architecture and operational priorities

The prototype prioritises explainable, governed decisions over feature breadth.

- Keep governance, auditability, incident response and crash recovery on the critical path.
- Use deterministic statistical rules to select winners; LLM suggestions supply replacement prompts only when a known-good prompt is unavailable.
- Spend finite credits where they can change a decision, with per-incident ceilings and a reserve.
- Keep configuration writes inside the governance boundary and respect stricter tenant requirements.
- Document throughput limits, production migration steps and runbooks for the next operator.
- Prefer measured evidence from events and API responses over assumptions about a workflow's behavior.
