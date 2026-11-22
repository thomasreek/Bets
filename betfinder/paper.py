"""Paper trading: log every bet a scan flags, settle it and measure CLV, with no money at risk.

Kept completely apart from the real bet log (bet_log.csv / BETS.md). Each flagged bet is
recorded once, at the price the scan first saw, with a fixed to-win stake, so the log is a
fair test of the scanner no matter which bets were actually placed. Real bets that match a
paper bet are linked with a "taken" note but never counted here as real money.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clv import CLOSE_BEFORE_START, CONSENSUS_REGIONS, Target, _consensus_fair, _fair_close, parse
from .scanner import base_market, is_prop, risk_to_win
from .timefmt import fmt_time
from .tracker import REPO_ROOT, grade_h2h

LOG_FILE = REPO_ROOT / "paper_log.csv"
TABLE_FILE = REPO_ROOT / "PAPER.md"
PAPER_UNIT = 8.0  # every paper bet is sized to win this much


@dataclass
class PaperBet:
    id: int
    logged: str  # when the scan flagged it, ISO UTC
    sport_key: str
    event: str  # "Away @ Home"
    start: str
    market: str  # base market key, e.g. totals, spreads, player_points
    line: str  # scanner line key as JSON, used to find the same line at the close
    selection: str  # scanner outcome id, e.g. "Under 139.5", "P1 Over 24.5"
    book: str
    odds: float  # after fees
    fair_odds: float
    ev: float
    source: str  # where the fair price came from
    stake: float
    status: str = "open"  # open, won, lost, push, ungraded
    close_pin: float | None = None
    clv_pin: float | None = None
    close_cons: float | None = None
    clv_cons: float | None = None
    taken: str = ""  # real bet number, if the bet was actually placed

    @property
    def profit(self) -> float:
        if self.status == "won":
            return self.stake * (self.odds - 1)
        if self.status == "lost":
            return -self.stake
        return 0.0

    @property
    def kind(self) -> str:
        if self.source.startswith(("consensus", "others")):
            return "consensus"
        if "converted" in self.source or "ladder" in self.source:
            return "converted"
        return "exact"


_FLOATS = {"odds", "fair_odds", "ev", "stake", "close_pin", "clv_pin", "close_cons", "clv_cons"}


def load(path: Path = LOG_FILE) -> list[PaperBet]:
    if not path.exists():
        return []
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            kw = {}
            for fl in fields(PaperBet):
                v = row.get(fl.name, "")
                if fl.name == "id":
                    kw[fl.name] = int(v)
                elif fl.name in _FLOATS:
                    kw[fl.name] = float(v) if v not in ("", None) else None
                else:
                    kw[fl.name] = v or ""
            kw["status"] = kw["status"] or "open"
            out.append(PaperBet(**kw))
    return out


def save(bets: list[PaperBet], path: Path = LOG_FILE, table: Path | None = TABLE_FILE) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(PaperBet)])
        w.writeheader()
        for b in bets:
            row = asdict(b)
            for k in _FLOATS:
                if row[k] is None:
                    row[k] = ""
                else:
                    row[k] = round(row[k], 4)
            w.writerow(row)
    if table:
        table.write_text(render_markdown(bets))


def _side_key(event: str, market: str, selection: str, line) -> tuple:
    """One paper bet per side of a market per game: alternate lines of the same side count once."""
    base = base_market(market)
    if base == "totals":
        side = selection.split(" ")[0]
    elif base == "spreads":
        side = selection.rsplit(" ", 1)[0]
    elif isinstance(line, (list, tuple)):  # props: player + Over/Under (or Yes/No)
        parts = selection.rsplit(" ", 2)
        side = f"{line[0]} {parts[-2] if len(parts) >= 2 and parts[-2] in ('Over', 'Under') else parts[-1]}"
    else:
        side = selection
    return (event, base, side)


def record(value_bets, now: datetime | None = None, path: Path = LOG_FILE) -> int:
    """Add a scan's flagged bets to the paper log. Returns how many were new."""
    now = now or datetime.now(timezone.utc)
    bets = load(path)
    seen = {_side_key(b.event, b.market, b.selection, json.loads(b.line)) for b in bets}
    best: dict[tuple, object] = {}
    for vb in value_bets:
        if vb.suspicious:
            continue
        line = getattr(vb, "line", None)
        key = _side_key(vb.event, vb.market, vb.selection, line)
        if key in seen:
            continue
        if key not in best or vb.ev > best[key].ev:
            best[key] = vb
    next_id = max((b.id for b in bets), default=0) + 1
    for vb in sorted(best.values(), key=lambda v: (v.commence_time, -v.ev)):
        net = vb.net_odds or vb.odds
        bets.append(PaperBet(
            id=next_id, logged=now.strftime("%Y-%m-%dT%H:%M:%SZ"), sport_key=getattr(vb, "sport_key", ""),
            event=vb.event, start=vb.commence_time, market=base_market(vb.market),
            line=json.dumps(getattr(vb, "line", None)), selection=vb.selection, book=vb.book,
            odds=round(net, 4), fair_odds=round(vb.fair_odds, 4), ev=round(vb.ev, 4),
            source=vb.fair_source, stake=risk_to_win(net, PAPER_UNIT),
        ))
        next_id += 1
    if best:
        save(bets, path, table=TABLE_FILE if path == LOG_FILE else None)
    return len(best)


