"""
Daily stock analysis agent.

What it does, every time it runs:
  1. Loads your portfolio.json (holdings + watchlist you maintain by hand).
  2. Pulls current prices, day change, and 52-week range for every ticker via yfinance.
  3. Sends that data to Claude (with the web_search tool turned on) and asks for a
     genuine buy/sell/hold read on each holding and watchlist name, grounded in
     today's actual news and analyst activity - not just price data.
  4. Emails you a plain-text note with a P/L table + Claude's reasoning.

This is a research/screening tool, not a trading bot - it never places trades.
It is not financial advice; treat its output as one input among several.

Required environment variables (set as GitHub Actions secrets, see README):
  ANTHROPIC_API_KEY   - your Anthropic API key
  EMAIL_ADDRESS       - Gmail address the note is sent FROM
  EMAIL_APP_PASSWORD  - Gmail App Password (not your normal password)
  TO_EMAIL            - address the note is sent TO (can be same as EMAIL_ADDRESS)
"""

import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText

import yfinance as yf
import anthropic

PORTFOLIO_FILE = "portfolio.json"
MODEL = "claude-sonnet-4-6"


def load_portfolio():
    with open(PORTFOLIO_FILE, "r") as f:
        return json.load(f)


def fetch_snapshot(ticker):
    """Pull current price, day change, and 52w range for one ticker."""
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        price = info.get("lastPrice") or info.get("last_price")
        prev_close = info.get("previousClose") or info.get("previous_close")
        year_high = info.get("yearHigh") or info.get("year_high")
        year_low = info.get("yearLow") or info.get("year_low")
        day_change_pct = None
        if price and prev_close:
            day_change_pct = round((price - prev_close) / prev_close * 100, 2)
        off_high_pct = None
        if price and year_high:
            off_high_pct = round((price - year_high) / year_high * 100, 2)
        return {
            "ticker": ticker,
            "price": round(price, 2) if price else None,
            "day_change_pct": day_change_pct,
            "year_high": round(year_high, 2) if year_high else None,
            "year_low": round(year_low, 2) if year_low else None,
            "off_52w_high_pct": off_high_pct,
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}


def build_holdings_table(holdings, snapshots):
    rows = []
    total_cost = 0.0
    total_value = 0.0
    for h in holdings:
        snap = snapshots.get(h["ticker"], {})
        price = snap.get("price")
        cost_basis = h["cost_basis"]
        shares = h["shares"]
        cost = cost_basis * shares
        value = price * shares if price else None
        pl_pct = round((price - cost_basis) / cost_basis * 100, 2) if price else None
        total_cost += cost
        if value:
            total_value += value
        rows.append({
            **h,
            "current_price": price,
            "unrealized_pl_pct": pl_pct,
            "off_52w_high_pct": snap.get("off_52w_high_pct"),
            "day_change_pct": snap.get("day_change_pct"),
        })
    return rows, total_cost, total_value


def format_table(rows):
    lines = []
    header = f"{'TICKER':8}{'SHARES':>8}{'COST':>10}{'PRICE':>10}{'P/L %':>10}{'TODAY %':>10}{'OFF 52W HI %':>14}"
    lines.append(header)
    lines.append("-" * len(header))
    for r in rows:
        lines.append(
            f"{r['ticker']:8}{r['shares']:>8}{r['cost_basis']:>10.2f}"
            f"{(r['current_price'] or 0):>10.2f}"
            f"{(r['unrealized_pl_pct'] if r['unrealized_pl_pct'] is not None else 0):>10.2f}"
            f"{(r['day_change_pct'] if r['day_change_pct'] is not None else 0):>10.2f}"
            f"{(r['off_52w_high_pct'] if r['off_52w_high_pct'] is not None else 0):>14.2f}"
        )
    return "\n".join(lines)


def get_claude_analysis(holdings_rows, watchlist_snapshots, total_cost, total_value):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    portfolio_summary = json.dumps(holdings_rows, indent=2)
    watchlist_summary = json.dumps(watchlist_snapshots, indent=2)

    prompt = f"""You are producing a daily research note for a personal investor.
Today's date: {datetime.now().strftime('%Y-%m-%d')}

Their current holdings (with cost basis and today's price data already computed):
{portfolio_summary}

Total cost basis: ${total_cost:,.2f}
Total current value: ${total_value:,.2f}

Their watchlist (not yet owned):
{watchlist_summary}

Using web search for current news, analyst ratings, and any material developments
in the last 24-48 hours, write a concise daily note that:

1. For each HOLDING: give a clear read of Hold / Consider Trimming / Consider Adding /
   Consider Selling, with the specific reason (news, earnings, valuation shift,
   technical level, analyst action). Flag anything urgent (bad news, downgrade,
   stock at a key level).
2. For each WATCHLIST name: note if today's price/news makes it more or less
   attractive to open a position, referencing valuation and any fresh catalyst.
3. Keep it tight - a few sentences per ticker, not an essay. Use plain language,
   no headers-heavy report formatting.
4. End with one short paragraph on overall portfolio risk (concentration, sector
   tilt, anything correlated) if relevant.
5. Do not give definitive commands ("sell now"); give reasoned reads the investor
   can act on. Remind them, briefly, this isn't personalized financial advice.

Be honest and specific. If nothing changed for a name, say so briefly rather than
padding it."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )

    text_parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(text_parts)


def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = os.environ["EMAIL_ADDRESS"]
    msg["To"] = os.environ["TO_EMAIL"]

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.environ["EMAIL_ADDRESS"], os.environ["EMAIL_APP_PASSWORD"])
        server.send_message(msg)


def main():
    portfolio = load_portfolio()
    holdings = portfolio.get("holdings", [])
    watchlist = portfolio.get("watchlist", [])

    snapshots = {h["ticker"]: fetch_snapshot(h["ticker"]) for h in holdings}
    watchlist_snapshots = [fetch_snapshot(t) for t in watchlist]

    holdings_rows, total_cost, total_value = build_holdings_table(holdings, snapshots)
    table = format_table(holdings_rows) if holdings_rows else "(no holdings logged yet)"

    try:
        analysis = get_claude_analysis(holdings_rows, watchlist_snapshots, total_cost, total_value)
    except Exception as e:
        analysis = f"[Claude analysis failed: {e}]"

    overall_pl_pct = (
        round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0
    )

    body = f"""DAILY STOCK NOTE - {datetime.now().strftime('%A, %B %d, %Y')}

PORTFOLIO SNAPSHOT
Total cost basis: ${total_cost:,.2f}
Total current value: ${total_value:,.2f}
Overall unrealized P/L: {overall_pl_pct}%

{table}

--------------------------------------------------
ANALYSIS
--------------------------------------------------
{analysis}

--------------------------------------------------
This is an automated research note, not financial advice. Data may be delayed
or incomplete; verify anything before acting on it.
"""

    print(body)  # also shows up in GitHub Actions logs

    if os.environ.get("EMAIL_ADDRESS"):
        send_email(f"Daily Stock Note - {datetime.now().strftime('%Y-%m-%d')}", body)
        print("\nEmail sent.")
    else:
        print("\nEMAIL_ADDRESS not set - skipped sending email.")


if __name__ == "__main__":
    sys.exit(main())
