"""Every tunable in one place, with the reason for its value.

Thresholds are chosen from the base rates the API exposes (history: ~25-27% pickup, ~50-59% engagement,
~1.4% hallucination flags per connected call, 500-1200 connected calls per workflow per day) and from the
false-alarm budget we are willing to pay. Nothing here is read from scenario files.
"""
from dataclasses import dataclass, field

# ---------------------------------------------------------------- loop / time
TICK_ACTIVE = 15          # minutes per advance while anyone (dialler, approver) is working
TICK_IDLE = 60            # minutes per advance at night; nothing dials, nobody approves
EXPERIMENT_EVAL_EVERY = 60  # minutes; anytime-valid tests make the cadence a cost question, not a validity one

# ---------------------------------------------------------------- caller-ID health
CID_WINDOW_HOURS = 2      # look-back; a spam flag shows up within an hour at 100+ dials/hour per DID
CID_SLOW_WINDOW_HOURS = 12 # slow window: power for numbers that are bad from the start (one dialling day)
CID_MIN_DIALS = 150       # per DID in the window before we judge it
CID_SEVERE_REL = 0.6      # pickup at <= 60% of expected looks like spam labelling, not volume fatigue: such a
                          # number is never re-admitted automatically; a human has to clear it with the carrier
CID_Z = 4.5               # one-sided, p~3e-6 per look; ~5k looks per run (DIDs x workflows x ticks) -> <2% runs
CID_MIN_REL_DROP = 0.25   # and the drop must matter: observed <= 75% of expected connects
CID_STEP_COOLDOWN = 30    # minutes between auto-apply steps while mitigating, so each step is re-checked
# Spread policy: a number's reputation degrades with its volume (carrier spam labelling), so volume is spread
# evenly over every healthy number and no number is concentrated on. A degraded number rests, then is
# re-admitted on probation at a small share; if it degrades again it rests twice as long.
CID_SPREAD_MIN_TV = 0.05  # re-spread when the allocation is this far from target (a 10% probe counts)
CID_REST_HOURS = 24
CID_QUIET_DIALS = 50      # a resting number must have had at most this many dials over the rest period
CID_PROBE_SHARE = 0.10
CID_PROBATION_MIN_REL = 0.85  # to graduate, a number on probation must reach >= 85% of expected pickup (or z > -2)

# ---------------------------------------------------------------- hallucination incidents
HAL_WINDOW_HOURS = 4
HAL_MIN_CONNECTED = 200
HAL_MIN_FLAGS = 10
HAL_P = 1e-5              # exact binomial upper tail vs the history flag rate; ~1% chance of a false incident per run
HAL_P_AFTER_CHANGE = 1e-4 # level for the since-the-prompt-change window (<= 48 looks per change)
HAL_MIN_RATIO = 2.5       # and the flag rate must be >= 2.5x the historical rate (detector noise ~1.4%)
HAL_Z = 4.0               # kept for post-fix verification (flags still elevated)
HAL_CREDIT_CEILING = 25   # per incident: 1 suggest (3) + up to 5 validations (4 each)
HAL_VALIDATION_N = 400    # 4 credits; SE of pass_rate at 0.99 is 0.005, well clear of a 0.97 bar
HAL_VALIDATION_MARGIN = 0.005  # require pass_rate >= min + margin so a noisy pass isn't the evidence
HAL_VERIFY_MIN_CONNECTED = 300
HAL_VERIFY_MAX_CONNECTED = 1200  # an inconclusive engagement drop keeps verification open up to this many calls
# A prompt fix may not cost more engagement than the workflow allows: its own engagement_rate_max_drop when engagement
# is the guardrail. When engagement is the primary KPI nothing is configured; 2 pp is the margin every public policy
# uses for its guardrail, so we hold the primary to the same bar.
PRIMARY_ENGAGEMENT_MAX_DROP = 0.02
HAL_VERIFY_HOURS = 3
CREDIT_RESERVE = 30       # never spend below this except to resolve an open incident's validation
# Stakeholder 04 (Kestrel ops): a vendor "fix" that read the compliance script verbatim killed
# engagement. We rank such candidates last; they are still used if nothing else passes validation.
DISFAVOURED_FIX_WORDS = ("verbatim", "word for word", "word-for-word", "strict")

