# Controller: design, operation and runbook

For the engineer who owns this loop. The simulator contract is in [SIMULATOR.md](SIMULATOR.md); the argument
for every choice is in [../SUBMISSION.md](../SUBMISSION.md).

## Run it

```bash
pip install -r requirements.txt
./run.sh http://127.0.0.1:8700                    # contract: runs until the scenario finishes
python scripts/run_scenario.py scenarios/public_02_incidents.yaml   # serve + run + grade, one command
python scripts/crash_test.py scenarios/public_02_incidents.yaml     # kill -9 eight times, verify invariants
# both refuse to replace an existing run dir, store or report: use --tag <new name>, or --overwrite on purpose
python -m pytest -q tests                          # simulator smoke tests + controller unit tests
```

State is one SQLite file, `state/controller.db` by default (`CONTROLLER_DB` overrides). Re-running against
the same world resumes from it. "Same world" is checked against the simulator's own records, not just the
scenario name (`loop.continuity`). Every ingested chunk must be listed with the same event ids, every recorded
config version must be in the simulator's config history with the same actor and `proposal_id`, and every
approval we hold must exist. Otherwise, for example on a fresh restart of the same scenario, the old file is
archived next to it as `*.old` and the controller starts fresh.

## Ask it questions (read-only, never touches the simulator)

```bash
python -m controller why    --workflow nw_renewal --at "D3 14:00"   # why did X change, who approved, what next
python -m controller view   [--workflow ks_cancel]                  # the supervisor's one screen
python -m controller health                                         # loop health: throughput, lag, credits, pages
python -m controller log    [--workflow X] [--kind applied]          # the raw decision log
python -m controller view   --tenant kestrel_finance                # one client's view: its workflows only
python -m controller replay --proposal <id> [--cancel]              # operator action on a dead-lettered request
```

## Shape

```text
                 +--------------------------- one SQLite file (WAL) ---------------------------+
 sim chunks ---> | ingest.py   chunks(checkpoint) agg_hour  agg_seg  leads/segs  daily           |
                 | callerid.py cid:<wf> number states          incidents.py  incidents           |
                 | experiments.py experiments cohorts          governance.py intents  proposals  |
                 | audit: decisions alerts metrics config_versions                              |
                 +----------------------------------------------------------------------------+
 per tick:  ingest -> reconcile in-flight requests (apply approved) -> caller ID -> incidents ->
            experiments -> submit batched requests -> metrics -> POST /sim/advance (15 min; 60 at night)
```

| module | owns |
|---|---|
| `api.py` | HTTP client. Retry rules per call: GETs retried; `advance` re-checks `/sim/state` before retrying so it never advances twice; LLM and validation calls never retried (credits). |
| `ingest.py` | One transaction per chunk: aggregates + leads + side effects + checkpoint row. Raw calls are not stored. |
| `governance.py` | The only code that changes config. Intents (replayable ops + evidence) become one batched request per workflow, through the path the policy allows, timed against approver hours, latency and quota. |
| `callerid.py` | C5: number state machine, detection, rotation, safe exploration of untested numbers. |
| `incidents.py` | C8 hallucination response, KPI drift alerts, the incident records. |
| `experiments.py` | C6/C7: experiment lifecycle on shared cohorts, interim, segment carve-outs, promotion, decay, rollback. |
| `estimates.py` | C10: affected leads, dry-run replay, conservative gain behind the impact score. |
| `audit.py` | C11/C12/C14 read-only views, optionally scoped to one tenant (`--tenant`). |
| `leases.py` | One controller process per world and per workflow: heartbeat leases, takeover from a dead or silent holder, fencing before every write. |
| `settings.py` | Every threshold, with the reason for its value; tenant preferences stricter than policy. |

## Decision rules (short form; numbers live in settings.py)

- **Populations, not anecdotes.** Every detector has a minimum sample, a minimum effect and a strict
  threshold. Caller ID: z <= -4.5 and pickup <= 75% of expected over >= 150 dials, compared with the other
  numbers dialled to the same cohorts in the same hours. Hallucination: exact binomial tail p <= 1e-5 and
  >= 2.5x the history flag rate over >= 200 connected calls.
