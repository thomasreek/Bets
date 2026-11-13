"""Closing line value: compare each bet's price with Pinnacle's fair price at game time.

For every bet whose game has started, the Pinnacle odds from a few minutes before the start
are fetched from The Odds API's historical endpoints. The margin is removed, and CLV is the
bet's (after-fee) odds divided by that fair closing price, minus 1. Beating the close
consistently is the quickest reliable sign that the scanner's edges are real.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .odds_math import devig_power
from .scanner import ScanConfig, _collect_lines, _nearest_model, _sharp_lines
from .lines import parse_bet

CLOSE_BEFORE_START = timedelta(minutes=5)

# Words in a logged selection -> Odds API player market.
_PROP_WORDS = [
    ("anytime td", "player_anytime_td"),
    ("shots", "player_shots_on_goal"),
    ("pra", "player_points_rebounds_assists"),
    ("rebounds", "player_rebounds"),
    ("assists", "player_assists"),
    ("threes", "player_threes"),
    ("rushing yards", "player_rush_yds"),
    ("receiving yards", "player_reception_yds"),
    ("passing yards", "player_pass_yds"),
    ("receptions", "player_receptions"),
    ("points", "player_points"),
]


@dataclass
class Target:
    market: str  # Odds API market key
    selection: str  # outcome id as the scanner builds it, e.g. "Over 151.5", "Ann Lee Over 1.5"
    line: object  # scanner line key
    point: float | None = None


def parse(selection: str, home: str) -> Target | None:
    """Turn a logged selection into the market and outcome to look up at the close."""
    text = selection.strip()
    low = text.lower()
    m = re.fullmatch(r"(Over|Under) ([\d.]+)", text)
    if m:
        pt = float(m.group(2))
        return Target("totals", f"{m.group(1)} {pt:g}", abs(pt), pt)
    for word, market in _PROP_WORDS:
        if word not in low:
            continue
        if market == "player_anytime_td":
            name = re.sub(r"\s*anytime td.*$", "", text, flags=re.I)
            return Target(market, f"{name} Yes", name)
        for pattern, side, shift in ((r"^(.*?) (Over|Under) ([\d.]+)", None, 0.0),
                                     (r"^(.*?) (\d+)\+", "Over", -0.5),
                                     (r"^(.*?) under (\d+)\b", "Under", -0.5)):
            # Capitalised "Over/Under 10.5" is an exact line; lowercase "under 7" is the
            # "7+ No" style, i.e. Under 6.5.
            m = re.match(pattern, text)
            if m:
                name = m.group(1).strip()
                if side is None:
                    side, pt = m.group(2).capitalize(), float(m.group(3))
                else:
                    pt = float(m.group(2)) + shift
                return Target(market, f"{name} {side} {pt:g}", (name, pt), pt)
        return None
    m = re.fullmatch(r"(.+) ([+-][\d.]+)", text)
    if m:
        team, pt = m.group(1), float(m.group(2))
        line = pt if team == home else -pt
        return Target("spreads", f"{team} {pt:+g}", line, pt)
    return Target("h2h", text, None)


def _fair_close(event: dict, target: Target) -> float | None:
    """Pinnacle's fair decimal odds for the target outcome in a historical event snapshot."""
    lines = _collect_lines(event)
    books = lines.get((target.market, target.line), {})
    prices = books.get("pinnacle")
    if prices and target.selection in prices and len(prices) >= 2:
        names = list(prices)
        probs = devig_power([prices[n] for n in names])
        return 1.0 / probs[names.index(target.selection)]
    if target.market in ("spreads", "totals") and target.line is not None:
        cfg = ScanConfig(sharp_books=("pinnacle",))
        model = _nearest_model(event, target.market, target.line, _sharp_lines(lines, cfg))
        bet = parse_bet(target.market, target.selection, event.get("home_team", ""))
        if model and bet:
            return model.fair_odds(bet)
    return None


def closing_odds(client, bet) -> tuple[float | None, str]:
    """(fair closing odds, note) for one logged bet, using historical Pinnacle odds."""
    try:
        away, home = bet.event.split(" @ ", 1)
    except ValueError:
        return None, "event name not in 'Away @ Home' form"
    target = parse(bet.selection, home)
    if target is None:
        return None, "could not read the selection"
    start = datetime.fromisoformat(bet.start.replace("Z", "+00:00"))
    when = (start - CLOSE_BEFORE_START).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = client.historical_events(bet.sport, when)
    match = next((e for e in events if e.get("home_team") == home and e.get("away_team") == away), None)
    if match is None:
        return None, "game not found in the historical feed"
    markets = target.market
    if target.market in ("spreads", "totals"):
        markets += f",alternate_{target.market}"
    snap = client.historical_event_odds(bet.sport, match["id"], markets, when)
    fair = _fair_close(snap, target) if snap else None
    if fair is None:
        return None, "Pinnacle had no closing price for this line"
    return fair, "ok"
