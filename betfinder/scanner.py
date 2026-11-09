"""Find +EV bets and arbitrage across bookmakers from The Odds API event data."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from .props import model_for
from .lines import LineModel, calibrate, crosses_key_number, max_shift, parse_bet
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
    "hardrockbet_fl",
    "hardrockbet_oh",
    # Exchanges and prediction markets (The Odds API region us_ex)
    "novig",
    "kalshi",
    "polymarket_us",
    "prophetx",
    "betopenly",
)


PREDICTION_MARKETS = ("kalshi", "polymarket_us", "novig", "prophetx", "betopenly")
OFFSHORE_BOOKS = (
    "bovada", "betonlineag", "mybookieag", "betus", "lowvig",
    "betanysports", "everygame", "gtbets", "betwhale",
)

# Named sets of books for --books, with the Odds API regions they need.
BOOK_PRESETS: dict[str, tuple[tuple[str, ...], str]] = {
    "texas": (PREDICTION_MARKETS + OFFSHORE_BOOKS, "us,us_ex,eu"),
    "prediction": (PREDICTION_MARKETS, "us_ex,eu"),
    "offshore": (OFFSHORE_BOOKS, "us,eu"),
    # The books this user actually has accounts with.
    "mine": (("novig", "kalshi", "polymarket_us", "prophetx", "bovada", "betus", "mybookieag"), "us,us_ex,eu"),
}

# Trading fee per contract as rate * P * (1 - P), where P is the contract price.
# Kalshi's published taker fee has this shape; check their fee schedule for the current rate.
FEE_RATES: dict[str, float] = {"kalshi": 0.07}


def net_odds(book: str, odds: float) -> float:
    """Decimal odds after the book's trading fee (unchanged for books without one)."""
    rate = FEE_RATES.get(book)
    if not rate:
        return odds
    price = 1.0 / odds
    return 1.0 / (price + rate * price * (1.0 - price))


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
    max_sharp_margin: float = 0.08
    max_consensus_margin: float = 0.12  # books with a bigger margin are left out of the consensus  # ignore sharp lines with a big margin (thin markets)
    bankroll: float = 1000.0
    kelly_multiplier: float = 0.25  # quarter Kelly
    max_stake_pct: float = 0.03  # never more than 3% of bankroll on one bet
    min_arb: float = 0.01  # smaller arbitrage margins are usually eaten by fees
    max_age_hours: float = 3.0  # ignore prices not updated for this long (likely stale)
    # Player props have no sharp line in the feed: the fair price is the average of the
    # *other* books on the same line, so the bar is higher.
    prop_min_books: int = 3
    prop_min_ev: float = 0.05


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


# Alternate lines are compared with the main lines (and each other) as one market.
_MARKET_ALIASES = {"alternate_spreads": "spreads", "alternate_totals": "totals"}


def base_market(market_key: str) -> str:
    return _MARKET_ALIASES.get(market_key, market_key)


_PERIOD_SUFFIXES = ("_h1", "_h2", "_q1", "_q2", "_q3", "_q4", "_p1", "_p2", "_p3")


def is_prop(market_key: str) -> bool:
    """Markets with no sharp line in the feed: player props and half/quarter/period lines."""
    return market_key.startswith("player_") or market_key.endswith(_PERIOD_SUFFIXES)


def _line_key(market_key: str, outcome: dict, home_team: str):
    """Identify which line an outcome belongs to, so only matching lines are compared.

    Player props and team totals carry the player or team in `description`, so the key
    includes it: two players with the same 24.5 line are different markets.
    """
    point = outcome.get("point")
    desc = outcome.get("description")
    if point is None or market_key == "h2h":
        return desc
    if market_key == "spreads":
        return point if outcome["name"] == home_team else -point
    if desc:
        return (desc, abs(point))
    return abs(point)  # totals and similar: Over/Under share a point


