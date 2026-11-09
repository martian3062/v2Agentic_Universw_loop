# Campaign operations

Kestrel is a simulated tenant used to model supervisor edits and constrained approval capacity.

- Preserve supervisor configuration edits and pause experiments on a dimension edited by a human.
- Batch multiple changes into one request, with at most two requests per day and none before 11:00.
- Show active experiments, results, pending requests, changes and incident status in one operator view.
- Allow incident caller-ID rotation only within the workflow's auto-apply policy.
- Verify engagement as well as safety after a prompt fix; rank verbatim-script fixes last because they can reduce engagement.
