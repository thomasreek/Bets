"""Convert a sharp book's spread or total to a fair price on a nearby line.

Pinnacle may price a hockey total at 7 while Novig offers 6.5, or a college spread at
-29.5 while a book offers -30. To compare them, the final total (or home margin) is
modelled as a discretised normal distribution, centred so that it reproduces the sharp
book's de-vigged price at the sharp line. The fair price on any nearby line, including
pushes on whole numbers and split (quarter) Asian lines, is then read off that model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Typical spread of the final result around the market line, per sport and market.
# Sports or markets not listed are never converted.
SIGMA: dict[str, dict[str, float]] = {
    "basketball_nba": {"totals": 18.0, "spreads": 12.0},
    "basketball_wnba": {"totals": 16.0, "spreads": 11.0},
    "basketball_ncaab": {"totals": 16.0, "spreads": 11.0},
    "basketball_wncaab": {"totals": 15.0, "spreads": 11.0},
    "americanfootball_nfl": {"totals": 13.5, "spreads": 13.5},
    "americanfootball_ncaaf": {"totals": 16.0, "spreads": 15.0},
    "icehockey_nhl": {"totals": 2.3},
    "baseball_mlb": {"totals": 4.3},
    "soccer": {"totals": 1.6, "spreads": 1.75},
}

# How far (in points/goals) a line may be from the sharp line and still be converted.
# Football is kept tight because margins cluster on key numbers (3, 7) the model ignores.
MAX_SHIFT: dict[str, float] = {
    "basketball": 2.0,
    "americanfootball": 1.0,
    "icehockey": 1.0,
    "baseball": 1.0,
    "soccer": 0.5,
}


def _params(sport_key: str, market: str) -> tuple[float, float] | None:
    table = SIGMA.get(sport_key) or SIGMA.get(sport_key.split("_")[0])
    if not table or market not in table:
        return None
    shift = MAX_SHIFT.get(sport_key.split("_")[0])
    if shift is None:
        return None
    return table[market], shift


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class _Bet:
    """A bet that settles on r = sign * X + offset: win if r > 0, push if r == 0."""

    sign: int
    offset: float

    def halves(self) -> list[float]:
        quarters = round(self.offset * 4)
        if quarters % 2:  # quarter line: half the stake on each neighbouring line
            return [self.offset - 0.25, self.offset + 0.25]
        return [self.offset]


def _wpl(bet: _Bet, mu: float, sigma: float) -> list[tuple[float, float, float]]:
    """(win, push, lose) probabilities for each half of the bet."""
    out = []
    for c in bet.halves():
        # Result r = sign * X + c with X integer. Threshold t: X compared with -sign * c.
        t = -bet.sign * c
        if abs(t - round(t)) < 1e-9:  # whole number: exact hit is a push
            k = round(t)
            p_push = _cdf((k + 0.5 - mu) / sigma) - _cdf((k - 0.5 - mu) / sigma)
            p_below = _cdf((k - 0.5 - mu) / sigma)
        else:
            p_push = 0.0
            p_below = _cdf((math.floor(t) + 0.5 - mu) / sigma)
        p_above = 1.0 - p_below - p_push
        win, lose = (p_above, p_below) if bet.sign > 0 else (p_below, p_above)
        out.append((win, p_push, lose))
    return out


def fair_odds(bet: _Bet, mu: float, sigma: float) -> float:
    parts = _wpl(bet, mu, sigma)
    win = sum(w for w, _, _ in parts)
    lose = sum(l for _, _, l in parts)
    return 1.0 + lose / win if win > 0 else float("inf")


def expected_value(bet: _Bet, mu: float, sigma: float, odds: float) -> float:
    parts = _wpl(bet, mu, sigma)
    return sum(w * (odds - 1.0) - l for w, _, l in parts) / len(parts)


def parse_bet(market: str, selection: str, home_team: str) -> _Bet | None:
    """Turn 'Over 6.5', 'Under 7', 'Houston Cougars -29.5' into a _Bet on X.

    X is the total for totals, and home score minus away score for spreads.
    """
    side, _, point = selection.rpartition(" ")
    try:
        line = float(point)
    except ValueError:
        return None
    if market == "totals":
        if side == "Over":
            return _Bet(1, -line)
        if side == "Under":
            return _Bet(-1, line)
        return None
    if market == "spreads":
        return _Bet(1, line) if side == home_team else _Bet(-1, line)
    return None


class LineModel:
    """Distribution of X calibrated to one sharp two-way price."""

    def __init__(self, mu: float, sigma: float, source: str):
        self.mu, self.sigma, self.source = mu, sigma, source

    def fair_odds(self, bet: _Bet) -> float:
        return fair_odds(bet, self.mu, self.sigma)

    def ev(self, bet: _Bet, odds: float) -> float:
        return expected_value(bet, self.mu, self.sigma, odds)


def calibrate(
    sport_key: str, market: str, home_team: str, sharp_prices: dict[str, float], source: str
) -> LineModel | None:
    """Fit the model so it reproduces the sharp book's de-vigged price."""
    params = _params(sport_key, market)
    if not params or len(sharp_prices) != 2:
        return None
    sigma, _ = params
    (sel_a, odds_a), (sel_b, odds_b) = sharp_prices.items()
    inv_a, inv_b = 1.0 / odds_a, 1.0 / odds_b
    target = 1.0 + inv_b / inv_a  # fair odds of side A after removing the margin
    bet = parse_bet(market, sel_a, home_team)
    if bet is None:
        return None
    # A's fair odds fall monotonically as mu moves in A's favour.
    centre = -bet.sign * bet.offset
    lo, hi = centre - 6 * sigma, centre + 6 * sigma
    for _ in range(100):
        mid = (lo + hi) / 2
        odds = fair_odds(bet, mid, sigma)
        if (odds > target) == (bet.sign > 0):
            lo = mid
        else:
            hi = mid
    return LineModel((lo + hi) / 2, sigma, source)


def max_shift(sport_key: str, market: str) -> float | None:
    params = _params(sport_key, market)
    return params[1] if params else None
