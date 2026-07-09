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

## The watchlist is now a weighted scoring system (US only)

The SG watchlist stays paused. The US watchlist is screened daily across ~150
liquid US stocks using a **two-stage gate**, then ranked:

**Stage 1 - base score minimum.** Trend + Momentum + Earnings (the original
three categories, out of 55) must score **at least 45/55**. Anything below
that is rejected immediately - it never even reaches the reward:risk check.

**Stage 2 - reward:risk minimum.** Of what clears Stage 1, anything with a
reward:risk below **2.5** is also rejected. If no confirmed support/resistance
exists at all (so reward:risk can't be computed), that also fails this stage
- there's no way to confirm a favorable setup without a real number.

**Then rank and cap.** Whatever survives both gates is ranked by total score
out of 85 (Trend+Momentum+Earnings+Location), and the **top 15** become the
watchlist.

This means a technically strong-looking name (great trend, great RSI) still
gets rejected if its risk/reward at current price isn't favorable, and a
name with a great reward:risk still gets rejected if its underlying trend and
momentum are weak. Both conditions have to hold.

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
