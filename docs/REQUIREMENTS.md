# Platform requirements

**Outcome-First Agent Loop** - Hackathon-style simulation and benchmarking

---

## 1. Context

The simulation models outbound voice workflows for insurers and lenders: renewal reminders, KYC follow-ups,
cancellation reconfirmations. Each **workflow** dials leads within a business-set window, following a retry
cadence the business configures. Today a human sets the agent's configuration once and rarely revisits it.
That configuration covers which welcome message it opens with, which voice it uses, which caller ID it dials
from, which prompt version runs, and which hours each kind of person gets called in.

Two numbers decide whether a workflow is healthy, and we never blend them:

- **Pickup rate**: of the dials, how many connected.
- **Engagement rate**: of the people who picked up, how many stayed to a conclusion (interested *or* not).

The business outcome sits above both: **leads concluded ÷ leads uploaded**.

We are moving from agents that humans tune to agents that **earn autonomy**. They notice what's working, try
better options on the right people, ask for permission in a form a busy supervisor can say yes to, apply
changes safely, and back out when something stops working. A client supervisor can see why every change was
made. Nothing changes on a live client's agent without passing the governance boundary.

The controller implements this loop against the simulator HTTP API.

## 2. The problem

The **controller** is an always-on service that, for every workflow on the platform, continuously:

1. **Detects**: pickup or engagement drifting, a caller ID going bad, hallucinations spiking. It works on
   thresholds and populations, never on single calls.
2. **Hypothesises and experiments**: it forms cohorts, splits traffic between variants, and decides how much
   evidence is enough.
3. **Proposes**: it packages a change with a summary, a diff, the affected leads, a dry run, an impact score
   and a rationale, then routes it through governance.
4. **Applies and learns**: it applies what's approved, keeps measuring, promotes winners and rolls back
   variants that degrade.
5. **Explains**: from your system's own records, anyone can answer *"why did workflow X change at 14:00 on
   D3, who approved it, and what happened next?"*

`sim/` contains a **closed-loop simulator** of the world: tenants,
workflows, leads, a dialler, people who pick up or don't, client approvers, client supervisors who edit things
mid-day, and an LLM that suggests prompt fixes. The controller drives it over HTTP. The simulator uses a reference model to represent which variants actually work, for whom, for how long, and what happens when you change
things. The API contract is documented in **[SIMULATOR.md](SIMULATOR.md)**.

## 3. Scale envelope

The target operating envelope is:

- **~2 crore dials/month**: about 6.5 lakh/day inside a 12-hour window, peaking above 50k dials/hour.
- Tens of tenants and hundreds of workflows, each with its own policy, objective and approver.
- Events arrive continuously. Your loop's decisions are only as fresh as its ingestion.

`scenarios/public_03_load.yaml` exercises 12 workflows at ~650k dials/day and ~850k events/day.
Measure ingest throughput, decision lag, memory, and the first bottleneck at 10x load.

## 4. What's in the box

```
sim/                      the closed-loop simulator
scenarios/
  public_01_dev.yaml      3 workflows, 8 days, no incidents — start here
  public_02_incidents.yaml  caller ID flagged, a supervisor ships a bad prompt, a supervisor edits mid-experiment
  public_03_load.yaml     12 workflows at full scale, 5 days
use_cases/                sales, compliance, operations, product and reliability requirements
examples/naive_controller.py   shows the API round-trip. It is deliberately bad.
docs/SIMULATOR.md         API, config, governance, events
DESIGN.md                 architecture, scope decisions and validation results
```

```bash
pip install -r requirements.txt
python -m sim baseline --scenario scenarios/public_01_dev.yaml --run-dir runs/base01
python -m sim serve    --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01     # :8700
./run.sh http://127.0.0.1:8700                                                        # start the controller
python -m sim grade --run runs/mine01 --baseline runs/base01 --out reports/public_01.md
```

**Simulator integration boundary**

- The controller uses only data exposed by the HTTP API, including event chunks referenced by it. It does
  not import simulator internals, read runtime logs directly, or parse scenario ground truth.
- `./run.sh <SIM_URL>` starts the controller and runs until the scenario finishes without manual steps.
- The target runtime is Linux with 4 vCPUs and 8 GB RAM. Dependencies are listed in `requirements.txt`.

## 5. Constraints that are not up for negotiation

These requirements apply to every workflow and take precedence over role-specific preferences.

1. **The governance boundary is absolute.** Every change your controller makes goes through `/approvals` or a
   policy-permitted auto-apply. Supervisor edits are a fact of life. You don't overwrite them silently.
2. **The business owns whether and when to call.** Cadence, dialling windows and capacity are out of scope.
   You decide *who* gets called in a given hour and *how* the agent shows up.
3. **Pickup and engagement stay separate KPIs.** Optimise a workflow's `primary` KPI and respect its
   `guardrail`.
4. **Winner selection is statistical and deterministic.** An LLM may propose what to try. It never decides
   what won.
5. **Act on populations, not anecdotes.** Detection is threshold-based. No change is triggered by one call.
6. **Every decision is auditable** from your own store. The simulator's logs don't count.
7. **Credits are finite.** LLM and validation calls cost credits. Running out mid-incident is a failure.
8. **Crash safety.** Kill your controller at any point, restart it, and it resumes. It must not double-apply,
   lose a pending approval, or forget what it learned.

## 6. Capability catalogue

Implementation status and scope decisions are recorded in [DESIGN.md](../DESIGN.md#2-scope-decisions).

| # | Capability |
|---|---|
| C1 | Event ingestion at load-scenario scale: backlog-safe, idempotent, restartable |
| C2 | Lead/feature joins and **one shared cohort object** used by detection, experiments and the supervisor view |
| C3 | Bootstrapping from history before D0 |
| C4 | Call-time optimisation: per cohort and/or per person |
| C5 | Caller-ID health: detect degradation, rotate, explore safely |
| C6 | Variant experimentation on welcome message and voice: design, minimum sample sizes, multiple comparisons |
| C7 | Winner promotion, **decay detection and rollback** |
| C8 | Hallucination incident response: detect → LLM suggest → validate → propose with evidence → verify after apply |
| C9 | Governance client: proposal lifecycle, batching under approver fatigue, timing against approver hours, expiry/lapse, stale-base re-proposal, auto-apply within policy |
| C10 | Honest impact scores, affected-lead counts, and dry runs against historical cohorts |
| C11 | A consolidated supervisor view (API or minimal UI): one place, not fifty alerts |
| C12 | A queryable decision/audit store: *"why did X change"* |
| C13 | Crash recovery, demonstrated: kill mid-run, restart, show the run completes correctly |
| C14 | Observability of the loop itself: ingest lag, decisions/hour, credit burn, alerts an on-call engineer can act on |
| C15 | Fair allocation of credits and approver attention across tenants |

## 7. Benchmark validation and use cases

The controller must complete the development and incident scenarios without manual steps, retain a
queryable audit trail, respect the constraints above, and recover correctly after a process kill.
The load scenario measures throughput, decision lag and memory; results are kept in `reports/`.

[use_cases/](../use_cases/) contains role-based technical briefs for Sales, Compliance, Platform/SRE, Operations,
Product and Architecture. Each case uses modelled workflow policies. Implementation choices and rationale
are documented in [DESIGN.md](../DESIGN.md#3-use-cases-and-policy-decisions).
