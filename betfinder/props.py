"""Price a player prop on a line Pinnacle does not offer, using the market's own ladder.

Pinnacle sets the level: its de-vigged chance of Over at its own line. Other books' alternate
lines set the shape: for each book that prices the player at both Pinnacle's line and the
target line, the change in its de-vigged Over chance between the two lines is measured, and
the average change is added to Pinnacle's level.

A fixed distribution (Poisson, normal) turned out too narrow for real player stats and made
"easier" lines look far better than they are, so it is no longer used.
"""

from __future__ import annotations

from .odds_math import devig_power

MIN_LADDER_BOOKS = 2
# Ladders are only trusted near the middle; tails are distorted by long-shot pricing.
MIN_PROB, MAX_PROB = 0.33, 0.67


def max_distance(sharp_line: float) -> float:
    """How far from Pinnacle's line a prop may be priced (1 for counts, ~12% for bigger stats)."""
    return max(1.0, 0.12 * abs(sharp_line))


def _over_share(prices: dict[str, float]) -> float | None:
    """De-vigged chance of Over from a book's Over/Under prices on one line."""
    over = next((v for k, v in prices.items() if " Over " in f" {k} "), None)
    under = next((v for k, v in prices.items() if " Under " in f" {k} "), None)
    if not over or not under or over <= 1.0 or under <= 1.0:
        return None
    if 1.0 / over + 1.0 / under > 1.15:  # broken or very wide line
        return None
    return devig_power([over, under])[0]


def ladder_fair_over(
    sharp_prices: dict[str, float],
    sharp_line_books: dict[str, dict[str, float]],
    target_line_books: dict[str, dict[str, float]],
    exclude: set[str],
) -> tuple[float, int] | None:
    """Fair chance of Over at the target line, and how many books' ladders it used."""
    level = _over_share(sharp_prices)
    if level is None:
        return None
    shifts = []
    for book, target in target_line_books.items():
        if book in exclude or book not in sharp_line_books:
            continue
        a, b = _over_share(sharp_line_books[book]), _over_share(target)
        if a is not None and b is not None:
            shifts.append(b - a)
    if len(shifts) < MIN_LADDER_BOOKS:
        return None
    p = level + sum(shifts) / len(shifts)
    if not MIN_PROB <= p <= MAX_PROB:
        return None
    return p, len(shifts)
