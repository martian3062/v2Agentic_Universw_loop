"""Statistics. Pure functions, deterministic, stdlib only.

Experiments use the mixture sequential probability ratio test (mSPRT, Johari et al. 2017, as used for
always-valid A/B testing) for a difference of two proportions under a normal approximation. Its confidence
sequence is valid at every look, so evaluating hourly does not inflate the false-winner rate.

Detectors use one-sided z-scores of observed vs. expected counts with a high bar (|z| >= 4), a minimum
sample and a minimum effect size, so a single call or a small population can never trigger an action.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass
class Comparison:
    """treatment minus control."""
    n_t: int
    x_t: int
    n_c: int
    x_c: int
    delta: float
    se: float
    cs_low: float
    cs_high: float
    alpha: float

    @property
    def better(self) -> bool:
        return self.cs_low > 0

    @property
    def worse(self) -> bool:
        return self.cs_high < 0

    def as_dict(self):
        return {"n_t": self.n_t, "x_t": self.x_t, "rate_t": _r(self.x_t, self.n_t), "n_c": self.n_c,
                "x_c": self.x_c, "rate_c": _r(self.x_c, self.n_c), "delta": round(self.delta, 4),
                "se": round(self.se, 4), "cs": [round(self.cs_low, 4), round(self.cs_high, 4)],
                "alpha": self.alpha}


def _r(x, n):
    return round(x / n, 4) if n else None


def _var(x: int, n: int) -> float:
    # add-one smoothing keeps the variance positive when a rate is 0 or 1 on small n
    p = (x + 1) / (n + 2)
    return p * (1 - p) / max(n, 1)


def msprt(x_t: int, n_t: int, x_c: int, n_c: int, alpha: float, tau: float) -> Comparison:
    """Always-valid (1 - alpha) confidence sequence for p_t - p_c.

    With V the variance of the estimated difference and a N(0, tau^2) mixture over the effect, the mSPRT
    rejects theta0 when (d - theta0)^2 >= V(V+tau^2)/tau^2 * (2 log(1/alpha) + log((V+tau^2)/V)),
    so that bound is the half-width of the confidence sequence.
    """
    if n_t == 0 or n_c == 0:
        return Comparison(n_t, x_t, n_c, x_c, 0.0, float("inf"), -1.0, 1.0, alpha)
    d = x_t / n_t - x_c / n_c
    v = _var(x_t, n_t) + _var(x_c, n_c)
    t2 = tau * tau
    half = math.sqrt(v * (v + t2) / t2 * (2 * math.log(1 / alpha) + math.log((v + t2) / v)))
    return Comparison(n_t, x_t, n_c, x_c, d, math.sqrt(v), d - half, d + half, alpha)


def _mh(strata) -> tuple[int, int, int, int, float, float] | None:
    """Mantel-Haenszel risk difference over strata [(x_t, n_t, x_c, n_c), ...] and its variance.

    Arms are compared only within a stratum (a config version and day, where assignment probabilities are
    constant) and then combined with weights n_t*n_c/(n_t+n_c). Pooling first is biased whenever the allocation
    changed while the rate moved over time (Simpson's paradox). Strata missing either arm carry no comparison."""
    used = [(xt, nt, xc, nc) for xt, nt, xc, nc in strata if nt > 0 and nc > 0]
    if not used:
        return None
    sw = num = var = 0.0
    for xt, nt, xc, nc in used:
        w = nt * nc / (nt + nc)
        sw += w
        num += w * (xt / nt - xc / nc)
        var += w * w * (_var(xt, nt) + _var(xc, nc))
    tx, tn = sum(s[0] for s in used), sum(s[1] for s in used)
    cx, cn = sum(s[2] for s in used), sum(s[3] for s in used)
    return tx, tn, cx, cn, num / sw, var / (sw * sw)


def msprt_strat(strata, alpha: float, tau: float) -> Comparison:
    """Always-valid confidence sequence (same normal-mixture boundary as msprt) for the stratified difference."""
    r = _mh(strata)
    if r is None:
        return Comparison(0, 0, 0, 0, 0.0, float("inf"), -1.0, 1.0, alpha)
    tx, tn, cx, cn, d, v = r
    t2 = tau * tau
    half = math.sqrt(v * (v + t2) / t2 * (2 * math.log(1 / alpha) + math.log((v + t2) / v)))
    return Comparison(tn, tx, cn, cx, d, math.sqrt(v), d - half, d + half, alpha)


def fixed_strat(strata, alpha: float) -> Comparison:
    r = _mh(strata)
    if r is None:
        return Comparison(0, 0, 0, 0, 0.0, float("inf"), -1.0, 1.0, alpha)
    tx, tn, cx, cn, d, v = r
    h = z_crit(alpha) * math.sqrt(v)
    return Comparison(tn, tx, cn, cx, d, math.sqrt(v), d - h, d + h, alpha)


def fixed_z(x_t: int, n_t: int, x_c: int, n_c: int) -> float:
    """Fixed-sample z for p_t - p_c (used for interaction checks and for drift, where we want a point test)."""
    if n_t == 0 or n_c == 0:
        return 0.0
    v = _var(x_t, n_t) + _var(x_c, n_c)
    return (x_t / n_t - x_c / n_c) / math.sqrt(v)


def count_z(observed: float, expected: float, variance: float) -> float:
    """z of an observed count against its expected value under the reference rates."""
    if variance <= 0:
        return 0.0
    return (observed - expected) / math.sqrt(variance)


def binom_sf(k: int, n: int, p: float) -> float:
    """Exact P(X >= k) for X ~ Binomial(n, p). Small counts (a handful of flags) make the normal
    approximation badly anti-conservative in the upper tail, so detectors use this instead."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    lp, lq = math.log(p), math.log1p(-p)
    terms = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
             for i in range(k, n + 1)]
    m = max(terms)
    return min(1.0, math.exp(m) * sum(math.exp(t - m) for t in terms))


def z_crit(alpha_two_sided: float) -> float:
    """Inverse normal for a two-sided level, by bisection (stdlib only)."""
    target = 1 - alpha_two_sided / 2
    lo, hi = 0.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if phi(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def fixed_test(x_t: int, n_t: int, x_c: int, n_c: int, alpha: float) -> Comparison:
    """Fixed-sample two-sided test at level alpha, for a single pre-planned look (segment analysis at the end
    of an experiment). Returned in the same shape as msprt so callers can treat them alike."""
    if n_t == 0 or n_c == 0:
        return Comparison(n_t, x_t, n_c, x_c, 0.0, float("inf"), -1.0, 1.0, alpha)
    d = x_t / n_t - x_c / n_c
    se = math.sqrt(_var(x_t, n_t) + _var(x_c, n_c))
    h = z_crit(alpha) * se
    return Comparison(n_t, x_t, n_c, x_c, d, se, d - h, d + h, alpha)


def dunnett_control_share(k: int) -> float:
    """Square-root rule: with k challengers each compared with one control, giving the control sqrt(k) times a
    challenger's share minimises the variance of the k comparisons."""
    return math.sqrt(k) / (k + math.sqrt(k))


def wilson(x: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = x / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def required_n(p: float, effect: float, alpha: float, tau: float, ratio: float = 1.0) -> int:
    """Smallest per-arm n at which an effect of `effect` would be declared by the confidence sequence
    (point estimate exactly at the true effect). ratio = n_control / n_treatment. Used for planning text."""
    lo, hi = 10, 10_000_000
    while lo < hi:
        mid = (lo + hi) // 2
        v = p * (1 - p) / mid + p * (1 - p) / (mid * ratio)
        t2 = tau * tau
        half = math.sqrt(v * (v + t2) / t2 * (2 * math.log(1 / alpha) + math.log((v + t2) / v)))
        if half < effect:
            hi = mid
        else:
            lo = mid + 1
    return lo