- **Experiments.** Fixed allocation (incumbent share by the square-root rule), always-valid mSPRT
  sequences at alpha/k, hourly looks. Winner = primary sequence excludes 0, every arm >= 300 trials,
  **guardrail non-inferiority shown** (the guardrail sequence's lower bound above -margin), and over the last
  24h still at least half as far ahead as overall (a fading variant fails this). A primary win without
  guardrail evidence is held, with a `promotion_held` log line stating the sample still needed. Harm
  (overall, or over the last 24h at alpha 0.002) drops the arm. At 4 days: segment analysis with always-valid
  sequences, Bonferroni over every segment value x every original challenger (fixed in advance, so repeated
  looks are fine). Losers leave; segment winners get a cohort only if they also show guardrail
  non-inferiority inside the segment (otherwise `segment_insufficient_guardrail`). Promotions keep a 10%
  holdout; decay = drift-adjusted CUSUM >= log(100) and the winner's own rate down (z <= -2). Carved cohorts
  roll back on harm only (their holdout is too small to show lost lift).
- **Atomic decisions.** Methods marked `@atomic` (db.py) run in one transaction: the intent an owner files and
  the state that intent relies on (an experiment's conclusion, an incident's chosen fix, numbers put on
  probation) commit together. Never put a credit-spending call inside one: a rollback would erase the record
  of a charge the simulator already made (`Incidents._paid_call` asserts this).
- **Experiment comparisons** are stratified by (config version, day): assignment probabilities are constant
  inside a stratum, and strata are combined with a Mantel-Haenszel risk difference (`stats.msprt_strat`).
  Pooling across an allocation change while rates drift manufactures lifts.
- **Prompt-fix verification** judges engagement against the workflow's own margin
  (`incidents.engagement_margin`): its `engagement_rate_max_drop`, or `PRIMARY_ENGAGEMENT_MAX_DROP` when
  engagement is the primary KPI.
- **Caller-ID comparison** is stratified: each number vs. the other numbers within the same cohort and hour,
  then summed. Pooling over hours first turns a different hourly traffic mix into a false "bad number".
- **Credits.** Every paid call is charged to its incident before it is sent and settled afterwards against
  the simulator's balance: refunded only if provably not charged. Each incident has a 25-credit ceiling. Spend must leave the 30-credit reserve intact, except
  an incident's first validation (without one validated candidate a prompt fix cannot be requested at all).
  LLM suggestions are never bought below the reserve.
- **Governance.** Customer-facing changes (welcome, voice, prompt, cohorts) always go to a human, even where
  policy would allow auto-apply, unless the change only moves traffic back to what a human approved. That
  exception never applies to a tenant with `human_approval_for_customer_facing` (Northwind): there, rollbacks
  go to the approver too.
  Operational changes (caller ID) auto-apply where policy allows, one `max_shift` step at a time, each step
  re-checked. One routine request in flight per workflow; incidents may add one urgent request alongside.
  One request per 24h is reserved for incidents. Routine requests are only filed when they can go out now
  (inside approver hours, with enough of the window left for the approver's worst-case latency), so the
  evidence is fresh when a human reads it.
- **Supervisor edits** are never overwritten. A supervisor's manual choice on a dimension ends our
  experimentation on that dimension for a week. A hallucination fix that reverts a supervisor's prompt is
  a request to a human that says so in its rationale.

## Proposal lifecycle

```text
intent: ready -> in_proposal -> applied | returned (rejected/expired/stale/lapsed/failed) | cancelled
proposal (approval): prepared -> pending -> approved -> applied
                                          \-> rejected | expired         \-> stale (409) | lapsed (410, pages)
proposal (auto):     prepared -> applied | refused (403: intents switch to the approval path)
uncertain send:      prepared -> (looked up every tick; re-sent at +15/30/60/120 min) -> ... -> dead after 8 sends
dead:                no more sends, still looked up every tick -> pending | applied (it had landed after all)
                     | prepared (operator replay, same proposal_id) | cancelling (operator cancel)
cancelling:          -> cancelled once a lookup confirms it never landed (intents back to owners, with callbacks)
                     | pending | applied (it had landed: adopted, the cancel is moot)
```

`prepared` rows are the crash-safety hinge. The `proposal_id` is committed before the request is sent. A
`prepared` row means "we may or may not have sent it": after a crash, and also after a transport error or a 5xx
on the request itself. Only a definite 4xx marks a request `failed`. Every reconcile looks a `prepared` approval
up in `GET /approvals` by `proposal_id`, and an auto-apply in `GET /config/history`. It is adopted if found, and
re-sent with the same id if not (a 409 duplicate means "look it up"). An approval already applied by us before a
crash answers `409 not_approved {status: applied}`, which reconciles to `applied`, never to a second apply.

**Atomic outcomes.** Recording an outcome (applied, or intents returned) and the owners' reaction to it
(experiment goes live, incident starts verification, caller-ID step recorded) commit in one transaction.
A crash or a raising callback leaves neither. The next reconcile replays both, because the simulator still says
`applied`. Reconcile isolates each proposal: one failing proposal pages (`reconcile_failed:<id>`) and is retried
every tick without blocking the others.

## Runbook: alerts

`python -m controller health` lists pages; `view` lists every open alert.

| alert | severity | what it means | what to do |
|---|---|---|---|
| `step_failed:<step>` | page | a controller step raised; the step changed nothing (one transaction) and is retried every tick | read the traceback in stderr; the loop keeps running |
| `dead:<proposal>` | page | a request's outcome stayed unknown through 8 sends (simulator unreachable or failing); no more sends, but it is still looked up every tick and the page resolves itself if it turns out to have landed; its intents are parked | fix the simulator/network, then `python -m controller replay --proposal <id>` (same id, so a request that did land is adopted, not duplicated), or `--cancel` to hand the work back to its owners (applied on the next tick that confirms it never landed) |
| `leases_lost` | page | another controller took over this one's leases (it was paused past the TTL, or a second instance started); this one stopped acting | make sure exactly one controller runs per store; the survivor carries on |
| `reconcile_failed:<proposal>` | page | reconciling one request failed (a callback raised); nothing was recorded, it is retried every tick | read the traceback; the other requests are unaffected |
| `lapsed:<proposal>` | page | an approved change was not applied within `apply_window_hours` | should not happen (we poll every 15 min); check the process was up and the clock advancing |
| `incident_age:<id>` | page | an incident open > 6h | `why`/`log` for the workflow; typical causes are approvals waiting for approver hours, or no validated fix |
| `no_fix:<id>` | page | no prompt candidate passed validation within the per-incident credit ceiling | needs a human prompt fix; the incident stays open |
| `incident_stuck:<id>` | page | a prompt fix was returned 4 times; the incident stays open and re-files every 2h | the approver keeps declining; talk to the client |
| `cid_nowhere:<wf>` | page | every caller ID is out of rotation | get numbers cleared or added with the carrier |
| `credits_low` | page | credits below the reserve (30) | incident response is at risk; stop optional validation |
| `incident:<id>` | warn | an incident is open and being handled (it resolves itself when the incident does) | nothing, unless it turns into `incident_age` |
| `did_out:<wf>:<num>` | warn | a number looks spam-labelled and is out of rotation | clear it with the carrier, then delete it from `cid:<wf>` in `meta` |
| `drift:<wf>:pickup` | warn | workflow pickup well below history, not specific to one number | look at the dialler and the lead mix; no automatic action |
| `slow_day:<d>` | warn | one simulated day took > 10 wall-clock minutes | see Scale in SUBMISSION.md |

## Extending

- A new detector: add it to `incidents.py` or a module of its own, open incidents with `Incidents._new`, and
  file intents with `Governance.file_intent(owner="inc:<id>")`. Register callbacks to hear about the outcome.
- A new experiment dimension: add it to `EXPERIMENT_DIMS_BY_PRIMARY`, and to `DIM_COL` if it is a column of
  `agg_hour`.
- A new tenant preference: `TENANT_PREFERENCES` in settings.py. Stricter than policy always wins.
