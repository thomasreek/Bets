"""Find +EV bets and arbitrage across bookmakers from The Odds API event data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .odds_math import (
    DEVIG_METHODS,
    arbitrage_stakes,
    expected_value,
    kelly_fraction,
    overround,
)

# Bookmakers whose lines are a good estimate of the true probability.
DEFAULT_SHARP_BOOKS = ("pinnacle",)

# US sportsbooks you can place bets with (The Odds API keys, regions us/us2).
# Check which ones are legal in your state and drop the rest with --books.
DEFAULT_US_BOOKS = (
    "draftkings",
    "fanduel",
    "betmgm",
    "williamhill_us",  # Caesars
    "espnbet",
    "betrivers",
    "fanatics",
    "hardrockbet",
    "ballybet",
    "betparx",
    "circa",
    "fliff",
    "bovada",
    "betonlineag",
    "lowvig",
    "mybookieag",
    "betus",
)


@dataclass
class ScanConfig:
    sharp_books: tuple[str, ...] = DEFAULT_SHARP_BOOKS
    bet_books: tuple[str, ...] = DEFAULT_US_BOOKS
    min_ev: float = 0.02  # flag bets with at least 2% expected return
    max_ev: float = 0.25  # above this, assume stale or mismatched data
    min_odds: float = 1.20
    max_odds: float = 10.0  # longshot fair probabilities are the least reliable
    devig: str = "power"
    consensus_min_books: int = 4  # when no sharp book has the line
    max_sharp_margin: float = 0.08  # ignore sharp lines with a big margin (thin markets)
    bankroll: float = 1000.0
    kelly_multiplier: float = 0.25  # quarter Kelly
    max_stake_pct: float = 0.03  # never more than 3% of bankroll on one bet
    max_age_hours: float = 3.0  # ignore prices not updated for this long (likely stale)


@dataclass
class ValueBet:
    sport: str
    event: str
    commence_time: str
    market: str
    selection: str
    book: str
    odds: float
    fair_prob: float
    fair_odds: float
    ev: float
    stake: float
    fair_source: str
    suspicious: bool = False


@dataclass
class Arbitrage:
    sport: str
    event: str
    commence_time: str
    market: str
    legs: list[tuple[str, str, float, float]] = field(default_factory=list)  # selection, book, odds, stake
    margin: float = 0.0


def _line_key(market_key: str, outcome: dict, home_team: str) -> float | None:
    """Identify which line an outcome belongs to, so only matching lines are compared."""
    point = outcome.get("point")
    if point is None or market_key == "h2h":
        return None
    if market_key == "spreads":
        return point if outcome["name"] == home_team else -point
    return abs(point)  # totals and similar: Over/Under share a point


def _outcome_id(outcome: dict) -> str:
    name = outcome["name"]
    if outcome.get("description"):
        name = f"{outcome['description']} {name}"
    point = outcome.get("point")
    if point is not None:
        return f"{name} {point:+g}" if name not in ("Over", "Under") else f"{name} {point:g}"
    return name


def _is_stale(updated: str | None, now: datetime, max_age_hours: float) -> bool:
    if not updated:
        return False
    try:
        ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now - ts).total_seconds() > max_age_hours * 3600


def _collect_lines(
    event: dict, now: datetime | None = None, max_age_hours: float = float("inf")
) -> dict[tuple[str, float | None], dict[str, dict[str, float]]]:
    """Map (market, line) -> book -> outcome_id -> decimal price, skipping stale prices."""
    lines: dict[tuple[str, float | None], dict[str, dict[str, float]]] = {}
    home = event.get("home_team", "")
    now = now or datetime.now(timezone.utc)
    for book in event.get("bookmakers", []):
        for market in book.get("markets", []):
            if _is_stale(market.get("last_update") or book.get("last_update"), now, max_age_hours):
                continue
            mkey = market["key"]
            for outcome in market.get("outcomes", []):
                price = outcome.get("price")
                if not price or price <= 1.0:
                    continue
                key = (mkey, _line_key(mkey, outcome, home))
                lines.setdefault(key, {}).setdefault(book["key"], {})[_outcome_id(outcome)] = float(price)
    return lines


def _fair_probs(
    books: dict[str, dict[str, float]], cfg: ScanConfig
) -> tuple[dict[str, float], str] | None:
    """Estimate true probabilities for one line. Prefers sharp books, falls back to consensus."""
    devig = DEVIG_METHODS[cfg.devig]
    n_outcomes = max(len(p) for p in books.values())
    if n_outcomes < 2:
        return None

    for sharp in cfg.sharp_books:
        prices = books.get(sharp)
        if prices and len(prices) == n_outcomes:
            names = list(prices)
            odds = [prices[n] for n in names]
            if overround(odds) > cfg.max_sharp_margin:
                continue
            return dict(zip(names, devig(odds))), sharp

    # Consensus: average the de-vigged probabilities of every book with the full line.
    complete = [p for p in books.values() if len(p) == n_outcomes]
    if len(complete) < cfg.consensus_min_books:
        return None
    names = list(complete[0])
    if any(set(p) != set(names) for p in complete):
        return None
    sums = dict.fromkeys(names, 0.0)
    for prices in complete:
        for name, prob in zip(names, devig([prices[n] for n in names])):
            sums[name] += prob
    return {n: s / len(complete) for n, s in sums.items()}, f"consensus({len(complete)})"


def scan_events(events: list[dict], cfg: ScanConfig) -> tuple[list[ValueBet], list[Arbitrage]]:
    value_bets: list[ValueBet] = []
    arbs: list[Arbitrage] = []
    now = datetime.now(timezone.utc)

    for event in events:
        start = event.get("commence_time", "")
        try:
            if datetime.fromisoformat(start.replace("Z", "+00:00")) <= now:
                continue  # skip live games: in-play odds move too fast for this
        except ValueError:
            pass
        name = f"{event.get('away_team', '?')} @ {event.get('home_team', '?')}"
        sport = event.get("sport_title") or event.get("sport_key", "")

        for (market, _line), books in _collect_lines(event, now, cfg.max_age_hours).items():
            fair = _fair_probs(books, cfg)
            if fair:
                probs, source = fair
                for book_key in cfg.bet_books:
                    prices = books.get(book_key)
                    if not prices:
                        continue
                    for selection, odds in prices.items():
                        p = probs.get(selection)
                        if p is None or not (cfg.min_odds <= odds <= cfg.max_odds):
                            continue
                        ev = expected_value(p, odds)
                        if ev < cfg.min_ev:
                            continue
                        stake_frac = min(
                            kelly_fraction(p, odds) * cfg.kelly_multiplier, cfg.max_stake_pct
                        )
                        value_bets.append(
                            ValueBet(
                                sport=sport,
                                event=name,
                                commence_time=start,
                                market=market,
                                selection=selection,
                                book=book_key,
                                odds=odds,
                                fair_prob=p,
                                fair_odds=1.0 / p,
                                ev=ev,
                                stake=round(stake_frac * cfg.bankroll, 2),
                                fair_source=source,
                                suspicious=ev > cfg.max_ev,
                            )
                        )

            arb = _find_arb(books, cfg)
            if arb:
                margin, legs = arb
                arbs.append(Arbitrage(sport, name, start, market, legs, margin))

    value_bets.sort(key=lambda b: (b.suspicious, -b.ev))
    arbs.sort(key=lambda a: -a.margin)
    return value_bets, arbs


def _find_arb(
    books: dict[str, dict[str, float]], cfg: ScanConfig
) -> tuple[float, list[tuple[str, str, float, float]]] | None:
    usable = {k: v for k, v in books.items() if k in cfg.bet_books}
    if not usable:
        return None
    n_outcomes = max(len(p) for p in books.values())
    best: dict[str, tuple[float, str]] = {}
    for book_key, prices in usable.items():
        for sel, odds in prices.items():
            if sel not in best or odds > best[sel][0]:
                best[sel] = (odds, book_key)
    if len(best) != n_outcomes or n_outcomes < 2:
        return None
    sels = list(best)
    margin, stakes = arbitrage_stakes([best[s][0] for s in sels], cfg.bankroll * cfg.max_stake_pct * 3)
    if margin <= 0 or margin > cfg.max_ev:
        return None
    return margin, [(s, best[s][1], best[s][0], round(st, 2)) for s, st in zip(sels, stakes)]
