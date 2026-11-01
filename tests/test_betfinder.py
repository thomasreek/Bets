import json
import unittest
from pathlib import Path

from betfinder.odds_math import (
    american_to_decimal, arbitrage_stakes, decimal_to_american, devig_multiplicative,
    devig_power, expected_value, kelly_fraction,
)
from betfinder.scanner import ScanConfig, scan_events

SAMPLE = json.loads((Path(__file__).parent.parent / "betfinder" / "sample_odds.json").read_text())


class OddsMathTest(unittest.TestCase):
    def test_conversions(self):
        self.assertAlmostEqual(american_to_decimal(150), 2.5)
        self.assertAlmostEqual(american_to_decimal(-200), 1.5)
        self.assertEqual(decimal_to_american(2.5), 150)
        self.assertEqual(decimal_to_american(1.5), -200)

    def test_devig_sums_to_one(self):
        odds = [1.45, 2.88]
        for fn in (devig_multiplicative, devig_power):
            self.assertAlmostEqual(sum(fn(odds)), 1.0, places=9)

    def test_power_devig_shades_longshot_more(self):
        odds = [1.30, 4.00]
        self.assertLess(devig_power(odds)[1], devig_multiplicative(odds)[1])

    def test_ev_and_kelly(self):
        self.assertAlmostEqual(expected_value(0.5, 2.1), 0.05)
        self.assertAlmostEqual(kelly_fraction(0.5, 2.1), 0.05 / 1.1)
        self.assertEqual(kelly_fraction(0.4, 2.0), 0.0)

    def test_arbitrage(self):
        margin, stakes = arbitrage_stakes([2.10, 2.02], 100)
        self.assertGreater(margin, 0)
        self.assertAlmostEqual(stakes[0] * 2.10, stakes[1] * 2.02)


class ScannerTest(unittest.TestCase):
    def test_finds_expected_bets(self):
        bets, arbs = scan_events(SAMPLE, ScanConfig())
        found = {(b.book, b.selection) for b in bets}
        self.assertIn(("fanduel", "Boston Celtics"), found)
        self.assertIn(("betmgm", "Draw"), found)
        self.assertIn(("fanduel", "Boston Celtics -6.5"), found)
        self.assertNotIn(("draftkings", "Boston Celtics"), found)
        self.assertEqual(len(arbs), 1)
        self.assertTrue(all(b.ev >= 0.02 for b in bets))

    def test_sharp_book_is_never_a_bet(self):
        bets, _ = scan_events(SAMPLE, ScanConfig())
        self.assertFalse(any(b.book == "pinnacle" for b in bets))

    def test_book_filter(self):
        bets, _ = scan_events(SAMPLE, ScanConfig(bet_books=("draftkings",)))
        self.assertTrue(bets)
        self.assertTrue(all(b.book == "draftkings" for b in bets))

    def test_started_games_skipped(self):
        past = json.loads(json.dumps(SAMPLE))
        for e in past:
            e["commence_time"] = "2000-01-01T00:00:00Z"
        self.assertEqual(scan_events(past, ScanConfig()), ([], []))

    def test_stake_cap(self):
        bets, _ = scan_events(SAMPLE, ScanConfig(kelly_multiplier=1.0, max_stake_pct=0.01, bankroll=1000))
        self.assertTrue(all(b.stake <= 10.0 for b in bets))


if __name__ == "__main__":
    unittest.main()


class BettingDataAdapterTest(unittest.TestCase):
    def test_conversion_and_staleness(self):
        from datetime import datetime, timedelta, timezone

        from betfinder.bettingdata_api import to_odds_api_event
        from betfinder.scanner import _collect_lines

        now = datetime.now(timezone.utc)
        fresh = now.isoformat()
        old = (now - timedelta(hours=10)).isoformat()
        raw = {"event_id": "x", "league": "NFL", "home_team": "H", "away_team": "A",
               "start_time": (now + timedelta(hours=5)).isoformat(),
               "books": [
                   {"book": "draftkings", "market": "h2h", "updated_at": fresh,
                    "outcomes": [{"name": "H", "price": 1.9}, {"name": "A", "price": 1.95}]},
                   {"book": "fanduel", "market": "h2h", "updated_at": old,
                    "outcomes": [{"name": "H", "price": 1.8}, {"name": "A", "price": 2.1}]},
               ]}
        ev = to_odds_api_event(raw, "americanfootball_nfl")
        self.assertEqual(ev["sport_title"], "NFL")
        self.assertEqual({b["key"] for b in ev["bookmakers"]}, {"draftkings", "fanduel"})
        lines = _collect_lines(ev, now, max_age_hours=3)
        self.assertEqual(set(lines[("h2h", None)]), {"draftkings"})
