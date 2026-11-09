"""Convert a sharp book's player-prop price to a nearby line for the same player.

When Pinnacle prices "Giannis Over 11.5 rebounds" and a book offers "Over 12.5", the
difference is the chance he lands on exactly 12, which can be 10% or more. The stat is
modelled as a discrete distribution calibrated to Pinnacle's de-vigged price at its line:

* counts (rebounds, assists, threes, receptions, shots, NHL points): Poisson
* larger stats (NBA points, PRA, yards): normal on whole numbers, with a spread that
  grows with the line

The fair price on any nearby line, with pushes on whole numbers, is read off that model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

COUNT_STATS = {
    "player_rebounds", "player_assists", "player_threes", "player_blocks", "player_steals",
    "player_receptions", "player_shots_on_goal", "player_goals", "player_blocked_shots",
    "player_power_play_points", "player_pass_tds", "player_rush_attempts",
}

# Standard deviation of the stat around its median, as a function of the line.
NORMAL_SD: dict[str, Callable[[float], float]] = {
    "player_points": lambda line: 1.4 * math.sqrt(max(line, 1.0)),
    "player_points_rebounds_assists": lambda line: 1.5 * math.sqrt(max(line, 1.0)),
    "player_points_rebounds": lambda line: 1.45 * math.sqrt(max(line, 1.0)),
    "player_points_assists": lambda line: 1.45 * math.sqrt(max(line, 1.0)),
    "player_rebounds_assists": lambda line: 1.3 * math.sqrt(max(line, 1.0)),
    "player_pass_yds": lambda line: 0.25 * line + 10,
    "player_rush_yds": lambda line: 0.55 * line + 5,
    "player_reception_yds": lambda line: 0.65 * line + 5,
}

MAX_COUNT_SHIFT = 2.0  # lines further apart than this are not converted
MAX_NORMAL_SHIFT_SD = 0.75  # ... or this many standard deviations for normal stats


def _kind(sport_key: str, market: str) -> str | None:
    if market in COUNT_STATS or (sport_key.startswith("icehockey") and market == "player_points"):
        return "count"
    if market in NORMAL_SD:
        return "normal"
    return None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class PropModel:
    kind: str
    param: float  # Poisson mean, or normal centre
    sd: float
    source: str

    def cdf(self, k: int) -> float:
        """P(stat <= k)."""
        if k < 0:
            return 0.0
        if self.kind == "count":
            lam, term, total = self.param, math.exp(-self.param), 0.0
            for i in range(k + 1):
                if i:
                    term *= lam / i
                total += term
            return min(total, 1.0)
        return _norm_cdf((k + 0.5 - self.param) / self.sd)

    def wpl(self, side: str, line: float) -> tuple[float, float, float]:
        """(win, push, lose) for Over/Under `line`."""
        if abs(line - round(line)) < 1e-9:
            k = round(line)
            below, push = self.cdf(k - 1), self.cdf(k) - self.cdf(k - 1)
        else:
            below, push = self.cdf(math.floor(line)), 0.0
        above = 1.0 - below - push
        return (above, push, below) if side == "Over" else (below, push, above)

    def fair_odds(self, side: str, line: float) -> float:
        win, _, lose = self.wpl(side, line)
        return 1.0 + lose / win if win > 0 else float("inf")

    def ev(self, side: str, line: float, odds: float) -> float:
        win, _, lose = self.wpl(side, line)
        return win * (odds - 1.0) - lose


def _calibrate(kind: str, market: str, line: float, over: float, under: float, source: str) -> PropModel:
    # Pinnacle's share of Over among Over+Under after removing the margin.
    target = (1.0 / over) / (1.0 / over + 1.0 / under)
    sd = NORMAL_SD[market](line) if kind == "normal" else 0.0
    lo, hi = (0.01, 100.0) if kind == "count" else (line - 6 * sd, line + 6 * sd)
    for _ in range(100):
        mid = (lo + hi) / 2
        win, _, lose = PropModel(kind, mid, sd, source).wpl("Over", line)
        share = win / (win + lose) if win + lose > 0 else 0.0
        if share < target:
            lo = mid
        else:
            hi = mid
    return PropModel(kind, (lo + hi) / 2, sd, source)


def model_for(
    sport_key: str, market: str, sharp_lines: dict[float, dict[str, float]], line: float, sharp: str
) -> PropModel | None:
    """Model from the sharp line nearest to `line`, or None if none is close enough."""
    kind = _kind(sport_key, market)
    if kind is None or not sharp_lines:
        return None
    best = min(sharp_lines, key=lambda p: abs(p - line))
    prices = sharp_lines[best]
    over = next((v for k, v in prices.items() if k.split(" ")[-2:-1] == ["Over"]), None)
    under = next((v for k, v in prices.items() if k.split(" ")[-2:-1] == ["Under"]), None)
    if over is None or under is None:
        return None
    if kind == "count":
        if abs(best - line) > MAX_COUNT_SHIFT:
            return None
    elif abs(best - line) > MAX_NORMAL_SHIFT_SD * NORMAL_SD[market](best):
        return None
    return _calibrate(kind, market, best, over, under, f"{sharp} {best:g}, converted")
