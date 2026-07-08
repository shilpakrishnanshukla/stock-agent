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
liquid US stocks and every candidate is scored out of **55 points** across
three categories, then the **top 15 scorers** (whatever their score) become
the watchlist:

**Category 1 - Trend (25 pts)**
- Price above 20-day EMA: +10
- 20-day EMA above 50-day EMA: +10
- Higher highs (last 10 sessions' high above the prior 10 sessions' high): +5

**Category 2 - Momentum (20 pts)**
- RSI(14) 50-60: +10 / 60-65: +8 / 65-70: +5 / above 70: +0
- Volume vs the 20-day average: above average: +10 / in line with average: +5 / below average: +0

**Category 3 - Earnings impact (10 pts)**
- Earnings within the next 5 trading days: +0
- Earnings in 6-10 trading days: +5
- Earnings further out (or none found): +10

Every email shows the full score breakdown per ticker (total, and each
category's sub-score) alongside the underlying numbers (price, 20EMA, 50EMA,
RSI, volume ratio, days to next earnings). Changes are logged with the actual
score for both drops and adds, and the updated `portfolio.json` is committed
back to the repo automatically, same as before.

Note: earnings dates come from Yahoo Finance's calendar data, which isn't
always populated for every ticker - if no date is found, it's treated as "no
near-term earnings risk visible" and scored favorably (+10), rather than
penalized for missing data.

You can still manually add or remove names from `watchlist_us`; the next
automated run will re-score and re-rank from there regardless.

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