def _outcome_id(outcome: dict) -> str:
    name = outcome["name"]
    if outcome.get("description"):
        name = f"{outcome['description']} {name}"
    point = outcome.get("point")
    if point is not None:
        return f"{name} {point:g}" if outcome["name"] in ("Over", "Under") else f"{name} {point:+g}"
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
            mkey = base_market(market["key"])
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
    # Skip lines with an impossible margin (broken or placeholder prices).
    complete = [
        p for p in books.values()
        if len(p) == n_outcomes and -0.02 <= overround(list(p.values())) <= cfg.max_consensus_margin
    ]
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

        home = event.get("home_team", "")
        lines = _collect_lines(event, now, cfg.max_age_hours)
        sharp_lines = _sharp_lines(lines, cfg)
        sharp_props = _sharp_props(lines, cfg)
        for (market, line), books in lines.items():
            fair = _fair_probs(books, cfg)
            sharp_fair = bool(fair and fair[1] in cfg.sharp_books)
            prop = is_prop(market) and not sharp_fair
            model = None
            if market in ("spreads", "totals") and not sharp_fair:
                # No sharp price on this exact line: convert from a nearby sharp line.
                model = _nearest_model(event, market, line, sharp_lines)
            pmodel = pin_lines = None
            if prop and isinstance(line, tuple):
                found = sharp_props.get((market, line[0]))
                if found:
                    sharp_name, pin_lines = found
                    pmodel = model_for(event.get("sport_key", ""), market, pin_lines, line[1], sharp_name)
            if model or fair or prop:
                probs, source = fair if fair else ({}, "")
                for book_key in cfg.bet_books:
                    prices = books.get(book_key)
                    if not prices:
                        continue
                    if prop and pmodel is None and pin_lines is not None:
                        break  # Pinnacle prices this player too far from here to convert
                    if prop and pmodel is not None:
                        pass  # priced from Pinnacle's nearby line below
                    elif prop:
                        # Leave this book out of its own fair price.
                        others = {k: v for k, v in books.items() if k != book_key}
                        prop_cfg = replace(cfg, consensus_min_books=cfg.prop_min_books)
                        fair_ex = _fair_probs(others, prop_cfg) if others else None
                        if not fair_ex:
                            continue
                        probs, source = fair_ex[0], fair_ex[1].replace("consensus", "others")
                    for selection, odds in prices.items():
                        if not (cfg.min_odds <= odds <= cfg.max_odds):
                            continue
                        net = net_odds(book_key, odds)
                        if prop and pmodel is not None:
                            side, _, pt = selection.rpartition(" ")
                            side = side.rsplit(" ", 1)[-1]
                            try:
                                pt_val = float(pt)
                            except ValueError:
                                continue
                            if side not in ("Over", "Under"):
                                continue
                            p = 1.0 / pmodel.fair_odds(side, pt_val)
                            ev = pmodel.ev(side, pt_val, net)
                            source = pmodel.source
                        elif model:
                            bet = parse_bet(market, selection, home)
                            if bet is None:
                                continue
                            p = 1.0 / model.fair_odds(bet)
                            ev = model.ev(bet, net)
                            source = model.source
                        else:
                            p = probs.get(selection)
                            if p is None:
                                continue
                            ev = expected_value(p, net)
                        if ev < (cfg.prop_min_ev if prop else cfg.min_ev):
                            continue
                        stake_frac = min(
                            kelly_fraction(p, net) * cfg.kelly_multiplier, cfg.max_stake_pct
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


def _sharp_props(lines, cfg: ScanConfig) -> dict:
    """(market, player) -> (sharp book, {line: prices}) for player props the sharp book prices."""
    out: dict = {}
    for (market, line), books in lines.items():
        if not is_prop(market) or not isinstance(line, tuple):
            continue
        for sharp in cfg.sharp_books:
            prices = books.get(sharp)
            if prices and len(prices) == 2 and overround(list(prices.values())) <= cfg.max_sharp_margin:
                out.setdefault((market, line[0]), (sharp, {}))[1][line[1]] = prices
                break
    return out


def _sharp_lines(lines, cfg: ScanConfig) -> dict[tuple[str, float], tuple[str, dict[str, float]]]:
    """Two-way spread/total lines priced by a sharp book with a sensible margin."""
    out = {}
    for (market, line), books in lines.items():
        if market not in ("spreads", "totals") or line is None:
            continue
        for sharp in cfg.sharp_books:
            prices = books.get(sharp)
            if prices and len(prices) == 2 and overround(list(prices.values())) <= cfg.max_sharp_margin:
                out[(market, line)] = (sharp, prices)
                break
    return out


def _nearest_model(event: dict, market: str, line: float | None, sharp_lines) -> LineModel | None:
    sport_key = event.get("sport_key", "")
    shift = max_shift(sport_key, market)
    if shift is None or line is None:
        return None
    candidates = sorted(
        (abs(sl - line), sl, v) for (m, sl), v in sharp_lines.items()
        if m == market and sl != line and abs(sl - line) <= shift + 1e-9
        and not crosses_key_number(sport_key, market, sl, line)
    )
    if not candidates:
        return None
    _, sharp_line, (sharp, prices) = candidates[0]
    label = f"{sharp} {'total' if market == 'totals' else 'line'} {sharp_line:g}, converted"
    return calibrate(sport_key, market, event.get("home_team", ""), prices, label)


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
            odds = net_odds(book_key, odds)
            if sel not in best or odds > best[sel][0]:
                best[sel] = (odds, book_key)
    if len(best) != n_outcomes or n_outcomes < 2:
        return None
    sels = list(best)
    margin, stakes = arbitrage_stakes([best[s][0] for s in sels], cfg.bankroll * cfg.max_stake_pct * 3)
    if margin < cfg.min_arb or margin > cfg.max_ev:
        return None
    return margin, [(s, best[s][1], best[s][0], round(st, 2)) for s, st in zip(sels, stakes)]
