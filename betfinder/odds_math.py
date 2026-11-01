"""Pure betting math: implied probability, de-vigging, expected value, Kelly sizing."""

from __future__ import annotations


def implied_prob(decimal_odds: float) -> float:
    """Raw implied probability of decimal odds (includes the bookmaker's margin)."""
    if decimal_odds <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0, got {decimal_odds}")
    return 1.0 / decimal_odds


def american_to_decimal(american: float) -> float:
    if american >= 100:
        return 1.0 + american / 100.0
    if american <= -100:
        return 1.0 + 100.0 / abs(american)
    raise ValueError(f"invalid American odds: {american}")


def decimal_to_american(decimal_odds: float) -> int:
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1.0) * 100)
    return round(-100.0 / (decimal_odds - 1.0))


def overround(decimal_odds: list[float]) -> float:
    """Bookmaker margin: sum of implied probabilities minus 1."""
    return sum(implied_prob(o) for o in decimal_odds) - 1.0


def devig_multiplicative(decimal_odds: list[float]) -> list[float]:
    """Remove the margin by scaling implied probabilities so they sum to 1."""
    raw = [implied_prob(o) for o in decimal_odds]
    total = sum(raw)
    return [p / total for p in raw]


def devig_power(decimal_odds: list[float], tol: float = 1e-10) -> list[float]:
    """Remove the margin with the power method: find k such that sum(p_i ** k) == 1.

    Puts more of the margin on longshots than the multiplicative method does,
    which matches how bookmakers usually price favourite-longshot bias.
    """
    raw = [implied_prob(o) for o in decimal_odds]
    if abs(sum(raw) - 1.0) < tol:
        return raw
    lo, hi = 0.5, 3.0
    for _ in range(200):
        k = (lo + hi) / 2
        s = sum(p ** k for p in raw)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    probs = [p ** k for p in raw]
    total = sum(probs)
    return [p / total for p in probs]


DEVIG_METHODS = {
    "multiplicative": devig_multiplicative,
    "power": devig_power,
}


def expected_value(fair_prob: float, decimal_odds: float) -> float:
    """Expected profit per 1 unit staked."""
    return fair_prob * decimal_odds - 1.0


def kelly_fraction(fair_prob: float, decimal_odds: float) -> float:
    """Full-Kelly fraction of bankroll. Returns 0 for non-positive edges."""
    b = decimal_odds - 1.0
    f = (fair_prob * b - (1.0 - fair_prob)) / b
    return max(0.0, f)


def arbitrage_stakes(decimal_odds: list[float], total_stake: float) -> tuple[float, list[float]]:
    """Return (guaranteed profit margin, stake per outcome) for a set of best prices.

    Margin is positive only if sum(1/odds) < 1, i.e. a true arbitrage.
    """
    inv = [implied_prob(o) for o in decimal_odds]
    s = sum(inv)
    stakes = [total_stake * p / s for p in inv]
    return 1.0 / s - 1.0, stakes