def settle(client, bets: list[PaperBet], now: datetime | None = None) -> int:
    """Grade finished spreads/totals/moneylines from final scores; mark old props ungraded."""
    now = now or datetime.now(timezone.utc)
    due = [b for b in bets if b.status == "open" and b.start
           and datetime.fromisoformat(b.start.replace("Z", "+00:00")) < now]
    n = 0
    for b in due:
        start = datetime.fromisoformat(b.start.replace("Z", "+00:00"))
        if b.market not in ("h2h", "spreads", "totals") and now - start > timedelta(hours=12):
            b.status = "ungraded"  # no player stats in the scores feed
    for sport in sorted({b.sport_key for b in due if b.market in ("h2h", "spreads", "totals")}):
        try:
            games = client.scores(sport, days_from=3)
        except Exception:
            continue
        for b in (x for x in due if x.sport_key == sport and x.market in ("h2h", "spreads", "totals")):
            away, _, home = b.event.partition(" @ ")
            for g in games:
                if g.get("completed") and g.get("home_team") == home and g.get("away_team") == away:
                    res = grade_h2h(b.selection, g.get("scores") or [])
                    if res == "push" and sport.startswith("soccer") and b.market == "h2h" and b.selection != "Draw":
                        res = "lost"
                    if res:
                        b.status = res
                        n += 1
                    break
    return n


def closing_values(client, bets: list[PaperBet], now: datetime | None = None, props: bool = False) -> int:
    """Pinnacle and non-Pinnacle consensus closing prices, one snapshot per game and market."""
    now = now or datetime.now(timezone.utc)
    todo = [b for b in bets if b.clv_pin is None and b.clv_cons is None and b.close_pin != -1
            and b.start and datetime.fromisoformat(b.start.replace("Z", "+00:00")) < now
            and (props or b.market in ("h2h", "spreads", "totals"))]
    events: dict[tuple, list] = {}
    snaps: dict[tuple, dict | None] = {}
    n = 0
    for b in todo:
        start = datetime.fromisoformat(b.start.replace("Z", "+00:00"))
        when = (start - CLOSE_BEFORE_START).strftime("%Y-%m-%dT%H:%M:%SZ")
        ek = (b.sport_key, when)
        if ek not in events:
            events[ek] = client.historical_events(b.sport_key, when)
        away, _, home = b.event.partition(" @ ")
        match = next((e for e in events[ek] if e.get("home_team") == home and e.get("away_team") == away), None)
        if match is None:
            b.close_pin = -1  # don't pay to retry
            continue
        sk = (match["id"], b.market, when)
        if sk not in snaps:
            snaps[sk] = client.historical_event_odds(b.sport_key, match["id"], b.market, when,
                                                     regions=CONSENSUS_REGIONS)
        snap = snaps[sk]
        line = json.loads(b.line)
        if isinstance(line, list):
            line = tuple(line)
        target = Target(b.market, b.selection, line, None)
        pin = _fair_close(snap, target) if snap else None
        cons = _consensus_fair(snap, target, b.sport_key) if snap else None
        b.close_pin = pin if pin else -1
        b.clv_pin = b.odds / pin - 1 if pin else None
        if cons:
            b.close_cons = cons[0]
            b.clv_cons = b.odds / cons[0] - 1
        n += 1
    return n


def link_taken(paper: list[PaperBet], real) -> int:
    """Mark paper bets that were actually placed, matching game, book and outcome."""
    n = 0
    for r in real:
        try:
            _, home = r.event.split(" @ ", 1)
        except ValueError:
            continue
        t = parse(r.selection, home)
        if t is None:
            continue
        for p in paper:
            if p.taken or p.event != r.event or p.book != r.book:
                continue
            if p.selection == t.selection or _side_key(p.event, p.market, p.selection, json.loads(p.line)) == \
                    _side_key(r.event, t.market, t.selection, t.line):
                p.taken = str(r.id)
                n += 1
                break
    return n


