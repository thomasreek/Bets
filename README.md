# betfinder

Finds positive expected value (+EV) bets at US sportsbooks. It doesn't try to predict who wins. Instead it:

1. Takes the **sharp line** from Pinnacle, a low-margin bookmaker that accepts big bets. Pinnacle's prices are the best public estimate of each outcome's true probability.
2. **Removes the margin** ("de-vigs" the line) to get fair probabilities. The power method is the default.
3. Flags any DraftKings, FanDuel, BetMGM, Caesars, etc. price that pays more than fair: `EV = fair_prob × odds − 1`.
4. Suggests a stake with **quarter Kelly**, capped at 3% of your bankroll per bet.
5. Also reports **arbitrage**: price gaps between US books where backing every outcome guarantees a profit.

If Pinnacle doesn't offer a line (common in smaller tennis events), it uses the average de-vigged price across at least 4 books instead. That estimate is weaker, and the output labels it `fair from consensus(N)`.

## Setup

Requires Python 3.10+ and nothing else. Two odds sources are supported:

- **Betting Data API** (<https://bettingdataapi.com>): `export BETTINGDATA_API_KEY=your_key`. Used automatically when this variable is set.
  - Free plan: NFL, NBA and MLB moneylines from about 8 US books, 25 requests a day, no Pinnacle.
  - Pro ($29/month): adds Eredivisie, EPL and other soccer leagues, NHL, tennis, spreads and totals.
  - Business: adds Pinnacle, the most reliable reference for fair prices.
- **The Odds API** (<https://the-odds-api.com>): `export ODDS_API_KEY=your_key`. The free tier gives 500 credits a month and includes Pinnacle via the `eu` region.

Pick a source explicitly with `--provider bettingdata` or `--provider oddsapi`.

## Usage

```bash
python -m betfinder scan --demo            # try it with bundled sample data, no key needed
python -m betfinder sports                 # list in-season sports (free)
python -m betfinder scan                   # NFL, NBA, NHL, EPL, all active tennis
python -m betfinder scan --sports soccer,tennis --markets h2h,totals
python -m betfinder scan --books draftkings,fanduel --bankroll 500 --min-ev 0.03
python -m betfinder scan --sports all --max-cost 200 --csv bets.csv
python -m betfinder scan --sports eredivisie        # league names work too
```

**Only show books you can use.** `--books` takes a list of bookmaker keys or a preset:

| Preset | Books |
|---|---|
| `prediction` | Kalshi, Polymarket US, Novig, ProphetX, BetOpenly |
| `offshore` | Bovada, BetOnline, MyBookie, BetUS, LowVig, BetAnySports, Everygame, GTBets, BetWhale |
| `texas` | both of the above (Texas has no licensed sportsbooks) |
| `mine` | Novig, Kalshi, Polymarket US, ProphetX, Bovada, BetUS, MyBookie: the accounts actually in use (BetOpenly left out: most bets there never fill) |

A preset also limits the API regions to the ones it needs, which saves credits. To make it permanent, run `export BETFINDER_BOOKS=texas`.

Offshore books aren't licensed or regulated in the US. If one refuses to pay out, you have no regulator to complain to.

**Different lines are compared too.** If a book offers Under 6.5 and Pinnacle only prices Under 7, or a book has +30 and Pinnacle has +29.5, the scanner converts Pinnacle's price to the book's line (`betfinder/lines.py`). It models the final total or margin as a normal curve calibrated to Pinnacle's price, with a typical spread per sport, and handles pushes and quarter lines. These rows say `converted` in the output. Football is limited to 1-point conversions, and football spreads are never converted across the key numbers 3, 7, 10 or 14 (Pinnacle -3 vs a book's -3.5 is skipped), because final margins land on those far more often than the model assumes.

Prices that haven't been updated for 3 hours are ignored, because a stale price looks like a fake edge. Change this with `--max-age`.

**API credits:** each scan costs sports × markets × regions credits. `--sports all` can cost over 100 credits, so the tool refuses any scan that would cost more than `--max-cost` (default 60). Keep `eu` in `--regions`, because that's where the Pinnacle line comes from. By default it also scans `us2` (ESPN BET, Hard Rock, Fliff) and `us_ex`, which covers exchanges and prediction markets: Kalshi, Novig, Polymarket US, ProphetX and BetOpenly. Each region adds to the credit cost. Kalshi's trading fee (7% × price × (1 − price) per contract) and ProphetX's 2% fee on net winnings are taken off their prices before the edge is calculated. Other exchange and prediction-market prices may not include their trading fees, so a small edge there can disappear after fees. Arbitrage margins under 1% are hidden for the same reason.

## Props and alternate lines

```bash
python -m betfinder scan --props --sports basketball_nba --hours 6
python -m betfinder scan --props --sports basketball_ncaab,icehockey_nhl --hours 4 --max-cost 300
```

`--props` fetches each game separately (games starting within `--hours`) with alternate spreads and totals, and for NBA, NHL and NFL also player props. Alternate lines are compared with Pinnacle's matching main or alternate line, or converted from the nearest one. A player prop on the same line as Pinnacle is compared with Pinnacle directly. On a nearby line (within 1 for counts, about 12% for bigger stats like points) it is priced from Pinnacle's level plus the average change in the *other* books' ladders between the two lines (`betfinder/props.py`, at least 2 books), and only when that chance is between 33% and 67%; tails and far lines are skipped. Only when Pinnacle has no line for the player is the fair price the average of the other books (at least 3). The bar for props is 5% (`prop_min_ev`).

**Cost:** per game, markets x regions. A 6-game NBA night with 8 markets and 3 regions is about 144 credits, so this is meant for a paid plan.

## Tracking your bets

Every bet you place goes in `bet_log.csv`, and `BETS.md` shows the table with your record, profit and ROI.

```bash
python -m betfinder bet add --event "Minnesota Vikings @ Detroit Lions" --selection "Detroit Lions" \
    --stake 16.68 --to-win 10 --book kalshi --sport americanfootball_nfl \
    --start 2026-11-01T18:00:00Z --fair 1.58
python -m betfinder bet                    # show all bets and profit
python -m betfinder bet settle 1 won       # record a result by hand (won, lost, push, void)
python -m betfinder bet settle --auto      # grade moneyline bets from final scores (2 credits per sport)
```

Game times in the scan output and `BETS.md` are shown in Houston time. Set `BETFINDER_TZ` to change it, e.g. `export BETFINDER_TZ=America/New_York`.

Copy `--event`, `--sport` and `--fair` from the scan output. `--fair` lets the log compare your actual profit with expected profit.

## Reading the output

| Column | Meaning |
|---|---|
| EV | Expected return per $1 staked. 3% means $3 of profit per $100 on average |
| Odds | The book's price, decimal (American) |
| Fair | Fair decimal odds after removing the margin. You want Odds > Fair |
| Stake | Suggested bet on your bankroll |

Rows marked ⚠ CHECK have suspiciously high EV, which almost always means stale or mismatched data. Verify these by hand.

## Things to know before betting real money

- **EV is long-run.** A 3% edge still loses often. You need hundreds of bets before results reflect the edge. Size stakes small (the defaults already do).
- **Prices move.** Re-check the price at the book before you bet. The data can be several minutes old.
- **Books limit winners.** US books often cap stakes for accounts that consistently beat sharp lines.
- **Track closing line value (CLV):** if your bets regularly beat the price at kickoff, the approach is working.
- Only use books that are licensed in your state. Gamble only what you can afford to lose. Problem gambling help is at 1-800-GAMBLER.

## Tests

```bash
python -m unittest -v
```
