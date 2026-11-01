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
from .scanner import BOOK_PRESETS, DEFAULT_US_BOOKS, ScanConfig, scan_events

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


def _bet_books(spec: str | None) -> tuple[str, ...]:
    if not spec:
        return DEFAULT_US_BOOKS
    if spec.lower() in BOOK_PRESETS:
        return BOOK_PRESETS[spec.lower()][0]
    return tuple(b.strip() for b in spec.split(",") if b.strip())


def _fmt_time(iso: str) -> str:
    try:
        from .timefmt import fmt_time
        return fmt_time(iso)
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
    if args.regions is None:
        preset = BOOK_PRESETS.get((args.books or "").lower())
        args.regions = preset[1] if preset else "us,us2,us_ex,eu"
    cfg = ScanConfig(
        min_ev=args.min_ev,
        bankroll=args.bankroll,
        kelly_multiplier=args.kelly,
        devig=args.devig,
        max_odds=args.max_odds,
        bet_books=_bet_books(args.books),
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


def _print_log(bets) -> None:
    from . import tracker
    s = tracker.summary(bets)
    for b in sorted(bets, key=lambda b: b.id):
        edge = f"{b.odds / b.fair_odds - 1:+.1%}" if b.fair_odds else "   -"
        print(f"#{b.id:<3} {b.placed}  {b.status:<5} ${b.stake:>7.2f} @ {b.odds:.3f} "
              f"({decimal_to_american(b.odds):+d})  edge {edge:>6}  {b.selection}  [{b.event}, {b.book}, {_fmt_time(b.start)}]"
              + (f"  profit {b.profit:+.2f}" if b.status in ("won", "lost", "push") else ""))
    print(f"\nRecord {s['won']}-{s['lost']}-{s['push']}, {s['open']} open (${s['open_stake']:.2f} at risk). "
          f"Profit ${s['profit']:+.2f} on ${s['staked']:.2f} settled (ROI {s['roi']:.1%}). "
          f"Expected profit ${s['expected_profit']:+.2f}.")


def cmd_bet(args: argparse.Namespace) -> int:
    from . import tracker
    from .odds_math import american_to_decimal

    bets = tracker.load()
    if args.action == "add":
        if args.odds:
            odds = args.odds
        elif args.american:
            odds = american_to_decimal(args.american)
        elif args.to_win:
            odds = 1 + args.to_win / args.stake
        else:
            print("Give the price with --odds, --american or --to-win.", file=sys.stderr)
            return 2
        fair = args.fair or (american_to_decimal(args.fair_american) if args.fair_american else None)
        bet = tracker.add(
            bets, placed=args.placed or datetime.now().strftime("%Y-%m-%d"), sport=args.sport or "",
            event=args.event, selection=args.selection, book=args.book, stake=args.stake,
            odds=round(odds, 4), fair_odds=fair, start=args.start or "", notes=args.notes or "",
        )
        tracker.save(bets)
        print(f"Added bet #{bet.id}: ${bet.stake:.2f} on {bet.selection} @ {bet.odds:.3f} to win ${bet.to_win:.2f}")
    elif args.action == "settle":
        if args.auto:
            try:
                done = tracker.auto_settle(bets, OddsApiClient())
            except OddsApiError as e:
                print(e, file=sys.stderr)
                return 1
            for b, result in done:
                print(f"#{b.id} {b.selection}: {result} ({b.profit:+.2f})")
            if not done:
                print("No open bets have final scores yet.")
        else:
            if not args.id or not args.result:
                print("Usage: bet settle ID won|lost|push|void   (or: bet settle --auto)", file=sys.stderr)
                return 2
            match = [b for b in bets if b.id == args.id]
            if not match:
                print(f"No bet #{args.id}", file=sys.stderr)
                return 1
            match[0].status = args.result
            print(f"#{args.id} marked {args.result} ({match[0].profit:+.2f})")
        tracker.save(bets)
    elif args.action == "delete":
        bets = [b for b in bets if b.id != args.id]
        tracker.save(bets)
        print(f"Deleted bet #{args.id}")
    _print_log(bets)
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
    scan.add_argument("--regions", default=None,
                      help="eu = Pinnacle sharp line, us2 = ESPN BET/Hard Rock/Fliff, us_ex = Kalshi/Novig/Polymarket/ProphetX")
    scan.add_argument("--books", default=os.environ.get("BETFINDER_BOOKS"),
                      help="bookmaker keys you can bet with, or a preset: texas, prediction, offshore "
                           "(default: $BETFINDER_BOOKS, else all US books)")
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

    bet = sub.add_parser("bet", help="track the bets you place (log: bet_log.csv, table: BETS.md)")
    bsub = bet.add_subparsers(dest="action")
    bet.set_defaults(func=cmd_bet, action="list")
    bsub.add_parser("list", help="show all bets and profit")
    add = bsub.add_parser("add", help="record a bet you placed")
    add.add_argument("--event", required=True, help='"Away Team @ Home Team" (as the scanner prints it)')
    add.add_argument("--selection", required=True, help="team or outcome you backed, e.g. 'Detroit Lions' or 'Draw'")
    add.add_argument("--stake", type=float, required=True)
    add.add_argument("--odds", type=float, help="decimal odds")
    add.add_argument("--american", type=float, help="American odds, e.g. -167")
    add.add_argument("--to-win", type=float, help="profit if it wins (odds are worked out from this)")
    add.add_argument("--book", default="unknown")
    add.add_argument("--sport", help="sport key, e.g. americanfootball_nfl (needed for --auto settling)")
    add.add_argument("--start", help="game start time, ISO 8601 UTC")
    add.add_argument("--fair", type=float, help="fair decimal odds from the scan, to track expected profit")
    add.add_argument("--fair-american", type=float)
    add.add_argument("--placed", help="date placed, YYYY-MM-DD (default today)")
    add.add_argument("--notes")
    settle = bsub.add_parser("settle", help="record a result")
    settle.add_argument("id", type=int, nargs="?")
    settle.add_argument("result", nargs="?", choices=["won", "lost", "push", "void"])
    settle.add_argument("--auto", action="store_true", help="grade open moneyline bets from final scores")
    delete = bsub.add_parser("delete", help="remove a bet entered by mistake")
    delete.add_argument("id", type=int)

    args = parser.parse_args(argv)
    return args.func(args)
