# Daily Stock Note Agent

Runs once a day, on its own, with no action from you. It:
- Reads `portfolio.json` for your holdings and watchlist
- Pulls current prices via Yahoo Finance
- Asks Claude (with live web search) for a buy/sell/hold read on each name,
  grounded in that day's actual news and analyst activity
- Emails you a plain-text note

It never places trades. It only reads and reports.

## One-time setup (about 10 minutes)

### 1. Create a GitHub repo
Go to github.com, create a new **private** repository, and upload these files
(or `git push` them) keeping the folder structure intact, especially
`.github/workflows/daily.yml`.

### 2. Get an Anthropic API key
Go to [console.anthropic.com](https://console.anthropic.com), create an API key.
Note: this uses the pay-as-you-go API, separate from your claude.ai subscription
— a daily run like this costs a small fraction of a cent to a few cents per day
depending on portfolio size.

### 3. Create a Gmail "App Password"
(You need 2-factor auth turned on for your Google account first.)
Go to Google Account → Security → 2-Step Verification → App Passwords,
generate one for "Mail". You'll get a 16-character password — use that,
not your normal Gmail password.

(If you'd rather not use Gmail, swap the `send_email` function in `analyze.py`
for any transactional email API like Resend or SendGrid instead.)

### 4. Add secrets to your GitHub repo
In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add these four:

| Secret name          | Value                                   |
|-----------------------|------------------------------------------|
| `ANTHROPIC_API_KEY`   | your key from console.anthropic.com      |
| `EMAIL_ADDRESS`       | the Gmail address sending the note       |
| `EMAIL_APP_PASSWORD`  | the 16-character App Password            |
| `TO_EMAIL`            | where you want the note delivered        |

### 5. Turn it on
Go to the **Actions** tab in your repo → you should see "Daily Stock Note" →
click "Enable workflow" if prompted. That's it — it will now run automatically
every weekday at 5:30pm US Eastern (after market close).

To test it immediately rather than waiting: Actions tab → Daily Stock Note →
**Run workflow** button.

## Logging a buy or sell

Edit `portfolio.json` directly and commit the change (GitHub's web editor works
fine for this, no need for git on your machine):

- **New buy**: add an object to `holdings` with `ticker`, `shares`, `cost_basis`
  (your actual purchase price), and `date_bought`.
- **Sell**: remove it from `holdings` and add it to `closed_positions` with
  `sold_price` and `date_sold`, so you keep a record.

## The daily report is now decision-first

The email used to lead with tables. It now leads with a plain-English
decision summary, and moves all the noisy detail either into a compact
form or into the console/GitHub Actions log (never both the full detail
and a summary in the email body).

**Six sections, in order:**

1. **Decision summary** - one paragraph, the single most important message
   first: how many holdings, how many need action, how many candidates
   passed, the strongest one, market open/closed status, and whether any
   data-quality issues exist. Example: *"No current holdings. One candidate
   passed: CRWD. Fixed-buffer reward:risk 3.29x, but execution is on hold
   (price-history columns missing from the data feed) - do not place a
   trade from the automated recommendation until that's resolved."*
2. **Portfolio actions** - `Current holdings: N`, `Portfolio actions
   required: N` (a real count, derived from a structured HOLD/TRIM/SELL/ADD
   verdict Claude returns alongside its written analysis - not a guess),
   then the holdings table and written analysis. Says "Current holdings:
   None" plainly when empty, not a paragraph of prose.
3. **New trade candidates** - the qualification bar stated in one line
   (`Base score >= 45/55 and reward:risk >= 2.5x`, full methodology lives
   in this README, not repeated daily), then each candidate's **two
   separate statuses**:
   - **Setup status**: `Qualified` - it passed both gates, this doesn't change
   - **Execution status**: `Ready`, or `Hold - <specific reason>` if the
     ATR or fixed-buffer sizing plan couldn't be completed
   A candidate that qualifies but has incomplete sizing data is never
   silently labeled just "candidate" - the report is explicit that setup
   quality and execution readiness are different things.
4. **Rejected / watch names** - counts only (`Stage 1 passed: 20`,
   `Rejected on reward:risk: 19`, `Final candidates: 1`), plus the
   **5 closest rejects** by reward:risk so you can see what almost made it.
   The full Stage 1/Stage 2 detail still exists - it's printed to the
   console/GitHub Actions log on every run, just not in the email.
5. **Market and pre-market validation** - the pre-market gap check on
   weekdays; on weekends, this correctly says "Not applicable - weekend,
   US market closed" instead of showing an empty or misleading table.
6. **Data-quality alerts** - a clean, human-readable list of anything that
   failed (e.g. *"CRWD: ATR plan unavailable - not enough price history."*).
   Raw Python exceptions are never shown here - they're only ever printed
   to the console/log for debugging, never emailed.

**Weekend handling**: if the report runs on a Saturday or Sunday, it
labels itself **"WEEKEND STRATEGY REVIEW"** instead of "DAILY PRE-MARKET
NOTE", states plainly which prior weekday's close the figures reflect, and
marks pre-market data as not applicable rather than showing a confusing or
empty section.

**Data freshness header**: every email opens with `Generated:` (timestamp),
`Price data through:` (which close the figures reflect), and `Premarket
data:` (applicable or not) - so there's never ambiguity between live,
overnight, and stale figures.

**Terminology, standardized throughout**: stop-loss (not "stop"/"SL"),
take-profit target (not target/upper limit/resistance/max profit mixed),
reward:risk (spelled out, not alternating with "R:R" as if they were
different things), and position value (not invest/investment
interchangeably). The watchlist table's pivot columns are labeled
`LAST PIV HI`/`LAST PIV LO` (the most recent confirmed swing pivot, used
for the higher-highs trend check) so they're never confused with the
separate `NEAREST SUPP`/`NEAREST RESIST` columns in the support/resistance
table, or the exit plan's distinct "nearest resistance" vs. "major
resistance" - three related but different numbers that used to look like
they might be duplicates of each other.

## The watchlist is now a weighted scoring system (US only)

The SG watchlist stays paused. The US watchlist is screened daily across ~150
liquid US stocks using a **two-stage gate**, then ranked:

**Stage 1 - base score minimum.** Trend + Momentum + Earnings (the original
three categories, out of 55) must score **at least 45/55**. Anything below
that is rejected immediately - it never even reaches the reward:risk check.
Only the **top 20** by base score are kept as the Stage 1 shortlist that
proceeds to Stage 2 (shown in full in the email, so you can see exactly
what cleared the bar and how it scored on each of the three categories).

**Stage 2 - reward:risk minimum.** Of that Stage 1 shortlist, anything with a
reward:risk below **2.5** is rejected. If no confirmed support/resistance
exists at all (so reward:risk can't be computed), that also fails this stage
- there's no way to confirm a favorable setup without a real number. Every
elimination at this stage is shown in the email with its base score and its
actual reward:risk (or "n/a" if no levels were found), so you can see exactly
which Stage 1 names got cut here and why - this used to be invisible.

**Then rank and cap.** Whatever survives both gates is ranked by total score
out of 85 (Trend+Momentum+Earnings+Location), and the **top 15** become the
final watchlist.

This means a technically strong-looking name (great trend, great RSI) still
gets rejected if its risk/reward at current price isn't favorable, and a
name with a great reward:risk still gets rejected if its underlying trend and
momentum are weak. Both conditions have to hold. The email now shows all
three stages of this funnel in order: the Stage 1 shortlist, what got
eliminated at Stage 2, and the final ranked watchlist.

**Category 1 - Trend (25 pts)**
- Price above 20-day EMA: +10
- 20-day EMA above 50-day EMA: +10
- Higher highs: +5 - based on actual confirmed swing pivots (a bar whose
  high is the highest within 2 bars either side), not a rough rolling-window
  comparison. The most recent pivot high must sit above the one before it.
  The same pivot logic also tracks higher lows, shown in the email table for
  context (HH/HL flags and the actual pivot price levels), though only
  higher highs currently affects the score, matching your original spec.

**Category 2 - Momentum (20 pts)**
- RSI(14) 50-60: +10 / 60-65: +8 / 65-70: +5 / above 70: +0
- Volume vs the 20-day average: above average: +10 / in line with average: +5 / below average: +0

**Category 3 - Earnings impact (10 pts)**
- Earnings within the next 5 trading days: +0
- Earnings in 6-10 trading days: +5
- Earnings further out (or none found): +10

**Category 4 - Location vs support/resistance (30 pts, only scored for names that clear both gates above)**
- Nearest support = closest confirmed pivot low below the current price;
  nearest resistance = closest confirmed pivot high above it
- Distance above support: <=3% away: +15 / <=6%: +10 / <=10%: +5 / further: +0
- Room below resistance: >=8%: +5 / >=5%: +3 / less: +0
- Reward:risk (target = resistance, stop = support minus a 0.5% buffer):
  >=4: +10 / >=3: +8 / >=2.5: +5 (anything below 2.5, or with no confirmed
  levels at all, was already rejected at Stage 2 and never reaches this
  scoring step)

Every email shows the full score breakdown per ticker (total out of 85, plus
each category's sub-score) alongside the underlying numbers (price, 20EMA,
50EMA, RSI, volume ratio, pivot levels, days to next earnings). A separate
table also shows support, resistance, stop, target, risk, reward, and R:R for
each ticker directly, sorted best R:R first. Changes are logged with the
actual reasoning for both drops and adds - including a specific "reward:risk
below 2.5 floor" note when that's why something didn't make the cut - and the
updated `portfolio.json` is committed back to the repo automatically.

Note: earnings dates come from Yahoo Finance's calendar data, which isn't
always populated for every ticker - if no date is found, it's treated as "no
near-term earnings risk visible" and scored favorably (+10), rather than
penalized for missing data.

You can still manually add or remove names from `watchlist_us`; the next
automated run will re-score, re-rank, and re-apply the R:R floor from there
regardless.

## Trade plan (position sizing)

Every ticker that survives the two-stage gate also gets an actual position
size, using the entry/stop/target already computed from the pivot-based
support/resistance:

- **Max risk per trade** = portfolio value x max risk % (default 1%)
- **Max position size** = portfolio value x max position % (default 15%)
- **Shares** = the smaller of (risk cap / risk per share) and (position cap /
  entry price), rounded down - whichever constraint binds first
- Shows entry, stop, target, shares, dollar investment, max dollar loss, max
  dollar profit, and the reward:risk ratio for each name

These three inputs live in `portfolio.json` under a `trade_settings` key:

```json
"trade_settings": {
  "portfolio_value": 10000,
  "max_risk_pct": 0.01,
  "max_position_pct": 0.15
}
```

Edit these any time to match your actual account size and risk tolerance -
the next run picks up the new values automatically. If you don't add this
key at all, it defaults to the values above and gets added to the file
automatically on the next run.

This is a sizing calculation based on the stop/target already derived from
technical levels, not a recommendation of how much you personally should
risk - adjust the percentages to whatever you're actually comfortable with.

## ATR-based trade plan (second, alternative sizing)

A second trade plan runs alongside the one above, using a **volatility-
adjusted stop** instead of a fixed 0.5% buffer below support:

- **ATR(14)** (Average True Range, Wilder-smoothed) measures how much a
  stock typically moves per day
- **Stop** = nearest pivot support minus 0.75x ATR - so a stock that
  normally swings $3/day gets more breathing room than one that swings
  $0.30/day, rather than both getting the same flat percentage
- **Target** = nearest pivot resistance, same as before
- Each ticker gets a **status**: `CANDIDATE` (R:R >= 3 and sizing works),
  `WATCH` (R:R between 2.5 and 3), or `PASS` (R:R below 2.5, position
  rounds to 0 shares, or no usable support/resistance was found nearby)

This is shown as a separate table alongside the original trade plan - not a
replacement - so you can compare a fixed-buffer stop against a volatility-
adjusted one side by side. Both use the same `trade_settings` from
`portfolio.json`.

## Exit plan - multi-target scale-out

For every ticker with a valid ATR trade plan, a further section plans **how
to sell**, not just where to buy - since a single fixed target rarely
matches how a real position gets managed.

Up to three exit targets are built from independent sources:

- **ATR extension** (3x ATR above entry) - a volatility-based stretch target
  that doesn't depend on chart structure at all
- **Nearest resistance** - the closest confirmed pivot high within the last
  60 sessions, the first likely area of selling pressure
- **Major resistance** - the highest confirmed pivot high within the last
  ~250 sessions (roughly a year) that's meaningfully higher than the
  nearest one (at least 1% further out, so it isn't just a near-duplicate) -
  a stretch target for a partial "runner" position

Each candidate is **scored out of 100** across five factors: reward:risk (up
to 30), how closely it aligns with actual chart structure vs. being a pure
volatility projection (up to 30), RSI (up to 15), relative volume (up to
15), and earnings timing (+10 if clear, -15 penalty if earnings fall within
5 trading days). Near-duplicate targets (within 0.25x ATR of each other) are
merged, keeping whichever scored higher.

The **PRIMARY TARGET** is the highest-scored candidate among those that
clear a 2.5 reward:risk floor. If nothing clears that floor, **no primary
target is shown** - the plan reports status `NO_TARGET_MEETS_MINIMUM_RR`
rather than quietly settling for a weaker one. This is a real behavior
worth knowing: unlike some of the other sections in this tool, there's no
fallback here - "no good exit target found today" is a valid, visible
outcome.

Shares are allocated with a fixed scheme: **30% at target 1, 40% at target
2, the remainder as a runner** (or 70% at target 1 / 30% runner if only one
target candidate exists).

## SG (SGX) watchlist - separate daily email at 8:30am SGT

A second script, `analyze_sg.py`, runs the exact same scoring engine
(Trend/Momentum/Earnings/Location, the two-stage gate, pivot-based
support/resistance, and position sizing) against a curated universe of
30 liquid SGX names (the current STI constituents), and emails a separate
note at **8:30am SGT** - just ahead of SGX's 9:00am open.

It shares `portfolio.json` with the US script: your holdings and
`trade_settings` are common to both, while `watchlist_sg` is tracked and
auto-curated separately from `watchlist_us`. It's capped at the top 10
(smaller than the US list's 15, since SGX has far less liquid breadth to
draw from) with a top-15 Stage 1 shortlist (vs the US script's top 20).

Deploy it the same way as the US script - upload `analyze_sg.py` and
`.github/workflows/daily_sg.yml` to your repo alongside the existing files.
It uses the same 4 secrets you've already set up (`ANTHROPIC_API_KEY`,
`EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`, `TO_EMAIL`) - no new secrets needed.

One deliberate omission: **this email does not include a pre-market gap
section.** Yahoo Finance's `preMarketPrice` field is a US-market concept and
doesn't reliably populate for `.SI` tickers, so faking that section would
give you misleading "no data" noise rather than something useful. If you
later want an SGX pre-open equivalent, that would need a different data
source than Yahoo Finance.

Also worth knowing: Yahoo's earnings-calendar coverage is noticeably less
complete for SGX names than US names. Since a missing earnings date scores
favorably by design (+10, "no near-term risk visible"), that means the
Earnings category leans a bit more optimistic-by-default for SG names -
worth treating with slightly more skepticism than the same score on a US
ticker.

## Schedule: now 8pm SGT

The workflow runs at **12:00 UTC = 8:00pm SGT**, which lands right in the US
pre-market window (roughly 8:00am ET during daylight saving, 7:00am ET
otherwise - both are within the 4:00am-9:30am ET pre-market session). This
means the email you get in the Singapore evening reflects that morning's
actual US pre-market activity, ahead of the regular session open at 9:30am ET
(9:30pm SGT that same evening).

## Pre-market gap check (shortlisted names only)

For every ticker that made the final watchlist (after both gates), the email
now includes a pre-market gap check:

```
gap_pct = (premarket_price - previous_close) / previous_close * 100
< 1% gap:  OK - plan unchanged
1-3% gap:  Recalculate entry/RR
> 3% gap:  Review manually - large gap
```

For any ticker with a gap outside the "OK" band, the script makes **one
combined web-search call** covering all of them together, asking for a
concise 2-3 sentence reason per ticker - grounded in that morning's actual
news, not a guess. If it can't find a clear catalyst, it says so rather than
inventing one. Tickers with an "OK" gap don't get an explanation, since
nothing meaningful needs explaining.

If Yahoo doesn't have pre-market data for a ticker at the time the script
runs (uncommon during the pre-market window for liquid large/mid-caps, but
possible), that ticker shows "no pre-market data available" instead of a gap.

## Adjusting the schedule

Edit the `cron` line in `.github/workflows/daily.yml`. Cron format is
`minute hour day month weekday`, always in UTC. For example, to run at
7am US Eastern before market open instead: `"0 11 * * 1-5"`.

## Costs
- GitHub Actions: free for private repos at this usage level (public repos are
  always free).
- Anthropic API: pay-as-you-go, roughly $0.01–0.05 per run depending on
  portfolio size and search depth.
- Email: free with Gmail.

## Limitations, honestly
- This is a screening tool, not a predictive model — treat its reads as one
  input, not a signal to act on mechanically.
- Yahoo Finance data can lag or occasionally hiccup; the script will note an
  error for a ticker rather than crash the whole run.
- It does not know your tax situation, risk tolerance, or the rest of your
  portfolio outside this file — keep `portfolio.json` accurate for it to be
  useful.
