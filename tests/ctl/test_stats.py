"""Statistical properties the design relies on."""
import math

import numpy as np
import pytest

from controller.stats import binom_sf, dunnett_control_share, fixed_test, msprt, z_crit
from controller.util import normalise, round_dist


def family_false_claim_rate(p, k, trials, looks, per_look, gate, alpha=0.05, tau=0.05, seed=1, test=msprt):
    """A/A experiments (every arm has the same true rate p), k challengers at the square-root split, looked at
    after every batch. Returns the share of experiments in which ANY comparison ever declared a difference."""
    rng = np.random.default_rng(seed)
    cs = dunnett_control_share(k)
    nc_l, nt_l = max(1, round(per_look * cs)), max(1, round(per_look * (1 - cs) / k))
    hits = 0
    for _ in range(trials):
        xc = np.cumsum(rng.binomial(nc_l, p, looks))
        xt = np.cumsum(rng.binomial(nt_l, p, (k, looks)), axis=1)
        hit = False
        for t in range(looks):
            nc, nt = nc_l * (t + 1), nt_l * (t + 1)
            if min(nc, nt) < gate:
                continue
            for j in range(k):
                c = test(int(xt[j, t]), nt, int(xc[t]), nc, alpha / k, tau) if test is msprt else \
                    test(int(xt[j, t]), nt, int(xc[t]), nc, alpha / k)
                if c.better or c.worse:
                    hit = True
                    break
            if hit:
                break
        hits += hit
    return hits / trials


@pytest.mark.parametrize("p", [0.05, 0.25, 0.5])
@pytest.mark.parametrize("k", [1, 3])
@pytest.mark.parametrize("gate", [0, 300])
def test_family_false_claim_rate_under_repeated_looks(p, k, gate):
    """The guarantee is asymptotic (normal approximation, plug-in variances), so it is checked, not assumed:
    across base rates, arm counts and with or without the MIN_ARM_N gate, the family-wise rate of false claims
    under 120 looks stays within alpha (measured 0-2%)."""
    assert family_false_claim_rate(p, k, trials=300, looks=120, per_look=120 if gate else 30, gate=gate) <= 0.05


def test_long_horizon_approaches_alpha_but_stays_below():
    """The limit of the claim: over very long horizons (1,000 looks, ~60k trials per arm) the rate climbs toward
    alpha (measured ~3.5%), as an always-valid sequence should; it does not exceed it."""
    r = family_false_claim_rate(0.25, 1, trials=300, looks=1000, per_look=120, gate=300)
    assert 0.01 < r <= 0.05


def test_peeking_with_a_fixed_sample_test_is_not_valid():
    """Why sequences are used at all: the same A/A data, tested with a fixed-sample test at every look."""
    assert family_false_claim_rate(0.5, 1, trials=300, looks=100, per_look=50, gate=0, test=fixed_test) > 0.10


def test_segment_family_holds_under_repeated_looks():
    """Segment claims: 13 segments x 3 challengers = 39 comparisons, always-valid at 0.05/39, looked at every
    batch (interim AND conclusion, and more). The family-wise false-claim rate stays within 0.05."""
    rng = np.random.default_rng(3)
    m, trials, looks, hits = 39, 200, 40, 0
    for _ in range(trials):
        xt = np.cumsum(rng.binomial(25, 0.5, (m, looks)), axis=1)
        xc = np.cumsum(rng.binomial(40, 0.5, (m, looks)), axis=1)
        hit = any((c := msprt(int(xt[j, t]), 25 * (t + 1), int(xc[j, t]), 40 * (t + 1), 0.05 / m, 0.05)).better
                  or c.worse for t in range(8, looks) for j in range(m))
        hits += hit
    assert hits / trials <= 0.05


def test_msprt_detects_a_real_effect():
    c = msprt(1300, 2000, 1100, 2000, 0.025, 0.05)   # 65% vs 55%
    assert c.better and c.cs_low > 0.03


def test_binom_sf_matches_brute_force():
    n, p = 30, 0.07
    for k in (0, 1, 5, 12, 30):
        brute = sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))
        assert abs(binom_sf(k, n, p) - brute) < 1e-12


def test_hallucination_threshold_rejects_small_count_noise():
    # 11 flags in 227 calls at a 1.33% base rate: z~4 under a normal approximation, but p~3e-4 exactly
    assert binom_sf(11, 227, 0.0133) > 1e-5
    assert binom_sf(24, 240, 0.0133) < 1e-10


def test_z_crit_and_dunnett():
    assert abs(z_crit(0.05) - 1.96) < 1e-3
    assert abs(dunnett_control_share(1) - 0.5) < 1e-9
    assert 0.36 < dunnett_control_share(3) < 0.37


def test_round_dist_is_stable_under_renormalisation():
    for k in range(1, 6):
        cs = dunnett_control_share(k)
        d = round_dist({"a": cs, **{f"b{i}": (1 - cs) / k for i in range(k)}})
        assert sum(d.values()) == 1.0
        assert normalise(d) == d
