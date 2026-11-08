# Crash-recovery test: public_02_incidents

`python scripts/crash_test.py scenarios/public_02_incidents.yaml --kills 8 --seed 1`

The controller was started 9 times and hard-killed 8 times (SIGKILL / TerminateProcess: no shutdown code runs).

## Kills

- kill #1: during history bootstrap (world at D0 00:00)
- kill #2: with an approved change not yet applied (world at D1 14:15)
- kill #3: with an approved change not yet applied (world at D1 14:30)
- kill #4: at D1 20:30 (world at D1 20:30)
- kill #5: at D6 11:30 (world at D6 11:30)
- kill #6: at D8 13:45 (world at D8 13:45)
- kill #7: at D9 03:00 (world at D9 03:00)
- kill #8: at D9 11:00 (world at D9 11:00)

## Invariants after the run

| check | result | detail |
|---|---|---|
| scenario finished | PASS | D10 00:00 |
| controller's final run exited cleanly | PASS | exit code 0 |
| no proposal applied twice | PASS | 18 controller changes, duplicates: none |
| every controller change is in our audit store | PASS | missing: none |
| every approval reconciled, none lost | PASS | 13 approvals; mismatches: none |
| no approved change lapsed | PASS | lapsed: none |
| every chunk ingested exactly once | PASS | 492 chunks listed, 492 ingested |

## Outcome of the crashed run (grader)

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.369 | +48.9% | 0.248 | +34.8% | 0.510 | +7.0% | 38% | 81 (330) |
| nw_kyc | 0.479 | +27.7% | 0.303 | +25.2% | 0.593 | +0.2% | 0% | 39 (31) |
| nw_renewal | 0.482 | +13.0% | 0.286 | +9.3% | 0.562 | +2.6% | 17% | 50 (55) |
| ALL | 0.430 | +28.6% | 0.272 | +22.4% | 0.547 | +3.2% | 27% | 170 (416) |

Full grade: `reports\crash_public_02_incidents_grade.md`