# ---------------------------------------------------------------- experiments
ALPHA = 0.05              # family-wise per experiment; Bonferroni across challengers
TAU = 0.05                # mSPRT mixing sd: the effect size we expect to matter (5 pp)
# incumbent share during an experiment comes from the square-root rule (stats.dunnett_control_share):
# 41% with 2 challengers, 37% with 3. That is both the most powerful split and a cap on exposure.
HOLDOUT_SHARE = 0.1       # incumbent share kept after promotion, to tell decay from a global shift
MIN_ARM_N = 300           # connected calls (engagement) / dials (pickup) per arm before any decision
INTERIM_DAYS = 4          # planned interim: segment analysis, drop arms estimated worse, keep testing the rest
FUTILITY_EFFECT = 0.02    # stop when no challenger can plausibly beat control by this much
RECENT_HOURS = 24         # a winner must still be winning in the last day before we promote it
RECENT_HARM_ALPHA = 0.002 # an arm significantly worse over the last day at this level is dropped mid-test
DECAY_WINDOW_HOURS = 24
DECAY_MIN_N = 300
DECAY_CUSUM_H = 4.6       # drift-adjusted Bernoulli CUSUM threshold, log(100): H0 = holdout + promoted lift,
                          # H1 = holdout (the lift is gone)
DECAY_SELF_Z = 2.0        # and the winner's own last-24h rate must be significantly below its promotion-time rate
EXPERIMENT_DIMS_BY_PRIMARY = {
    # A welcome message or voice is heard after the call connects, so it cannot move pickup. Pickup-primary
    # workflows are served by the caller-ID spread policy (C5); call-time optimisation (C4) is out of scope.
    "engagement_rate": ("welcome_message", "voice"),
    "pickup_rate": (),
}
SUPERVISOR_RESPECT_HOURS = 7 * 24  # a supervisor's manual choice on a dimension is a decision: we stop
                                   # experimenting on that dimension for a week (stakeholders/04)
SEGMENT_FEATURES = ("lang", "city_tier", "age_band", "value_band")
SEGMENT_MIN_ARM_N = 200   # per arm inside a segment before the segment is analysed at all
SEGMENT_ALPHA = 0.05      # family-wise level for all segment claims of one experiment (always-valid, Bonferroni over
                          # every segment value x every original challenger). With ALPHA for the workflow-level
                          # claim, one experiment's total false-claim rate is <= ALPHA + SEGMENT_ALPHA (union bound)
MAX_CARVED_COHORTS = 3    # per experiment; all from one feature so carved cohorts never overlap

# ---------------------------------------------------------------- governance
CUSTOMER_FACING_DIMS = {"welcome_message", "voice", "prompt_version", "cohorts"}
OPERATIONAL_DIMS = {"caller_id", "schedule"}
URGENT_QUOTA_RESERVE = 1  # requests per 24h kept free for incidents
REJECTION_COOLDOWN_HOURS = 24   # before a dimension is tested again after an experiment ends
# After a judgement-call rejection we re-decide on fresh data, sooner for changes that reduce exposure.
RETRY_AFTER_REJECTION_MIN = {"experiment_start": 12 * 60, "experiment_promote": 4 * 60,
                             "experiment_drop_arm": 60, "experiment_stop": 60, "experiment_rollback": 60}
MAX_ATTEMPTS_PER_INTENT = 3

# ---------------------------------------------------------------- impact score
IMPACT_SCALE_LEADS_PER_DAY = 50.0   # impact = 1 - exp(-conservative extra concluded leads per day / 50)

# ---------------------------------------------------------------- alerts (C14)
PAGE_INCIDENT_OPEN_HOURS = 6
WALL_SECONDS_PER_SIM_DAY_BUDGET = 600   # SRE: one load-scenario day in < 10 wall-clock minutes


@dataclass
class TenantPreference:
    """What a client asked for that is stricter than its signed policy. Stricter always wins."""
    earliest_request_hour: int | None = None
    max_requests_per_day: int | None = None
    # every customer-facing change (welcome, voice, prompt, cohorts) needs a named human approval, even a rollback
    # that only returns traffic to what a human approved before, and even where the policy would allow auto-apply
    human_approval_for_customer_facing: bool = False
    notes: list[str] = field(default_factory=list)


# Keyed by tenant_id. Unknown tenants get policy only, plus the global rule that customer-facing changes
# go through a human approver.
TENANT_PREFERENCES = {
    "kestrel_finance": TenantPreference(
        earliest_request_hour=11, max_requests_per_day=2,
        notes=["stakeholders/04: never before 11:00 (standups), two requests a day maximum"]),
    "northwind_life": TenantPreference(
        human_approval_for_customer_facing=True,
        notes=["stakeholders/02: named human approval for anything a customer hears, rollbacks included"]),
}
