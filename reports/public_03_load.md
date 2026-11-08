# Grade report — public_03_load

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.398 | +16.5% | 0.261 | +7.1% | 0.537 | +7.3% | 50% | 410 (408) |
| arav_kyc_1 | 0.492 | +39.0% | 0.285 | +32.7% | 0.593 | -0.5% | 0% | 452 (370) |
| arav_renewal_0 | 0.470 | +16.9% | 0.284 | +7.5% | 0.585 | +7.7% | 37% | 466 (440) |
| kest_cancel_2 | 0.389 | +14.9% | 0.261 | +7.2% | 0.526 | +5.6% | 37% | 433 (411) |
| kest_kyc_1 | 0.453 | +28.4% | 0.263 | +22.8% | 0.596 | +0.3% | 0% | 452 (339) |
| kest_renewal_0 | 0.474 | +18.7% | 0.283 | +7.3% | 0.595 | +9.7% | 46% | 445 (433) |
| nort_cancel_2 | 0.388 | +14.7% | 0.261 | +7.8% | 0.525 | +5.3% | 39% | 408 (393) |
| nort_kyc_1 | 0.491 | +38.6% | 0.284 | +33.4% | 0.592 | -0.6% | 0% | 439 (323) |
| nort_renewal_0 | 0.470 | +16.1% | 0.281 | +6.7% | 0.592 | +8.1% | 43% | 446 (411) |
| tide_cancel_2 | 0.394 | +16.1% | 0.260 | +6.6% | 0.534 | +7.0% | 48% | 391 (407) |
| tide_kyc_1 | 0.450 | +27.2% | 0.261 | +21.7% | 0.594 | +0.4% | 0% | 432 (356) |
| tide_renewal_0 | 0.468 | +16.7% | 0.281 | +6.5% | 0.591 | +8.9% | 46% | 466 (438) |
| ALL | 0.445 | +21.8% | 0.272 | +13.2% | 0.572 | +5.3% | 27% | 5240 (4729) |

## Changes applied by the controller

57 applied — neutral: 18, beneficial: 38, harmful: 1

- nort_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- nort_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- kest_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- kest_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- arav_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- arav_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- tide_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- tide_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- nort_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.015 → **neutral**
- nort_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.023 → **beneficial**
- kest_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.015 → **neutral**
- kest_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.023 → **beneficial**
- arav_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.015 → **neutral**
- arav_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.023 → **beneficial**
- tide_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.015 → **neutral**
- tide_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.023 → **beneficial**
- kest_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.043 → **beneficial**
- arav_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.043 → **beneficial**
- tide_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.043 → **beneficial**
- nort_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.043 → **beneficial**
- nort_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- arav_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.003 didΔ=+0.000 → **neutral**
- tide_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.003 didΔ=+0.000 → **neutral**
- kest_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- kest_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.060 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.059 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.059 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.060 didΔ=+0.000 → **beneficial**
- nort_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.393 → **beneficial**
- nort_cancel_2 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.030 didΔ=+0.000 → **beneficial**
- arav_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.442 → **beneficial**
- kest_cancel_2 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.030 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.030 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.090 didΔ=+0.000 → **beneficial**
- arav_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.126 → **beneficial**
- kest_renewal_0 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.070 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.070 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v6 (controller_approved) dims=['voice'] engageΔ=+0.058 didΔ=+0.000 → **beneficial**
- kest_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.391 → **beneficial**
- nort_renewal_0 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.070 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v7 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.041 → **harmful**
- nort_renewal_0 v8 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.043 → **beneficial**
- nort_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.136 → **beneficial**
- tide_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.471 → **beneficial**
- nort_cancel_2 v6 (controller_approved) dims=['voice'] engageΔ=+0.058 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.071 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v6 (controller_approved) dims=['voice'] engageΔ=+0.058 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.138 → **beneficial**
- tide_cancel_2 v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**
- kest_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.142 → **beneficial**
- kest_cancel_2 v7 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v9 (controller_approved) dims=['voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- kest_renewal_0 v7 (controller_approved) dims=['cohorts', 'voice'] engageΔ=+0.027 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v7 (controller_approved) dims=['cohorts', 'voice'] engageΔ=+0.028 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v7 (controller_approved) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nort_cancel_2 v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 42,
  "decisions": {
    "approved": 38,
    "rejected": 3,
    "expired": 1
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 3
  },
  "applied_via_approval": 35,
  "auto_applied": 22,
  "auto_apply_rejected": 0,
  "apply_conflicts": 3,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nort_renewal_0": 2,
    "nort_kyc_1": 1,
    "nort_cancel_2": 2,
    "arav_renewal_0": 2,
    "arav_kyc_1": 1,
    "arav_cancel_2": 1,
    "tide_renewal_0": 2,
    "tide_kyc_1": 1,
    "tide_cancel_2": 3,
    "kest_renewal_0": 1,
    "kest_kyc_1": 1,
    "kest_cancel_2": 2
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nort_renewal_0:welcome_message=W3": {
    "3": 0.901,
    "4": 0.901
  },
  "kest_renewal_0:welcome_message=W3": {
    "3": 0.901,
    "4": 0.899
  },
  "arav_renewal_0:welcome_message=W3": {
    "3": 0.901,
    "4": 0.9
  },
  "tide_renewal_0:welcome_message=W3": {
    "3": 0.902,
    "4": 0.903
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0