# Crash-recovery test: public_02_incidents

`python scripts/crash_test.py scenarios/public_02_incidents.yaml --kills 8 --seed 1`

The controller was started 9 times and hard-killed 8 times (SIGKILL / TerminateProcess: no shutdown code runs).

## Kills

- kill #1: during history bootstrap (world at D0 00:00)
- kill #2: with an approved change not yet applied (world at D0 12:45)
- kill #3: with an approved change not yet applied (world at D0 12:45)
- kill #4: at D1 20:30 (world at D1 20:30)
- kill #5: at D6 11:30 (world at D6 11:30)
- kill #6: at D8 13:45 (world at D8 13:45)
- kill #7: at D9 02:00 (world at D9 02:00)
- kill #8: at D9 10:45 (world at D9 10:45)

## Invariants after the run

| check | result | detail |
|---|---|---|
| scenario finished | PASS | D10 00:00 |
| controller's final run exited cleanly | PASS | exit code 0 |
| no proposal applied twice | PASS | 14 controller changes, duplicates: none |
| every controller change is in our audit store | PASS | missing: none |
| every approval reconciled, none lost | PASS | 8 approvals; mismatches: none |
| no approved change lapsed | PASS | lapsed: none |
| every chunk ingested exactly once | PASS | 492 chunks listed, 492 ingested |

## Outcome of the crashed run (grader)

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.353 | +42.7% | 0.247 | +34.5% | 0.494 | +3.7% | 35% | 80 (330) |
| nw_kyc | 0.506 | +35.0% | 0.319 | +31.6% | 0.596 | +0.6% | 0% | 31 (31) |
| nw_renewal | 0.474 | +11.2% | 0.288 | +9.8% | 0.551 | +0.6% | 8% | 55 (55) |
| ALL | 0.426 | +27.4% | 0.275 | +23.8% | 0.537 | +1.4% | 21% | 166 (416) |

Full grade: `reports\crash_public_02_incidents_grade.md`
