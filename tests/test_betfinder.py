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


class PresetTest(unittest.TestCase):
    def test_texas_preset(self):
        from betfinder.cli import _bet_books
        books = _bet_books("texas")
        self.assertIn("kalshi", books)
        self.assertIn("bovada", books)
        self.assertNotIn("draftkings", books)
        self.assertEqual(_bet_books("fanduel, kalshi"), ("fanduel", "kalshi"))


class TrackerTest(unittest.TestCase):
    def test_log_roundtrip_and_profit(self):
        import tempfile

        from betfinder import tracker
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "log.csv"
            bets = []
            tracker.add(bets, placed="2026-11-01", sport="americanfootball_nfl", event="A @ H",
                        selection="H", book="x", stake=16.68, odds=1 + 10 / 16.68, fair_odds=1.58,
                        start="2026-11-01T18:00:00Z")
            tracker.add(bets, placed="2026-11-01", sport="", event="B @ C", selection="B", book="y",
                        stake=10, odds=3.0, fair_odds=None, start="")
            bets[0].status, bets[1].status = "won", "lost"
            tracker.save(bets, path, table=None)
            loaded = tracker.load(path)
            self.assertEqual(loaded[0].to_win, 10.0)
            s = tracker.summary(loaded)
            self.assertAlmostEqual(s["profit"], 0.0)
            self.assertEqual((s["won"], s["lost"]), (1, 1))
            self.assertIn("| 2 |", tracker.render_markdown(loaded))

    def test_grade_h2h(self):
        from betfinder.tracker import grade_h2h
        scores = [{"name": "Detroit Lions", "score": "27"}, {"name": "Minnesota Vikings", "score": "20"}]
        self.assertEqual(grade_h2h("Detroit Lions", scores), "won")
        self.assertEqual(grade_h2h("Minnesota Vikings", scores), "lost")
        self.assertEqual(grade_h2h("Draw", scores), "lost")
        self.assertIsNone(grade_h2h("Somebody", scores))
        self.assertEqual(grade_h2h("Draw", [{"name": "A", "score": "1"}, {"name": "B", "score": "1"}]), "won")


class GradeLinesTest(unittest.TestCase):
    def test_spreads_and_totals(self):
        from betfinder.tracker import grade_h2h
        sc = [{"name": "Las Vegas Raiders", "score": "20"}, {"name": "New York Jets", "score": "21"}]
        self.assertEqual(grade_h2h("Las Vegas Raiders +1.5", sc), "won")
        self.assertEqual(grade_h2h("Las Vegas Raiders +1", sc), "push")
        self.assertEqual(grade_h2h("New York Jets -1.5", sc), "lost")
        self.assertEqual(grade_h2h("Over 40.5", sc), "won")
        self.assertEqual(grade_h2h("Under 41", sc), "push")


class ConsensusSanityTest(unittest.TestCase):
    def test_broken_book_excluded_from_consensus(self):
        from betfinder.scanner import _fair_probs
        books = {b: {"A": 1.91, "B": 1.91} for b in ("b1", "b2", "b3", "b4")}
        books["broken"] = {"A": 1.45, "B": 1.56}
        probs, source = _fair_probs(books, ScanConfig())
        self.assertEqual(source, "consensus(4)")
        self.assertAlmostEqual(probs["A"], 0.5)


class FeeTest(unittest.TestCase):
    def test_kalshi_fee_lowers_odds(self):
        from betfinder.scanner import net_odds
        self.assertEqual(net_odds("novig", 2.0), 2.0)
        # 50c contract + 0.07*0.25 = 1.75c fee -> 1/0.5175
        self.assertAlmostEqual(net_odds("kalshi", 2.0), 1 / 0.5175)


class LineConversionTest(unittest.TestCase):
    def test_reproduces_sharp_price_on_its_own_line(self):
        from betfinder.lines import calibrate, parse_bet
        m = calibrate("icehockey_nhl", "totals", "H", {"Over 7": 2.16, "Under 7": 1.77}, "pin")
        inv_o, inv_u = 1 / 2.16, 1 / 1.77
        self.assertAlmostEqual(m.fair_odds(parse_bet("totals", "Under 7", "H")), 1 + inv_o / inv_u, places=4)

    def test_ducks_under_6_5(self):
        # Pinnacle had Under 7 at 1.77; Novig offered Under 6.5 at 2.35.
        from betfinder.lines import calibrate, parse_bet
        m = calibrate("icehockey_nhl", "totals", "H", {"Over 7": 2.16, "Under 7": 1.77}, "pin")
        fair = m.fair_odds(parse_bet("totals", "Under 6.5", "H"))
        self.assertTrue(2.0 < fair < 2.25, fair)
        self.assertGreater(m.ev(parse_bet("totals", "Under 6.5", "H"), 2.35), 0.04)

    def test_spread_extra_points_help_underdog(self):
        from betfinder.lines import calibrate, parse_bet
        home = "Houston Cougars"
        m = calibrate("basketball_ncaab", "spreads", home,
                      {"Houston Cougars -29.5": 1.86, "La Salle Explorers +29.5": 1.96}, "pin")
        f295 = m.fair_odds(parse_bet("spreads", "La Salle Explorers +29.5", home))
        f30 = m.fair_odds(parse_bet("spreads", "La Salle Explorers +30", home))
        f31 = m.fair_odds(parse_bet("spreads", "La Salle Explorers +31", home))
        self.assertTrue(f31 < f30 < f295)
        self.assertTrue(1.85 < f30 < 2.0, f30)

    def test_quarter_line_sits_between_neighbours(self):
        from betfinder.lines import calibrate, parse_bet
        m = calibrate("soccer_epl", "totals", "H", {"Over 2.5": 1.95, "Under 2.5": 1.95}, "pin")
        o25 = m.fair_odds(parse_bet("totals", "Over 2.5", "H"))
        o275 = m.fair_odds(parse_bet("totals", "Over 2.75", "H"))
        o3 = m.fair_odds(parse_bet("totals", "Over 3", "H"))
        self.assertTrue(o25 < o275 < o3)

    def test_scanner_uses_conversion(self):
        from datetime import datetime, timedelta, timezone
        start = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        ev = [{"sport_key": "icehockey_nhl", "sport_title": "NHL", "commence_time": start,
               "home_team": "Vancouver Canucks", "away_team": "Anaheim Ducks", "bookmakers": [
                   {"key": "pinnacle", "markets": [{"key": "totals", "outcomes": [
                       {"name": "Over", "point": 7.0, "price": 2.16}, {"name": "Under", "point": 7.0, "price": 1.77}]}]},
                   {"key": "novig", "markets": [{"key": "totals", "outcomes": [
                       {"name": "Over", "point": 6.5, "price": 1.71}, {"name": "Under", "point": 6.5, "price": 2.35}]}]},
               ]}]
        bets, _ = scan_events(ev, ScanConfig(bet_books=("novig",)))
        self.assertEqual([b.selection for b in bets], ["Under 6.5"])
        self.assertIn("converted", bets[0].fair_source)


