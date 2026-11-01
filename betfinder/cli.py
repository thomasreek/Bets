"""Command-line entry point: python -m betfinder ..."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import os

from .bettingdata_api import BettingDataClient
from .odds_api import OddsApiClient, OddsApiError
from .odds_math import decimal_to_american
from .scanner import DEFAULT_US_BOOKS, ScanConfig, scan_events

DEFAULT_SPORTS = "americanfootball_nfl,basketball_nba,icehockey_nhl,soccer_epl,tennis"


def _make_client(provider: str):
    if provider == "auto":
        provider = "bettingdata" if os.environ.get("BETTINGDATA_API_KEY") else "oddsapi"
    return (BettingDataClient() if provider == "bettingdata" else OddsApiClient()), provider
DEMO_FILE = Path(__file__).parent / "sample_odds.json"


def _resolve_sports(client, spec: str) -> list[str]:
    """Turn 'all', group names like 'tennis', league names like 'eredivisie', or sport keys into keys."""
    active = [s for s in client.sports() if s.get("active") and not s.get("has_outrights")]
    wanted = [w.strip().lower() for w in spec.split(",") if w.strip()]
    if "all" in wanted:
        return [s["key"] for s in active]
    keys: list[str] = []
    for w in wanted:
        group = [s["key"] for s in active if s.get("group", "").lower() == w]
        if group:
            keys.extend(group)
        elif any(s["key"] == w for s in active):
            keys.append(w)
        elif any(s.get("title", "").lower() == w or s["key"].endswith("_" + w) for s in active):
            keys.extend(s["key"] for s in active
                        if s.get("title", "").lower() == w or s["key"].endswith("_" + w))
        else:
            print(f"  (skipping '{w}': not in season, unknown, or not included in your API plan)",
                  file=sys.stderr)
    return list(dict.fromkeys(keys))


def _fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%a %d %b %H:%M")
    except ValueError:
        return iso


def _print_value_bets(bets, limit: int) -> None:
    if not bets:
        print("\nNo +EV bets found at the current thresholds.")
        return
    print(f"\n=== +EV BETS ({len(bets)} found, showing top {min(limit, len(bets))}) ===")
    header = f"{'EV':>6}  {'Odds':>11}  {'Fair':>6}  {'Stake':>8}  {'Book':<14} {'Market':<8} Selection / Event"
    print(header)
    print("-" * len(header))
    for b in bets[:limit]:
        flag = "  ⚠ CHECK: EV too high, likely stale or mismatched" if b.suspicious else ""
        odds = f"{b.odds:.2f} ({decimal_to_american(b.odds):+d})"
        print(
            f"{b.ev:>6.1%}  {odds:>11}  {b.fair_odds:>6.2f}  ${b.stake:>7.2f}  {b.book:<14} {b.market:<8} "
            f"{b.selection}{flag}"
        )
        print(f"{'':>46}{b.event} | {b.sport} | {_fmt_time(b.commence_time)} | fair from {b.fair_source}")


def _print_arbs(arbs) -> None:
    if not arbs:
        return
    print(f"\n=== ARBITRAGE ({len(arbs)} found) ===")
    for a in arbs:
        print(f"{a.margin:.2%} guaranteed | {a.event} | {a.sport} | {a.market} | {_fmt_time(a.commence_time)}")
        for sel, book, odds, stake in a.legs:
            print(f"    ${stake:>7.2f} on {sel:<30} @ {odds:.2f} ({book})")


def _write_csv(path: str, bets) -> None:
    with open(path, "w", newline="") as f:
        if not bets:
            return
        writer = csv.DictWriter(f, fieldnames=list(asdict(bets[0])))
        writer.writeheader()
        for b in bets:
            writer.writerow(asdict(b))
    print(f"\nSaved {len(bets)} bets to {path}")


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = ScanConfig(
        min_ev=args.min_ev,
        bankroll=args.bankroll,
        kelly_multiplier=args.kelly,
        devig=args.devig,
        max_odds=args.max_odds,
        bet_books=tuple(b.strip() for b in args.books.split(",")) if args.books else DEFAULT_US_BOOKS,
        max_age_hours=args.max_age,
    )

    if args.demo:
        events = json.loads(DEMO_FILE.read_text())
        print(f"DEMO MODE: using bundled sample odds from {DEMO_FILE.name} (not real games).")
    else:
        try:
            client, provider = _make_client(args.provider)
            sports = _resolve_sports(client, args.sports)
            n_markets = len(args.markets.split(","))
            n_regions = len(args.regions.split(","))
            if provider == "bettingdata":
                cost = len(sports)
                print(f"[Betting Data API] Scanning {len(sports)} sports = {cost} requests")
            else:
                cost = len(sports) * n_markets * n_regions
                print(f"[The Odds API] Scanning {len(sports)} sports x {n_markets} markets x {n_regions} "
                      f"regions = ~{cost} API credits")
            if cost > args.max_cost:
                print(f"That is above --max-cost {args.max_cost}. Narrow --sports/--markets or raise "
                      f"--max-cost.", file=sys.stderr)
                return 2
            events = []
            for sport in sports:
                try:
                    events.extend(client.odds(sport, regions=args.regions, markets=args.markets))
                except OddsApiError as e:
                    print(f"  {sport}: {e}", file=sys.stderr)
            if provider == "bettingdata":
                client.account()
            print(f"Fetched {len(events)} events. API requests remaining: {client.requests_remaining}")
            if args.save_raw:
                Path(args.save_raw).write_text(json.dumps(events, indent=1))
        except OddsApiError as e:
            print(e, file=sys.stderr)
            return 1

    bets, arbs = scan_events(events, cfg)
    _print_value_bets(bets, args.limit)
    _print_arbs(arbs)
    if args.csv:
        _write_csv(args.csv, bets)
    print(
        "\nStakes are fractional Kelly on a ${:,.0f} bankroll, capped at {:.0%} per bet. Re-check each price "
        "at the book before betting: lines move fast.".format(cfg.bankroll, cfg.max_stake_pct)
    )
    return 0


def cmd_sports(args: argparse.Namespace) -> int:
    try:
        client, _ = _make_client(args.provider)
        for s in client.sports():
            if s.get("active"):
                outright = " (outrights)" if s.get("has_outrights") else ""
                print(f"{s['key']:<45} {s.get('group', ''):<20} {s.get('title', '')}{outright}")
    except OddsApiError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="betfinder", description="Find +EV bets at US sportsbooks.")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan odds for +EV bets and arbitrage")
    scan.add_argument("--provider", choices=["auto", "bettingdata", "oddsapi"], default="auto",
                      help="odds source (auto: bettingdata if BETTINGDATA_API_KEY is set)")
    scan.add_argument("--demo", action="store_true", help="use bundled sample data, no API key needed")
    scan.add_argument("--sports", default=DEFAULT_SPORTS,
                      help="comma list of sport keys, groups (soccer, tennis, ...) or 'all'")
    scan.add_argument("--markets", default="h2h", help="h2h,spreads,totals (each costs credits)")
    scan.add_argument("--regions", default="us,us2,us_ex,eu",
                      help="eu = Pinnacle sharp line, us2 = ESPN BET/Hard Rock/Fliff, us_ex = Kalshi/Novig/Polymarket/ProphetX")
    scan.add_argument("--books", help="comma list of bookmaker keys you can bet with (default: all US books)")
    scan.add_argument("--min-ev", type=float, default=0.02, help="minimum expected value (0.02 = 2%%)")
    scan.add_argument("--max-odds", type=float, default=10.0, help="ignore longer odds than this (decimal)")
    scan.add_argument("--bankroll", type=float, default=1000.0)
    scan.add_argument("--kelly", type=float, default=0.25, help="Kelly multiplier (0.25 = quarter Kelly)")
    scan.add_argument("--devig", choices=["power", "multiplicative"], default="power")
    scan.add_argument("--max-cost", type=int, default=60, help="refuse scans that cost more API credits")
    scan.add_argument("--max-age", type=float, default=3.0,
                      help="ignore prices not updated in this many hours")
    scan.add_argument("--limit", type=int, default=25)
    scan.add_argument("--csv", help="also write the bets to this CSV file")
    scan.add_argument("--save-raw", help="save the raw API response to this JSON file")
    scan.set_defaults(func=cmd_scan)

    sports = sub.add_parser("sports", help="list sports your key can access")
    sports.add_argument("--provider", choices=["auto", "bettingdata", "oddsapi"], default="auto")
    sports.set_defaults(func=cmd_sports)

    args = parser.parse_args(argv)
    return args.func(args)
