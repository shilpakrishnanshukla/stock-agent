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

## The watchlist is now self-maintaining

You no longer need to hand-edit the watchlist. Every run, the script asks
Claude (with live web search) to look at current market trends, sector
momentum, and analyst activity, then:

- **drops** names whose thesis has played out or gone stale
- **adds** names where something genuinely new is happening (a real catalyst,
  analyst action, valuation shift) - not just well-known tickers for the sake
  of it

It's capped at 15 names so it doesn't run away. Every change is explained in
the email under "WATCHLIST CHANGES TODAY," and the script commits the updated
`portfolio.json` back to the repo automatically after each run - so what you
see on GitHub always reflects the latest list.

You can still manually add or remove watchlist names any time by editing the
file yourself; the automated run will just keep curating from there.

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