def _stats(bets: list[PaperBet]) -> dict:
    graded = [b for b in bets if b.status in ("won", "lost", "push")]
    wl = [b for b in graded if b.status != "push"]
    staked = sum(b.stake for b in graded)
    profit = sum(b.profit for b in graded)
    exp_wins = sum(1 / b.fair_odds for b in wl)
    var = sum((1 / b.fair_odds) * (1 - 1 / b.fair_odds) for b in wl)
    pin = [b.clv_pin for b in bets if b.clv_pin is not None]
    cons = [b.clv_cons for b in bets if b.clv_cons is not None]
    return {
        "n": len(bets), "graded": len(graded),
        "won": sum(b.status == "won" for b in graded), "lost": sum(b.status == "lost" for b in graded),
        "push": sum(b.status == "push" for b in graded),
        "profit": profit, "staked": staked, "roi": profit / staked if staked else 0.0,
        "exp_profit": sum(b.stake * b.ev for b in graded),
        "exp_wins": exp_wins, "z": (sum(b.status == "won" for b in wl) - exp_wins) / math.sqrt(var) if var else 0.0,
        "pin_n": len(pin), "pin_avg": sum(pin) / len(pin) if pin else 0.0, "pin_beat": sum(x > 0 for x in pin),
        "cons_n": len(cons), "cons_avg": sum(cons) / len(cons) if cons else 0.0,
        "cons_beat": sum(x > 0 for x in cons),
    }


def _row(name: str, s: dict) -> str:
    rec = f"{s['won']}-{s['lost']}-{s['push']}"
    pin = f"{s['pin_avg']:+.1%} ({s['pin_beat']}/{s['pin_n']})" if s["pin_n"] else ""
    cons = f"{s['cons_avg']:+.1%} ({s['cons_beat']}/{s['cons_n']})" if s["cons_n"] else ""
    exp = f"{s['exp_wins']:.1f}" if s["graded"] else ""
    return (f"| {name} | {s['n']} | {rec} | {exp} | ${s['profit']:+,.2f} | {s['roi']:+.1%} "
            f"| ${s['exp_profit']:+,.2f} | {pin} | {cons} |")


def summary_line(bets: list[PaperBet]) -> str:
    s = _stats(bets)
    return (f"Paper: {s['won']}-{s['lost']}-{s['push']}, ${s['profit']:+.2f} (ROI {s['roi']:+.1%}), "
            f"CLV vs Pinnacle {s['pin_avg']:+.1%} ({s['pin_beat']}/{s['pin_n']}), "
            f"vs other books {s['cons_avg']:+.1%} ({s['cons_beat']}/{s['cons_n']}), "
            f"{sum(b.status == 'open' for b in bets)} open")


def render_markdown(bets: list[PaperBet]) -> str:
    head = ("| Group | Bets | W-L-P | Expected wins | Profit | ROI | Expected profit "
            "| CLV vs Pinnacle (beat) | CLV vs other books (beat) |\n|---|---|---|---|---|---|---|---|---|")
    lines = [
        "# Paper trading log",
        "",
        f"Every bet a scan flags, sized to win ${PAPER_UNIT:g}, logged at the price the scan saw. "
        "No real money: this is kept apart from `BETS.md`. Spreads, totals and moneylines are "
        "graded from final scores; props get CLV only. \"Expected wins\" is the sum of the fair "
        "chances, so wins far below it mean the fair prices are too optimistic.",
        "",
        "## Summary",
        "",
        head,
        _row("**All**", _stats(bets)),
    ]
    groups = [
        ("Pinnacle exact line", lambda b: b.kind == "exact"),
        ("Converted / ladder", lambda b: b.kind == "converted"),
        ("Other-books average", lambda b: b.kind == "consensus"),
        ("Main lines", lambda b: b.market in ("h2h", "spreads", "totals")),
        ("Props", lambda b: is_prop(b.market)),
        ("Edge under 4%", lambda b: b.ev < 0.04),
        ("Edge 4-8%", lambda b: 0.04 <= b.ev < 0.08),
        ("Edge 8%+", lambda b: b.ev >= 0.08),
        ("Taken for real", lambda b: bool(b.taken)),
    ]
    for name, f in groups:
        sub = [b for b in bets if f(b)]
        if sub:
            lines.append(_row(name, _stats(sub)))
    for sport in sorted({b.sport_key for b in bets}):
        lines.append(_row(sport, _stats([b for b in bets if b.sport_key == sport])))
    for book in sorted({b.book for b in bets}):
        lines.append(_row(f"book: {book}", _stats([b for b in bets if b.book == book])))
    lines += [
        "",
        "## Bets",
        "",
        "| # | Game time (Houston) | Event | Bet | Book | Odds | Fair | Edge | Source | Result | CLV Pin | CLV others | Taken |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for b in sorted(bets, key=lambda b: b.id, reverse=True):
        lines.append(
            f"| {b.id} | {fmt_time(b.start)} | {b.event} | {b.selection} | {b.book} | {b.odds:.2f} "
            f"| {b.fair_odds:.2f} | {b.ev:+.1%} | {b.kind} | {b.status} "
            f"| {'' if b.clv_pin is None else f'{b.clv_pin:+.1%}'} "
            f"| {'' if b.clv_cons is None else f'{b.clv_cons:+.1%}'} | {('#' + b.taken) if b.taken else ''} |"
        )
    return "\n".join(lines) + "\n"
