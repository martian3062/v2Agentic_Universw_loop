# Compliance and approval policy

Northwind is a simulated tenant used to exercise stricter governance requirements.

- Require named human approval for customer-facing welcome, voice, prompt and cohort changes, including rollbacks.
- Respect each workflow's policy: the KYC flow disables auto-apply for all configuration dimensions.
- Attach a validation run to every proposed prompt change; validation measures safety, with engagement checked after apply.
- Keep decisions queryable by workflow, cohort, configuration version, time and approver.
- Respect approver hours, expiry, apply windows and request quotas.
