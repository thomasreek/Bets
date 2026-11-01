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

Prices that haven't been updated for 3 hours are ignored, because a stale price looks like a fake edge. Change this with `--max-age`.

**API credits:** each scan costs sports × markets × regions credits. `--sports all` can cost over 100 credits, so the tool refuses any scan that would cost more than `--max-cost` (default 60). Keep `eu` in `--regions`, because that's where the Pinnacle line comes from. By default it also scans `us2` (ESPN BET, Hard Rock, Fliff) and `us_ex`, which covers exchanges and prediction markets: Kalshi, Novig, Polymarket US, ProphetX and BetOpenly. Each region adds to the credit cost. Exchange and prediction-market prices may not include their trading fees, so a small edge there can disappear after fees. Arbitrage margins under 1% are hidden for the same reason.

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