class KeyNumberTest(unittest.TestCase):
    def test_football_spread_not_converted_across_3(self):
        from betfinder.lines import crosses_key_number
        self.assertTrue(crosses_key_number("americanfootball_ncaaf", "spreads", -3, -3.5))
        self.assertTrue(crosses_key_number("americanfootball_nfl", "spreads", 7, 6.5))
        self.assertFalse(crosses_key_number("americanfootball_nfl", "spreads", -4, -4.5))
        self.assertFalse(crosses_key_number("basketball_nba", "spreads", -3, -3.5))
        self.assertFalse(crosses_key_number("americanfootball_nfl", "totals", 43, 43.5))


class PropsTest(unittest.TestCase):
    def _event(self, books):
        from datetime import datetime, timedelta, timezone
        start = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        return {"sport_key": "basketball_nba", "sport_title": "NBA", "commence_time": start,
                "home_team": "H", "away_team": "A",
                "bookmakers": [{"key": k, "markets": m} for k, m in books.items()]}

    @staticmethod
    def _pp(player, line, over, under):
        return {"key": "player_points", "outcomes": [
            {"name": "Over", "description": player, "point": line, "price": over},
            {"name": "Under", "description": player, "point": line, "price": under}]}

    def test_players_with_same_line_stay_separate(self):
        from betfinder.scanner import _collect_lines
        ev = self._event({"novig": [self._pp("P1", 24.5, 1.9, 1.9), self._pp("P2", 24.5, 2.5, 1.5)]})
        keys = [k for k in _collect_lines(ev) if k[0] == "player_points"]
        self.assertEqual(len(keys), 2)

    def test_prop_edge_uses_other_books_only(self):
        books = {b: [self._pp("P1", 24.5, 1.91, 1.91)] for b in ("fanduel", "prophetx", "polymarket")}
        books["novig"] = [self._pp("P1", 24.5, 2.15, 1.75)]
        bets, _ = scan_events([self._event(books)], ScanConfig(bet_books=("novig",)))
        self.assertEqual([(b.selection, b.book) for b in bets], [("P1 Over 24.5", "novig")])
        self.assertIn("others(3)", bets[0].fair_source)
        self.assertGreater(bets[0].ev, 0.05)

    def test_prop_needs_enough_other_books(self):
        books = {b: [self._pp("P1", 24.5, 1.91, 1.91)] for b in ("fanduel", "prophetx")}
        books["novig"] = [self._pp("P1", 24.5, 2.3, 1.6)]
        bets, _ = scan_events([self._event(books)], ScanConfig(bet_books=("novig",)))
        self.assertEqual(bets, [])

    def test_alternate_total_matches_pinnacle_line(self):
        pin_alt = {"key": "alternate_totals", "outcomes": [
            {"name": "Over", "point": 236.5, "price": 1.95}, {"name": "Under", "point": 236.5, "price": 1.95}]}
        nov_alt = {"key": "alternate_totals", "outcomes": [
            {"name": "Over", "point": 236.5, "price": 2.10}, {"name": "Under", "point": 236.5, "price": 1.80}]}
        bets, _ = scan_events([self._event({"pinnacle": [pin_alt], "novig": [nov_alt]})],
                              ScanConfig(bet_books=("novig",)))
        self.assertEqual([b.selection for b in bets], ["Over 236.5"])
        self.assertEqual(bets[0].fair_source, "pinnacle")
        self.assertEqual(bets[0].market, "totals")


class PeriodMarketTest(unittest.TestCase):
    def test_period_markets_treated_like_props(self):
        from betfinder.scanner import is_prop
        self.assertTrue(is_prop("h2h_h1"))
        self.assertTrue(is_prop("totals_q1"))
        self.assertTrue(is_prop("player_points_rebounds_assists"))
        self.assertFalse(is_prop("alternate_totals"))
        self.assertFalse(is_prop("h2h"))
