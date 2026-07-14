"""
Daily stock analysis agent.

What it does, every time it runs:
  1. Loads your portfolio.json (holdings you own + a US watchlist).
  2. Pulls current prices for your holdings via yfinance, and asks Claude
     (with the web_search tool on) for a genuine hold/sell/trim read on each,
     grounded in that day's actual news and analyst activity.
  3. Runs a rules-based technical screen over a broad universe of liquid US
     stocks to maintain the watchlist automatically:
       - Price > 20-day EMA > 50-day EMA (bullish trend alignment)
       - RSI(14) between 50 and 60 (momentum without being overbought)
       - Latest volume compared to the 20-day average volume (above / in line
         with / below average)
     Tickers that stop meeting these criteria are dropped; new tickers that
     newly meet them are added, up to a cap (WATCHLIST_MAX_US).
  4. Emails you a plain-text note with your holdings' P/L + hold/sell read,
     plus the full watchlist with each ticker's technical numbers.

This is a research/screening tool, not a trading bot - it never places trades.
It is not financial advice; treat its output as one input among several.

Required environment variables (set as GitHub Actions secrets, see README):
  ANTHROPIC_API_KEY   - your Anthropic API key
  EMAIL_ADDRESS       - Gmail address the note is sent FROM
  EMAIL_APP_PASSWORD  - Gmail App Password (not your normal password)
  TO_EMAIL            - address the note is sent TO (can be same as EMAIL_ADDRESS)
"""

import json
import math
import os
import smtplib
import sys
from dataclasses import dataclass, asdict
from typing import Any, Optional
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import yfinance as yf
import pandas as pd
import numpy as np
import anthropic

from gdrive_sync import download_workbook, upload_workbook
from trade_planner_writer import update_trade_planner
from trade_journal_reader import read_holdings_from_trade_journal

PORTFOLIO_FILE = "portfolio.json"
MODEL = "claude-sonnet-4-6"
WATCHLIST_MAX_US = 15
TRADE_PLANNER_LOCAL = "trade_planner_temp.xlsx"
TRADE_PLANNER_MARKET = "US"

# User-curated universe: exactly the tickers requested across sector
# messages (Technology, Energy, Industrials, Materials & Mining,
# Consumer/Travel, Healthcare, Oil Services, Gold & Precious Metals,
# Uranium, Crypto Miners, Airlines, Cruise Lines, Casinos, Agriculture),
# deduplicated. Replaces the earlier ~150-ticker generic large-cap list,
# which was too broad/slow for a daily run.
US_CANDIDATE_UNIVERSE = [
    "NVDA","AMD","CRWD","AMAT","SNDK","PLTR","ARM","AVGO",     # Technology
    "XOM","CVX","OXY","DVN","FANG","EQT",                      # Energy
    "SLB","HAL","BKR",                                          # Oil Services
    "NEM","AEM","GOLD","WPM",                                   # Gold & Precious Metals
    "CCJ","UEC","LEU",                                          # Uranium
    "MARA","RIOT","CLSK",                                       # Crypto Miners
    "DAL","UAL","AAL",                                          # Airlines
    "CCL","RCL","NCLH",                                         # Cruise Lines
    "LVS","MGM","WYNN",                                         # Casinos
    "HCA","MRNA","VRTX","REGN",                                 # Healthcare/Biotech
    "CAT","DE","URI",                                           # Industrials
    "NUE","STLD","FCX","AA",                                    # Steel & Metals
    "MOS","CF","NTR",                                           # Agriculture
]

DEFAULT_TRADE_SETTINGS = {
    "portfolio_value": 10_000,
    "max_risk_pct": 0.01,
    "max_position_pct": 0.15,
}


def is_weekend():
    """True on Saturday/Sunday. GitHub Actions runs in UTC, but for the two
    scheduled run times in this project (8pm SGT / 8:30am SGT) the UTC
    weekday matches the SGT weekday exactly - no date-rollover edge case."""
    return datetime.now().weekday() >= 5


def last_trading_day_label():
    """For weekend runs, labels which prior weekday's close the data
    reflects (Saturday -> Friday, Sunday -> Friday)."""
    now = datetime.now()
    weekday = now.weekday()
    if weekday == 5:  # Saturday
        as_of = now - timedelta(days=1)
    elif weekday == 6:  # Sunday
        as_of = now - timedelta(days=2)
    else:
        as_of = now
    return as_of.strftime("%d %b %Y")


def closest_rejects(stage2_eliminated, n=5):
    """The N Stage-2 rejects with the best (closest to qualifying)
    reward:risk, for a short summary instead of printing the full list."""
    with_rr = [e for e in stage2_eliminated if e["reward_risk"] is not None]
    without_rr = [e for e in stage2_eliminated if e["reward_risk"] is None]
    with_rr.sort(key=lambda e: e["reward_risk"], reverse=True)
    return (with_rr + without_rr)[:n]


def get_execution_status(ticker, trade_plans, atr_trade_plans):
    """Separates 'did this ticker qualify' (always Qualified for anything
    on the final watchlist) from 'can we actually act on it today' - since
    a ticker can pass the scoring gates but still have incomplete sizing
    data (e.g. ATR unavailable), which is a materially different situation."""
    fixed_plan = trade_plans.get(ticker)
    atr_plan = atr_trade_plans.get(ticker)
    atr_ready = bool(atr_plan and atr_plan.get("entry") is not None and atr_plan.get("shares", 0) > 0)
    fixed_ready = bool(fixed_plan and fixed_plan.get("shares", 0) > 0)

    if atr_ready and fixed_ready:
        return "Ready"
    if fixed_ready and not atr_ready:
        reason = atr_plan.get("reason", "ATR data incomplete") if atr_plan else "ATR data incomplete"
        return f"Hold - {reason}"
    if not fixed_ready and not atr_ready:
        return "Hold - sizing unavailable (risk/reward not positive at current caps)"
    return "Hold - fixed-buffer sizing unavailable"


def build_decision_summary(
    holdings, portfolio_action_count, watchlist, scores, trade_plans,
    atr_trade_plans, weekend, data_quality_count,
):
    """One-paragraph, decision-first summary for the very top of the email -
    the single most important message, stated plainly before any tables."""
    parts = []

    if not holdings:
        parts.append("No current holdings.")
    else:
        action_word = "action" if portfolio_action_count == 1 else "actions"
        if portfolio_action_count == 0:
            parts.append(f"{len(holdings)} holding(s), no actions needed today.")
        else:
            parts.append(f"{len(holdings)} holding(s), {portfolio_action_count} {action_word} needed - see Portfolio Actions.")

    if not watchlist:
        parts.append("No candidates passed today's screen.")
    else:
        ranked = sorted(watchlist, key=lambda t: scores.get(t, {}).get("total", 0), reverse=True)
        top_ticker = ranked[0]
        top_score = scores.get(top_ticker, {}).get("total", "n/a")
        fixed_plan = trade_plans.get(top_ticker)
        rr = fixed_plan["reward_risk"] if fixed_plan else None
        rr_text = f"fixed-buffer R:R {rr}x" if rr is not None else "fixed-buffer R:R unavailable"

        if len(watchlist) == 1:
            lead = f"One candidate passed: {top_ticker}."
        else:
            lead = f"{len(watchlist)} candidates passed. Strongest: {top_ticker} (score {top_score}/85)."

        exec_status = get_execution_status(top_ticker, trade_plans, atr_trade_plans)
        if exec_status == "Ready":
            action_text = f"{rr_text}. Both sizing plans are ready."
        else:
            hold_reason = exec_status.split("-", 1)[1].strip() if "-" in exec_status else exec_status
            action_text = (
                f"{rr_text}, but execution is on hold ({hold_reason}) - do not place a "
                f"trade from the automated recommendation until that's resolved."
            )

        parts.append(f"{lead} {action_text}")

    if weekend:
        parts.append(f"US market closed (weekend) - figures reflect {last_trading_day_label()}'s close.")

    if data_quality_count:
        alert_word = "alert" if data_quality_count == 1 else "alerts"
        parts.append(f"{data_quality_count} data-quality {alert_word} today - see Data-Quality Alerts.")

    return " ".join(parts)


def load_portfolio():
    with open(PORTFOLIO_FILE, "r") as f:
        portfolio = json.load(f)
    # Migrate any old key names from earlier versions of this tool.
    if "watchlist" in portfolio and "watchlist_us" not in portfolio:
        portfolio["watchlist_us"] = portfolio.pop("watchlist")
    portfolio.setdefault("watchlist_us", [])
    portfolio.setdefault("watchlist_sg", [])
    portfolio.setdefault("holdings", [])
    portfolio.setdefault("closed_positions", [])
    settings = portfolio.setdefault("trade_settings", {})
    for key, default in DEFAULT_TRADE_SETTINGS.items():
        settings.setdefault(key, default)
    return portfolio


def save_portfolio(portfolio):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Holdings: live price + P/L (via yfinance fast_info, same as before)
# ---------------------------------------------------------------------------

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


def format_holdings_table(rows):
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


def get_claude_analysis(holdings_rows, total_cost, total_value):
    """Returns (analysis_text, verdicts) where verdicts is {ticker: "HOLD"|"TRIM"|"SELL"|"ADD"},
    used to build the top-of-email decision summary (e.g. "2 actions needed")."""
    if not holdings_rows:
        return "No current holdings.", {}

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    portfolio_summary = json.dumps(holdings_rows, indent=2)

    prompt = f"""You are producing a daily research note for a personal investor.
Today's date: {datetime.now().strftime('%Y-%m-%d')}

Their current holdings (with cost basis and today's price data already computed):
{portfolio_summary}

Total cost basis: ${total_cost:,.2f}
Total current value: ${total_value:,.2f}

Using web search for current news, analyst ratings, and any material developments
in the last 24-48 hours, write a concise daily note that:

1. For each HOLDING: give a clear read of Hold / Consider Trimming / Consider Adding /
   Consider Selling, with the specific reason (news, earnings, valuation shift,
   technical level, analyst action). Flag anything urgent (bad news, downgrade,
   stock at a key level).
2. Keep it tight - a few sentences per ticker, not an essay. Use plain language,
   no headers-heavy report formatting.
3. End with one short paragraph on overall portfolio risk (concentration, sector
   tilt, anything correlated) if relevant.
4. Do not give definitive commands ("sell now"); give reasoned reads the investor
   can act on. Remind them, briefly, this isn't personalized financial advice.

Be honest and specific. If nothing changed for a name, say so briefly rather than
padding it.

Finally, after your written analysis, add a line containing exactly
VERDICTS_JSON: followed by ONLY a JSON object (no markdown fences) mapping each
ticker to exactly one of "HOLD", "TRIM", "SELL", or "ADD" - your one-word
verdict for that holding, matching the read you gave it above."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )

    text_parts = [block.text for block in response.content if block.type == "text"]
    full_text = "\n".join(text_parts)

    analysis_text = full_text
    verdicts = {}
    if "VERDICTS_JSON:" in full_text:
        analysis_text, _, verdict_raw = full_text.partition("VERDICTS_JSON:")
        analysis_text = analysis_text.strip()
        verdict_raw = verdict_raw.strip()
        if "```" in verdict_raw:
            verdict_raw = verdict_raw.split("```")[1] if verdict_raw.count("```") >= 2 else verdict_raw
            verdict_raw = verdict_raw.replace("json", "", 1).strip()
        start = verdict_raw.find("{")
        end = verdict_raw.rfind("}")
        if start != -1 and end != -1:
            try:
                verdicts = json.loads(verdict_raw[start:end + 1])
            except Exception:
                verdicts = {}

    return analysis_text, verdicts


# ---------------------------------------------------------------------------
# Pre-market gap check (final shortlisted tickers only)
# ---------------------------------------------------------------------------

def fetch_premarket_gap(ticker):
    """Compares the current pre-market price to yesterday's close. Returns
    None if pre-market data isn't available (e.g. run outside the pre-market
    window, or Yahoo hasn't got a quote for this name)."""
    try:
        info = yf.Ticker(ticker).info
        previous_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
        premarket_price = info.get("preMarketPrice")
        if previous_close is None or premarket_price is None:
            return None

        gap_pct = (premarket_price - previous_close) / previous_close * 100

        if abs(gap_pct) < 1:
            status = "OK - plan unchanged"
        elif abs(gap_pct) <= 3:
            status = "Recalculate entry/RR"
        else:
            status = "Review manually - large gap"

        return {
            "ticker": ticker,
            "previous_close": round(previous_close, 2),
            "premarket_price": round(premarket_price, 2),
            "gap_pct": round(gap_pct, 2),
            "status": status,
        }
    except Exception:
        return None


def get_gap_explanations(gaps_needing_explanation):
    """One combined Claude call (with web search) covering every ticker with
    a significant pre-market gap, asking for a short 2-3 sentence reason
    each. Returns {ticker: explanation_string}."""
    if not gaps_needing_explanation:
        return {}

    client = anthropic.Anthropic()

    gap_lines = "\n".join(
        f"- {g['ticker']}: gap {g['gap_pct']}% vs previous close "
        f"(previous close {g['previous_close']}, pre-market {g['premarket_price']}), "
        f"status: {g['status']}"
        for g in gaps_needing_explanation
    )

    prompt = f"""Today's date: {datetime.now().strftime('%Y-%m-%d')}

The following US stocks have a significant pre-market gap vs yesterday's
regular-session close:
{gap_lines}

Using web search, find today's actual pre-market news or catalyst for each
ticker and explain briefly (2-3 sentences maximum per ticker) why the price
gapped. Be specific and factual - reference the real news if you find it
(earnings, guidance, an analyst upgrade/downgrade, an FDA decision, M&A,
broad macro data, etc.). If you genuinely can't find a clear reason, say so
plainly rather than guessing or inventing one.

Respond with ONLY a JSON object mapping ticker to its explanation string, no
other text, no markdown fences:
{{"TICKER1": "explanation...", "TICKER2": "explanation..."}}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )

    text_parts = [block.text for block in response.content if block.type == "text"]
    raw = "\n".join(text_parts).strip()

    if "```" in raw:
        raw = raw.split("```")[1] if raw.count("```") >= 2 else raw
        raw = raw.replace("json", "", 1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]

    try:
        return json.loads(raw)
    except Exception:
        return {}


def build_premarket_gaps(watchlist):
    """Fetches pre-market gap data for the final shortlisted tickers, then
    gets an explanation for any that gapped significantly (status other than
    'OK'). Returns {ticker: gap_dict_or_None}, with an 'explanation' key
    added for tickers that needed one."""
    gaps = {}
    needs_explanation = []
    for ticker in watchlist:
        gap = fetch_premarket_gap(ticker)
        gaps[ticker] = gap
        if gap and gap["status"] != "OK - plan unchanged":
            needs_explanation.append(gap)

    explanations = get_gap_explanations(needs_explanation)
    for ticker, gap in gaps.items():
        if gap:
            gap["explanation"] = explanations.get(ticker)

    return gaps


def format_premarket_table(watchlist, gaps):
    lines = []
    header = f"{'TICKER':8}{'PREV CLOSE':>12}{'PREMARKET':>12}{'GAP %':>9}{'STATUS':>26}"
    lines.append(header)
    lines.append("-" * len(header))
    explanation_lines = []
    for ticker in watchlist:
        gap = gaps.get(ticker)
        if not gap:
            lines.append(f"{ticker:8}  (no pre-market data available)")
            continue
        lines.append(
            f"{ticker:8}{gap['previous_close']:>12.2f}{gap['premarket_price']:>12.2f}"
            f"{gap['gap_pct']:>+8.2f}%{gap['status']:>26}"
        )
        if gap.get("explanation"):
            explanation_lines.append(f"{ticker}: {gap['explanation']}")
    table = "\n".join(lines) if watchlist else "(watchlist is empty)"
    explanations_text = "\n\n".join(explanation_lines) if explanation_lines else "(no significant gaps today)"
    return table, explanations_text


# ---------------------------------------------------------------------------
# Watchlist: rules-based technical screen
#   Price > 20 EMA > 50 EMA, RSI(14) in [50, 60], volume vs 20d avg
# ---------------------------------------------------------------------------

def compute_rsi(close_series, period=14):
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def add_pivots(df: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    """
    Add pivot_high and pivot_low Boolean columns.

    A pivot high must be the highest point within the surrounding window.
    A pivot low must be the lowest point within the surrounding window.
    """
    required = {"High", "Low"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    result = df.copy()
    result["pivot_high"] = False
    result["pivot_low"] = False

    highs = result["High"].to_numpy(dtype=float)
    lows = result["Low"].to_numpy(dtype=float)
    pivot_high_col = result.columns.get_loc("pivot_high")
    pivot_low_col = result.columns.get_loc("pivot_low")

    for i in range(left, len(result) - right):
        high_window = highs[i - left:i + right + 1]
        low_window = lows[i - left:i + right + 1]
        result.iloc[i, pivot_high_col] = highs[i] == np.max(high_window)
        result.iloc[i, pivot_low_col] = lows[i] == np.min(low_window)

    return result


def get_pivot_structure_from_pivots(pivots_df):
    """Given a df already processed by add_pivots(), returns whether the last
    two confirmed pivot highs are rising (higher highs), whether the last two
    confirmed pivot lows are rising (higher lows), and the latest pivot
    high/low price levels for reference."""
    pivot_highs = pivots_df.loc[pivots_df["pivot_high"], "High"]
    pivot_lows = pivots_df.loc[pivots_df["pivot_low"], "Low"]

    higher_highs = len(pivot_highs) >= 2 and bool(pivot_highs.iloc[-1] > pivot_highs.iloc[-2])
    higher_lows = len(pivot_lows) >= 2 and bool(pivot_lows.iloc[-1] > pivot_lows.iloc[-2])

    return {
        "higher_highs": higher_highs,
        "higher_lows": higher_lows,
        "latest_pivot_high": round(float(pivot_highs.iloc[-1]), 2) if len(pivot_highs) else None,
        "latest_pivot_low": round(float(pivot_lows.iloc[-1]), 2) if len(pivot_lows) else None,
    }


def get_pivot_levels(pivots_df, lookback_days=60):
    """All confirmed pivot highs (resistance) and pivot lows (support)
    within the lookback window, as a single price/type table."""
    recent = pivots_df.tail(lookback_days)
    pivot_highs = recent[recent["pivot_high"]][["High"]].rename(columns={"High": "price"})
    pivot_highs["type"] = "resistance"
    pivot_lows = recent[recent["pivot_low"]][["Low"]].rename(columns={"Low": "price"})
    pivot_lows["type"] = "support"
    levels = pd.concat([pivot_highs, pivot_lows]).sort_index()
    return levels


def nearest_levels(pivots_df, current_price, lookback_days=60):
    """Nearest confirmed support below and resistance above the current price."""
    levels = get_pivot_levels(pivots_df, lookback_days)
    supports = levels[(levels["type"] == "support") & (levels["price"] < current_price)]
    resistances = levels[(levels["type"] == "resistance") & (levels["price"] > current_price)]
    nearest_support = float(supports["price"].max()) if not supports.empty else None
    nearest_resistance = float(resistances["price"].min()) if not resistances.empty else None
    return nearest_support, nearest_resistance


def find_nearest_pivot_levels(pivots_df, current_price, lookback_bars=60):
    """Same as nearest_levels, under the name/parameter used by
    build_atr_trade_plan (lookback measured in bars rather than days -
    equivalent here since we're on daily bars)."""
    return nearest_levels(pivots_df, current_price, lookback_days=lookback_bars)


def reward_risk(entry, support, resistance, buffer_pct=0.005):
    """Reward:risk using nearest support (with a small buffer as the stop)
    and nearest resistance (as the target). Returns None if either level
    is missing or if risk works out to zero/negative."""
    if support is None or resistance is None:
        return None
    stop = support * (1 - buffer_pct)
    target = resistance
    risk = entry - stop
    reward = target - entry
    if risk <= 0:
        return None
    rr = reward / risk
    return {
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk": round(risk, 2),
        "reward": round(reward, 2),
        "reward_risk": round(rr, 2),
    }


def calculate_trade_plan(
    ticker,
    entry_price,
    stop_price,
    target_price,
    portfolio_value=10_000,
    max_risk_pct=0.01,
    max_position_pct=0.15,
):
    """Position sizing for a final shortlisted ticker: how many shares to
    buy so that neither the dollar risk (stop hit) nor the position size
    exceeds the configured caps."""
    max_risk_dollars = portfolio_value * max_risk_pct
    max_position_dollars = portfolio_value * max_position_pct
    risk_per_share = entry_price - stop_price
    profit_per_share = target_price - entry_price
    if risk_per_share <= 0 or profit_per_share <= 0:
        return None
    shares_by_risk = max_risk_dollars / risk_per_share
    shares_by_position = max_position_dollars / entry_price
    shares = math.floor(min(shares_by_risk, shares_by_position))
    investment = shares * entry_price
    max_loss = shares * risk_per_share
    max_profit = shares * profit_per_share
    reward_risk_ratio = profit_per_share / risk_per_share
    return {
        "ticker": ticker,
        "entry": entry_price,
        "stop_loss": stop_price,
        "target_sell": target_price,
        "risk_per_share": round(risk_per_share, 2),
        "profit_per_share": round(profit_per_share, 2),
        "shares": shares,
        "investment": round(investment, 2),
        "max_loss": round(max_loss, 2),
        "max_profit": round(max_profit, 2),
        "reward_risk": round(reward_risk_ratio, 2),
        "position_limit": round(max_position_dollars, 2),
        "risk_limit": round(max_risk_dollars, 2),
    }


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Add Wilder-style ATR.

    Required columns:
        High, Low, Close
    """
    required = {"High", "Low", "Close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    result = df.copy()
    previous_close = result["Close"].shift(1)

    true_range = pd.concat(
        [
            result["High"] - result["Low"],
            (result["High"] - previous_close).abs(),
            (result["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    result["TR"] = true_range
    result[f"ATR_{period}"] = true_range.ewm(
        alpha=1 / period, adjust=False, min_periods=period,
    ).mean()

    return result


def calculate_atr_stop(support, atr, zone_atr_fraction=0.25, stop_buffer_atr=0.75):
    """Places the stop a volatility-adjusted distance below support, instead
    of a fixed percentage buffer. `stop_buffer_atr` controls how many ATRs
    below support the stop sits; `zone_atr_fraction` defines a "support zone"
    band around the pivot (for context/display, not used in the stop math
    itself) so a near-miss touch of support still reads as being in the zone."""
    zone_low = support - zone_atr_fraction * atr
    zone_high = support + zone_atr_fraction * atr
    stop_price = support - stop_buffer_atr * atr
    return {
        "stop_price": round(stop_price, 2),
        "atr": round(atr, 2),
        "support_zone_low": round(zone_low, 2),
        "support_zone_high": round(zone_high, 2),
        "stop_buffer_atr": stop_buffer_atr,
        "zone_atr_fraction": zone_atr_fraction,
    }


# ---------------------------------------------------------------------------
# Exit plan engine: multi-target scale-out planning
# ---------------------------------------------------------------------------

def get_resistance_levels(
    df: pd.DataFrame,
    current_price: float,
    recent_lookback: int = 60,
    major_lookback: int = 250,
    minimum_separation_pct: float = 0.01,
):
    """
    Return:
        1. Nearest recent pivot resistance above current price
        2. Next higher major pivot resistance

    minimum_separation_pct prevents near-duplicate resistance levels.
    """
    if "pivot_high" not in df.columns:
        raise ValueError("Run add_pivots() before get_resistance_levels().")

    recent = df.tail(recent_lookback)
    major = df.tail(major_lookback)

    recent_resistances = (
        recent.loc[
            recent["pivot_high"] & (recent["High"] > current_price),
            "High",
        ]
        .dropna()
        .astype(float)
        .sort_values()
    )

    major_resistances = (
        major.loc[
            major["pivot_high"] & (major["High"] > current_price),
            "High",
        ]
        .dropna()
        .astype(float)
        .sort_values()
    )

    nearest_resistance = (
        float(recent_resistances.iloc[0])
        if not recent_resistances.empty
        else None
    )

    major_resistance = None

    if nearest_resistance is not None:
        sufficiently_higher = major_resistances[
            major_resistances
            > nearest_resistance * (1 + minimum_separation_pct)
        ]

        if not sufficiently_higher.empty:
            major_resistance = float(sufficiently_higher.iloc[0])

    elif not major_resistances.empty:
        major_resistance = float(major_resistances.iloc[0])

    return nearest_resistance, major_resistance


@dataclass
class TargetCandidate:
    name: str
    price: float
    reward_per_share: float
    reward_risk: float
    ranking_score: float
    notes: str


def score_target(
    target_name: str,
    target_price: float,
    entry_price: float,
    stop_price: float,
    atr: float,
    nearest_resistance,
    major_resistance,
    rsi=None,
    volume_ratio=None,
    earnings_within_5_days: bool = False,
) -> TargetCandidate:
    """
    Score a target using:
        - Reward-to-risk
        - Alignment with market structure
        - RSI
        - Volume ratio
        - Earnings risk

    The score is a ranking score, not a probability.
    """
    risk_per_share = entry_price - stop_price
    reward_per_share = target_price - entry_price

    if risk_per_share <= 0:
        raise ValueError("Stop price must be below entry price.")

    if reward_per_share <= 0:
        raise ValueError("Target price must be above entry price.")

    reward_risk = reward_per_share / risk_per_share

    score = 0.0
    notes = []

    # Reward-to-risk: max 30
    if reward_risk >= 4:
        score += 30
        notes.append("Excellent reward-to-risk")
    elif reward_risk >= 3:
        score += 25
        notes.append("Good reward-to-risk")
    elif reward_risk >= 2:
        score += 15
        notes.append("Moderate reward-to-risk")
    else:
        score += 5
        notes.append("Weak reward-to-risk")

    # Structure alignment: max 30
    if (
        nearest_resistance is not None
        and abs(target_price - nearest_resistance) <= atr * 0.5
    ):
        score += 30
        notes.append("Aligned with nearest resistance")

    elif (
        major_resistance is not None
        and abs(target_price - major_resistance) <= atr * 0.75
    ):
        score += 22
        notes.append("Aligned with major resistance")

    elif target_name == "ATR projection":
        score += 15
        notes.append("Based on volatility projection")

    # RSI: max 15
    if rsi is not None:
        if 50 <= rsi <= 65:
            score += 15
            notes.append("Healthy RSI")
        elif 45 <= rsi < 50 or 65 < rsi <= 70:
            score += 8
            notes.append("Acceptable RSI")
        else:
            notes.append("Weak or overextended RSI")

    # Volume: max 15
    if volume_ratio is not None:
        if volume_ratio >= 1.5:
            score += 15
            notes.append("Strong relative volume")
        elif volume_ratio >= 1.0:
            score += 8
            notes.append("Average relative volume")
        else:
            notes.append("Below-average relative volume")

    # Earnings: max 10, with penalty
    if earnings_within_5_days:
        score -= 15
        notes.append("Near-term earnings risk")
    else:
        score += 10
        notes.append("No near-term earnings risk")

    score = max(0.0, min(score, 100.0))

    return TargetCandidate(
        name=target_name,
        price=round(target_price, 4),
        reward_per_share=round(reward_per_share, 4),
        reward_risk=round(reward_risk, 2),
        ranking_score=round(score, 1),
        notes="; ".join(notes),
    )


def build_target_candidates(
    entry_price: float,
    stop_price: float,
    atr: float,
    nearest_resistance,
    major_resistance,
    atr_target_multiple: float = 3.0,
    rsi=None,
    volume_ratio=None,
    earnings_within_5_days: bool = False,
    duplicate_tolerance_atr: float = 0.25,
):
    """
    Build and score:
        - Nearest resistance target
        - ATR projection target
        - Major resistance target

    Near-duplicate targets are merged.
    """
    raw_targets = []

    if nearest_resistance is not None:
        raw_targets.append(("Nearest resistance", nearest_resistance))

    atr_target = entry_price + atr_target_multiple * atr
    raw_targets.append(("ATR projection", atr_target))

    if major_resistance is not None:
        raw_targets.append(("Major resistance", major_resistance))

    candidates = []

    for target_name, target_price in raw_targets:
        if target_price <= entry_price:
            continue

        candidate = score_target(
            target_name=target_name,
            target_price=target_price,
            entry_price=entry_price,
            stop_price=stop_price,
            atr=atr,
            nearest_resistance=nearest_resistance,
            major_resistance=major_resistance,
            rsi=rsi,
            volume_ratio=volume_ratio,
            earnings_within_5_days=earnings_within_5_days,
        )

        candidates.append(candidate)

    # Sort by target price before deduplication
    candidates.sort(key=lambda candidate: candidate.price)

    unique_candidates = []
    tolerance = atr * duplicate_tolerance_atr

    for candidate in candidates:
        matching_index = next(
            (
                index
                for index, existing in enumerate(unique_candidates)
                if abs(candidate.price - existing.price) <= tolerance
            ),
            None,
        )

        if matching_index is None:
            unique_candidates.append(candidate)
        else:
            existing = unique_candidates[matching_index]
            if candidate.ranking_score > existing.ranking_score:
                unique_candidates[matching_index] = candidate

    return sorted(
        unique_candidates,
        key=lambda candidate: (candidate.ranking_score, candidate.reward_risk),
        reverse=True,
    )


def recommend_primary_target(candidates, minimum_reward_risk=2.5):
    """
    Recommend the highest-ranked target that meets the minimum R:R.
    """
    qualifying = [
        candidate
        for candidate in candidates
        if candidate.reward_risk >= minimum_reward_risk
    ]

    if not qualifying:
        return None

    return max(
        qualifying,
        key=lambda candidate: (candidate.ranking_score, candidate.reward_risk),
    )


def allocate_scale_out_shares(total_shares: int, candidates):
    """
    Allocate shares across up to two fixed targets plus a runner.

    For 2+ targets:
        30% at target 1
        40% at target 2
        remainder as runner

    For 1 target:
        70% at target 1
        remainder as runner
    """
    if total_shares <= 0:
        raise ValueError("total_shares must be positive.")

    targets_by_price = sorted(candidates, key=lambda candidate: candidate.price)

    if not targets_by_price:
        return {
            "target_1_price": None,
            "target_1_shares": 0,
            "target_2_price": None,
            "target_2_shares": 0,
            "runner_shares": total_shares,
        }

    if len(targets_by_price) == 1:
        target_1_shares = math.floor(total_shares * 0.70)
        runner_shares = total_shares - target_1_shares

        return {
            "target_1_price": targets_by_price[0].price,
            "target_1_shares": target_1_shares,
            "target_2_price": None,
            "target_2_shares": 0,
            "runner_shares": runner_shares,
        }

    target_1_shares = math.floor(total_shares * 0.30)
    target_2_shares = math.floor(total_shares * 0.40)
    runner_shares = total_shares - target_1_shares - target_2_shares

    return {
        "target_1_price": targets_by_price[0].price,
        "target_1_shares": target_1_shares,
        "target_2_price": targets_by_price[1].price,
        "target_2_shares": target_2_shares,
        "runner_shares": runner_shares,
    }


def build_exit_plan(
    df: pd.DataFrame,
    entry_price: float,
    stop_price: float,
    total_shares: int,
    atr_period: int = 14,
    pivot_left: int = 2,
    pivot_right: int = 2,
    recent_lookback: int = 60,
    major_lookback: int = 250,
    atr_target_multiple: float = 3.0,
    rsi=None,
    volume_ratio=None,
    earnings_within_5_days: bool = False,
    minimum_reward_risk: float = 2.5,
) -> dict:
    """
    Complete exit planning workflow.
    """
    if entry_price <= 0:
        raise ValueError("entry_price must be positive.")

    if stop_price >= entry_price:
        raise ValueError("stop_price must be below entry_price.")

    if total_shares <= 0:
        raise ValueError("total_shares must be positive.")

    working = add_atr(df, period=atr_period)
    working = add_pivots(working, left=pivot_left, right=pivot_right)

    atr_value = working[f"ATR_{atr_period}"].iloc[-1]
    if pd.isna(atr_value):
        raise ValueError("ATR is unavailable. Provide more historical price rows.")
    atr = float(atr_value)

    nearest_resistance, major_resistance = get_resistance_levels(
        working,
        current_price=entry_price,
        recent_lookback=recent_lookback,
        major_lookback=major_lookback,
    )

    candidates = build_target_candidates(
        entry_price=entry_price,
        stop_price=stop_price,
        atr=atr,
        nearest_resistance=nearest_resistance,
        major_resistance=major_resistance,
        atr_target_multiple=atr_target_multiple,
        rsi=rsi,
        volume_ratio=volume_ratio,
        earnings_within_5_days=earnings_within_5_days,
    )

    primary_target = recommend_primary_target(
        candidates, minimum_reward_risk=minimum_reward_risk,
    )

    scale_out_plan = allocate_scale_out_shares(
        total_shares=total_shares, candidates=candidates,
    )

    return {
        "entry_price": round(entry_price, 4),
        "stop_price": round(stop_price, 4),
        "risk_per_share": round(entry_price - stop_price, 4),
        "atr": round(atr, 4),
        "nearest_resistance": (
            round(nearest_resistance, 4) if nearest_resistance is not None else None
        ),
        "major_resistance": (
            round(major_resistance, 4) if major_resistance is not None else None
        ),
        "primary_target": (
            asdict(primary_target) if primary_target is not None else None
        ),
        "target_candidates": [asdict(candidate) for candidate in candidates],
        "scale_out_plan": scale_out_plan,
        "status": (
            "TARGET_FOUND" if primary_target is not None else "NO_TARGET_MEETS_MINIMUM_RR"
        ),
    }


def build_atr_trade_plan(
    df: pd.DataFrame,
    ticker: str,
    portfolio_value: float = 10_000,
    atr_period: int = 14,
    pivot_left: int = 2,
    pivot_right: int = 2,
    pivot_lookback: int = 60,
    zone_atr_fraction: float = 0.25,
    stop_buffer_atr: float = 0.75,
    max_risk_pct: float = 0.01,
    max_position_pct: float = 0.15,
) -> dict[str, Any]:
    """
    Complete long-trade planning engine.
    Entry:
        Latest closing price
    Stop:
        Below nearest pivot support using ATR
    Target:
        Nearest pivot resistance
    """
    working = add_atr(df, period=atr_period)
    working = add_pivots(
        working,
        left=pivot_left,
        right=pivot_right,
    )
    latest = working.iloc[-1]
    entry_price = float(latest["Close"])
    atr = float(latest[f"ATR_{atr_period}"])
    if np.isnan(atr):
        raise ValueError("Latest ATR is unavailable.")
    support, resistance = find_nearest_pivot_levels(
        working,
        current_price=entry_price,
        lookback_bars=pivot_lookback,
    )
    if support is None:
        return {
            "ticker": ticker,
            "status": "PASS",
            "reason": "No recent pivot support below the current price.",
        }
    if resistance is None:
        return {
            "ticker": ticker,
            "status": "REVIEW",
            "reason": "No recent pivot resistance above the current price.",
        }
    stop_details = calculate_atr_stop(
        support=support,
        atr=atr,
        zone_atr_fraction=zone_atr_fraction,
        stop_buffer_atr=stop_buffer_atr,
    )
    stop_price = stop_details["stop_price"]
    plan = calculate_trade_plan(
        ticker=ticker,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=resistance,
        portfolio_value=portfolio_value,
        max_risk_pct=max_risk_pct,
        max_position_pct=max_position_pct,
    )
    if plan is None:
        return {
            "ticker": ticker,
            "status": "PASS",
            "reason": "Risk or reward per share was not positive (stop/target inverted around entry).",
        }
    plan.update(stop_details)
    if plan["shares"] < 1:
        plan["status"] = "PASS"
        plan["reason"] = "Position size is below one whole share."
    elif plan["reward_risk"] < 2.5:
        plan["status"] = "PASS"
        plan["reason"] = "Reward-to-risk is below 2.5."
    elif plan["reward_risk"] < 3:
        plan["status"] = "WATCH"
        plan["reason"] = "Reward-to-risk is acceptable but below 3."
    else:
        plan["status"] = "CANDIDATE"
        plan["reason"] = "ATR stop and reward-to-risk criteria are satisfied."
    return plan


def compute_technical_indicators(tickers):
    """Batch-fetch daily history for all tickers and compute indicators.
    Returns {ticker: indicators_dict_or_None}.
    """
    results = {}
    if not tickers:
        return results

    try:
        data = yf.download(
            tickers=tickers, period="6mo", interval="1d",
            group_by="ticker", threads=True, progress=False, auto_adjust=False,
        )
    except Exception:
        data = None

    for ticker in tickers:
        try:
            if data is None:
                raise ValueError("batch download failed")
            # Don't assume shape from len(tickers) - check the actual columns
            # returned. A MultiIndex means this ticker's data is one slice of
            # a multi-ticker frame; flat columns mean it's already isolated.
            if isinstance(data.columns, pd.MultiIndex):
                if ticker in data.columns.get_level_values(0):
                    df = data[ticker]
                elif ticker in data.columns.get_level_values(-1):
                    df = data.xs(ticker, axis=1, level=-1)
                else:
                    raise ValueError(f"{ticker} not found in downloaded data")
            else:
                df = data
            df = df.dropna(subset=["Close", "High", "Low", "Volume"])
            if df is None or len(df) < 55:
                results[ticker] = None
                continue

            close = df["Close"]
            volume = df["Volume"]
            ema20 = close.ewm(span=20, adjust=False).mean()
            ema50 = close.ewm(span=50, adjust=False).mean()
            rsi = compute_rsi(close, 14)

            # ATR14 (Wilder) - reuses the High/Low/Close already fetched
            # above, no extra network call. Needed for the full-universe
            # snapshot table (ATR14 $ and ATR% columns).
            previous_close = close.shift(1)
            true_range = pd.concat([
                df["High"] - df["Low"],
                (df["High"] - previous_close).abs(),
                (df["Low"] - previous_close).abs(),
            ], axis=1).max(axis=1)
            atr14_series = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
            latest_atr14 = float(atr14_series.iloc[-1]) if not pd.isna(atr14_series.iloc[-1]) else None

            avg_vol_20d = float(volume.tail(20).mean())
            latest_vol = float(volume.iloc[-1])
            latest_price = float(close.iloc[-1])

            # Compute pivots once, reuse for trend structure + support/resistance.
            pivots_df = add_pivots(df, left=2, right=2)
            structure = get_pivot_structure_from_pivots(pivots_df)
            nearest_support, nearest_resistance = nearest_levels(
                pivots_df, latest_price, lookback_days=60
            )
            rr = reward_risk(latest_price, nearest_support, nearest_resistance, buffer_pct=0.005)

            results[ticker] = {
                "ticker": ticker,
                "price": round(latest_price, 2),
                "ema20": round(float(ema20.iloc[-1]), 2),
                "ema50": round(float(ema50.iloc[-1]), 2),
                "rsi": round(float(rsi.iloc[-1]), 1),
                "atr14": round(latest_atr14, 2) if latest_atr14 is not None else None,
                "atr_pct": round(latest_atr14 / latest_price * 100, 2) if (latest_atr14 is not None and latest_price) else None,
                "avg_vol_20d": int(avg_vol_20d),
                "latest_vol": int(latest_vol),
                "vol_ratio": round(latest_vol / avg_vol_20d, 2) if avg_vol_20d else None,
                "higher_highs": structure["higher_highs"],
                "higher_lows": structure["higher_lows"],
                "latest_pivot_high": structure["latest_pivot_high"],
                "latest_pivot_low": structure["latest_pivot_low"],
                "nearest_support": round(nearest_support, 2) if nearest_support is not None else None,
                "nearest_resistance": round(nearest_resistance, 2) if nearest_resistance is not None else None,
                "reward_risk": rr,
            }
        except Exception:
            results[ticker] = None

    return results


def get_earnings_trading_days_away(ticker):
    """Business days from today until the next scheduled earnings date.
    Returns None if no upcoming earnings date is found (treated favorably -
    no near-term earnings risk visible)."""
    try:
        edf = yf.Ticker(ticker).get_earnings_dates(limit=8)
        if edf is None or edf.empty:
            return None
        today = pd.Timestamp.now(tz=edf.index.tz) if edf.index.tz is not None else pd.Timestamp.now()
        future = edf[edf.index >= today]
        if future.empty:
            return None
        next_date = future.index.min()
        days = int(np.busday_count(pd.Timestamp.now().date(), next_date.date()))
        return max(days, 0)
    except Exception:
        return None


def score_trend(ind):
    """Category 1 - Trend, out of 25."""
    score = 0
    detail = []
    if ind["price"] > ind["ema20"]:
        score += 10
        detail.append("price>20EMA +10")
    else:
        detail.append("price>20EMA +0")
    if ind["ema20"] > ind["ema50"]:
        score += 10
        detail.append("20EMA>50EMA +10")
    else:
        detail.append("20EMA>50EMA +0")
    if ind["higher_highs"]:
        score += 5
        detail.append(f"higher highs (pivot high {ind['latest_pivot_high']}) +5")
    else:
        detail.append(f"higher highs (pivot high {ind['latest_pivot_high']}) +0")
    return score, "; ".join(detail)


def score_momentum(ind):
    """Category 2 - Momentum (RSI + Volume), out of 20."""
    rsi = ind["rsi"]
    if 50 <= rsi <= 60:
        rsi_score = 10
    elif 60 < rsi <= 65:
        rsi_score = 8
    elif 65 < rsi <= 70:
        rsi_score = 5
    else:
        rsi_score = 0  # >70 (overbought) or <50 (weak momentum)

    vol_ratio = ind["vol_ratio"] or 0
    if vol_ratio > 1.1:
        vol_score = 10
        vol_label = "above average"
    elif vol_ratio >= 0.9:
        vol_score = 5
        vol_label = "in line with average"
    else:
        vol_score = 0
        vol_label = "below average"

    detail = f"RSI {rsi} +{rsi_score}; volume {vol_label} vs 20d ({vol_ratio}x) +{vol_score}"
    return rsi_score + vol_score, detail


def score_earnings(days_away):
    """Category 3 - Earnings impact, out of 10."""
    if days_away is None:
        return 10, "no earnings date found nearby +10"
    if days_away <= 5:
        return 0, f"earnings in {days_away} trading days (within 5) +0"
    elif days_away <= 10:
        return 5, f"earnings in {days_away} trading days (6-10 out) +5"
    else:
        return 10, f"earnings in {days_away} trading days (>10 out) +10"


def score_location(current_price, support, resistance, rr_data):
    """Category 4 - Location relative to support/resistance, out of 30.
    Rewards being close to support (a better entry), having enough room
    below resistance (enough upside), and a strong reward:risk ratio."""
    if support is None or resistance is None or rr_data is None:
        return 0, "no confirmed support/resistance nearby - not scored"

    distance_to_support = (current_price - support) / current_price
    distance_to_resistance = (resistance - current_price) / current_price
    rr = rr_data["reward_risk"]

    score = 0
    detail = []

    if distance_to_support <= 0.03:
        score += 15
        detail.append(f"{distance_to_support*100:.1f}% above support +15")
    elif distance_to_support <= 0.06:
        score += 10
        detail.append(f"{distance_to_support*100:.1f}% above support +10")
    elif distance_to_support <= 0.10:
        score += 5
        detail.append(f"{distance_to_support*100:.1f}% above support +5")
    else:
        detail.append(f"{distance_to_support*100:.1f}% above support +0")

    if distance_to_resistance >= 0.08:
        score += 5
        detail.append(f"{distance_to_resistance*100:.1f}% below resistance +5")
    elif distance_to_resistance >= 0.05:
        score += 3
        detail.append(f"{distance_to_resistance*100:.1f}% below resistance +3")
    else:
        detail.append(f"{distance_to_resistance*100:.1f}% below resistance +0")

    if rr >= 4:
        score += 10
        detail.append(f"R:R {rr} +10")
    elif rr >= 3:
        score += 8
        detail.append(f"R:R {rr} +8")
    elif rr >= 2.5:
        score += 5
        detail.append(f"R:R {rr} +5")
    else:
        detail.append(f"R:R {rr} +0")

    return min(score, 30), "; ".join(detail)


def score_ticker(ind, earnings_days):
    trend_score, trend_detail = score_trend(ind)
    momentum_score, momentum_detail = score_momentum(ind)
    earnings_score, earnings_detail = score_earnings(earnings_days)
    location_score, location_detail = score_location(
        ind["price"], ind["nearest_support"], ind["nearest_resistance"], ind["reward_risk"]
    )
    total = trend_score + momentum_score + earnings_score + location_score
    return {
        "total": total,
        "trend": trend_score,
        "momentum": momentum_score,
        "earnings": earnings_score,
        "location": location_score,
        "trend_detail": trend_detail,
        "momentum_detail": momentum_detail,
        "earnings_detail": earnings_detail,
        "location_detail": location_detail,
        "earnings_days_away": earnings_days,
    }


BASE_SCORE_MINIMUM = 45  # out of 55 (Trend + Momentum + Earnings only)
REWARD_RISK_MINIMUM = 2.5
STAGE1_SHORTLIST_SIZE = 20


def screen_watchlist(current_watchlist, holdings_tickers, universe_list=None, max_size=None, stage1_size=None):
    """Two-stage gate, then rank:
      Stage 1: reject anything scoring below BASE_SCORE_MINIMUM out of 55 on
               Trend + Momentum + Earnings alone (the original three
               categories, before Location/reward:risk is even considered).
               The top `stage1_size` survivors (by base score) are kept as
               the Stage 1 shortlist shown in the email.
      Stage 2: of that Stage 1 shortlist, reject anything with reward:risk
               below REWARD_RISK_MINIMUM.
      Then rank Stage 2 survivors by total score (out of 85, Location
      included) and take the top `max_size` as the final watchlist.

    `universe_list`, `max_size`, and `stage1_size` default to the US settings
    (US_CANDIDATE_UNIVERSE, WATCHLIST_MAX_US, STAGE1_SHORTLIST_SIZE) so
    existing callers don't need to change; pass different values to run this
    same screen against a different market/universe (e.g. SGX).

    Returns (new_watchlist, changes_log, indicators_by_ticker, scores_by_ticker,
    stage1_shortlist, stage2_eliminated) where:
      stage1_shortlist = list of dicts for the top `stage1_size` that cleared Stage 1
      stage2_eliminated = list of dicts for Stage 1 survivors cut at Stage 2
    """
    universe_list = universe_list if universe_list is not None else US_CANDIDATE_UNIVERSE
    max_size = max_size if max_size is not None else WATCHLIST_MAX_US
    stage1_size = stage1_size if stage1_size is not None else STAGE1_SHORTLIST_SIZE

    universe = sorted(set(universe_list) | set(current_watchlist))
    universe = [t for t in universe if t not in holdings_tickers]

    indicators = compute_technical_indicators(universe)

    stage1_candidates = []  # everything that cleared the base score minimum

    for ticker in universe:
        ind = indicators.get(ticker)
        if not ind:
            continue

        trend_score, _ = score_trend(ind)
        momentum_score, _ = score_momentum(ind)
        earnings_days = get_earnings_trading_days_away(ticker)
        earnings_score, _ = score_earnings(earnings_days)
        base_score = trend_score + momentum_score + earnings_score

        if base_score < BASE_SCORE_MINIMUM:
            continue

        stage1_candidates.append({
            "ticker": ticker,
            "base_score": base_score,
            "trend": trend_score,
            "momentum": momentum_score,
            "earnings": earnings_score,
            "earnings_days": earnings_days,
        })

    # Keep only the top N by base score as the Stage 1 shortlist that
    # actually proceeds to the reward:risk check - matches "show me the
    # first 20 it shortlisted" rather than every single Stage 1 passer.
    stage1_candidates.sort(key=lambda c: c["base_score"], reverse=True)
    stage1_shortlist = stage1_candidates[:stage1_size]

    scores = {}
    stage2_eliminated = []

    for candidate in stage1_shortlist:
        ticker = candidate["ticker"]
        ind = indicators[ticker]
        rr_data = ind.get("reward_risk")
        rr_value = rr_data["reward_risk"] if rr_data else None

        if rr_value is None or rr_value < REWARD_RISK_MINIMUM:
            stage2_eliminated.append({
                "ticker": ticker,
                "base_score": candidate["base_score"],
                "reward_risk": rr_value,
            })
            continue

        scores[ticker] = score_ticker(ind, candidate["earnings_days"])

    ranked = sorted(scores.items(), key=lambda kv: kv[1]["total"], reverse=True)
    new_watchlist = [ticker for ticker, _ in ranked[:max_size]]

    # Simple day-over-day diff, same as before, for the "changes today" log.
    rejected_base_lookup = {
        c["ticker"]: c["base_score"] for c in stage1_candidates if c["base_score"] < BASE_SCORE_MINIMUM
    }
    rejected_rr_lookup = {e["ticker"]: e["reward_risk"] for e in stage2_eliminated}

    changes = []
    old_set = set(current_watchlist)
    new_set = set(new_watchlist)

    for ticker in old_set - new_set:
        if ticker in rejected_rr_lookup:
            rr_str = rejected_rr_lookup[ticker] if rejected_rr_lookup[ticker] is not None else "n/a (no confirmed levels)"
            changes.append(f"- Dropped {ticker}: reward:risk {rr_str} below the {REWARD_RISK_MINIMUM} floor")
        else:
            s = scores.get(ticker)
            if s:
                changes.append(f"- Dropped {ticker}: score {s['total']}/85, no longer in the top {max_size}")
            else:
                changes.append(f"- Dropped {ticker}: below the {BASE_SCORE_MINIMUM}/55 base score minimum or outside the top {stage1_size}")

    for ticker in new_set - old_set:
        s = scores[ticker]
        changes.append(
            f"- Added {ticker}: score {s['total']}/85 "
            f"(Trend {s['trend']}/25, Momentum {s['momentum']}/20, "
            f"Earnings {s['earnings']}/10, Location {s['location']}/30)"
        )

    return new_watchlist, changes, indicators, scores, stage1_shortlist, stage2_eliminated


def format_stage1_table(stage1_shortlist):
    """The top N candidates that cleared the base score minimum (Stage 1),
    ranked by base score, before the reward:risk check is even applied."""
    lines = []
    header = f"{'TICKER':8}{'BASE':>8}{'TREND':>8}{'MOM':>6}{'EARN':>6}"
    lines.append(header)
    lines.append("-" * len(header))
    for c in stage1_shortlist:
        lines.append(
            f"{c['ticker']:8}{c['base_score']:>6}/55{c['trend']:>7}/25"
            f"{c['momentum']:>5}/20{c['earnings']:>5}/10"
        )
    return "\n".join(lines) if stage1_shortlist else "(no candidates cleared the base score minimum today)"


def format_stage2_eliminated_table(stage2_eliminated):
    """Stage 1 survivors that got cut at Stage 2 for failing the reward:risk
    minimum - the piece that was previously invisible."""
    if not stage2_eliminated:
        return "(none - every Stage 1 name also cleared the reward:risk minimum today)"
    lines = []
    header = f"{'TICKER':8}{'BASE':>8}{'REWARD:RISK':>14}{'REASON':>36}"
    lines.append(header)
    lines.append("-" * len(header))
    for e in stage2_eliminated:
        rr = e["reward_risk"]
        if rr is None:
            rr_str = "n/a"
            reason = "no confirmed support/resistance nearby"
        else:
            rr_str = f"{rr:.2f}x"
            reason = f"below the {REWARD_RISK_MINIMUM} floor"
        lines.append(f"{e['ticker']:8}{e['base_score']:>6}/55{rr_str:>14}{reason:>36}")
    return "\n".join(lines)


def classify_trend(price, ema20, ema50):
    if price > ema20 > ema50:
        return "Bullish"
    if price < ema20 < ema50:
        return "Bearish"
    return "Mixed"


def build_ticker_flags(ind, in_stage1_shortlist):
    """
    Legend:
      green_circle  Price > EMA20 > EMA50 (bullish alignment)
      yellow_circle RSI 50-70 (healthy momentum zone)
      red_circle    RSI > 75 (overbought)
      blue_circle   ATR% > 4% (elevated volatility)
      star          Cleared today's Stage 1 screen (Trend+Momentum+Earnings >= 45/55)
    """
    flags = []
    if ind["price"] > ind["ema20"] > ind["ema50"]:
        flags.append("\U0001F7E2")  # green circle
    if 50 <= ind["rsi"] <= 70:
        flags.append("\U0001F7E1")  # yellow circle
    if ind["rsi"] > 75:
        flags.append("\U0001F534")  # red circle
    if ind["atr_pct"] is not None and ind["atr_pct"] > 4:
        flags.append("\U0001F535")  # blue circle
    if in_stage1_shortlist:
        flags.append("\u2B50")  # star
    return "".join(flags) if flags else "-"


def format_full_universe_table(indicators, stage1_tickers):
    """Every ticker in today's screening universe, technicals only - no
    scoring gate applied, so nothing is hidden. Sorted alphabetically."""
    lines = []
    header = (
        f"{'TICKER':8}{'PRICE':>9}{'EMA20':>9}{'EMA50':>9}{'RSI14':>8}"
        f"{'ATR14($)':>10}{'ATR%':>7}  {'TREND':8}  FLAGS"
    )
    lines.append(header)
    lines.append("-" * len(header))
    no_data = []
    for ticker in sorted(indicators.keys()):
        ind = indicators.get(ticker)
        if not ind:
            no_data.append(ticker)
            continue
        trend = classify_trend(ind["price"], ind["ema20"], ind["ema50"])
        flags = build_ticker_flags(ind, ticker in stage1_tickers)
        atr14_str = f"{ind['atr14']:.2f}" if ind["atr14"] is not None else "n/a"
        atr_pct_str = f"{ind['atr_pct']:.1f}%" if ind["atr_pct"] is not None else "n/a"
        lines.append(
            f"{ticker:8}{ind['price']:>9.2f}{ind['ema20']:>9.2f}{ind['ema50']:>9.2f}"
            f"{ind['rsi']:>8.1f}{atr14_str:>10}{atr_pct_str:>7}  {trend:8}  {flags}"
        )
    table = "\n".join(lines)
    legend = (
        "Legend: \U0001F7E2 Price>EMA20>EMA50 (bullish align)  "
        "\U0001F7E1 RSI 50-70  \U0001F534 RSI>75 (overbought)  "
        "\U0001F535 ATR%>4 (elevated volatility)  "
        "\u2B50 cleared today's Stage 1 screen (base score >= 45/55)"
    )
    no_data_note = f"\nNo data today: {', '.join(no_data)}" if no_data else ""
    return f"{table}\n\n{legend}{no_data_note}"


def format_watchlist_table(watchlist, indicators, scores):
    lines = []
    header = (
        f"{'TICKER':8}{'SCORE':>9}{'TREND':>8}{'MOM':>6}{'EARN':>6}{'LOC':>7}"
        f"{'PRICE':>10}{'20EMA':>9}{'50EMA':>9}{'RSI':>7}{'VOLx(20d)':>11}"
        f"{'LAST PIV HI':>12}{'LAST PIV LO':>12}{'EARN(days)':>12}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for ticker in sorted(watchlist, key=lambda t: scores.get(t, {}).get("total", 0), reverse=True):
        ind = indicators.get(ticker)
        s = scores.get(ticker)
        if not ind or not s:
            lines.append(f"{ticker:8}  (no data)")
            continue
        days = s["earnings_days_away"]
        days_str = str(days) if days is not None else "n/a"
        pivot_hi = ind["latest_pivot_high"]
        pivot_lo = ind["latest_pivot_low"]
        pivot_hi_str = f"{pivot_hi:.2f}" if pivot_hi is not None else "n/a"
        pivot_lo_str = f"{pivot_lo:.2f}" if pivot_lo is not None else "n/a"
        hh_flag = "HH" if ind["higher_highs"] else "--"
        hl_flag = "HL" if ind["higher_lows"] else "--"
        lines.append(
            f"{ticker:8}{s['total']:>6}/85{s['trend']:>7}/25{s['momentum']:>5}/20"
            f"{s['earnings']:>5}/10{s['location']:>5}/30"
            f"{ind['price']:>10.2f}{ind['ema20']:>9.2f}{ind['ema50']:>9.2f}"
            f"{ind['rsi']:>7.1f}{(ind['vol_ratio'] or 0):>11.2f}"
            f"{pivot_hi_str:>10}{hh_flag:>2}{pivot_lo_str:>10}{hl_flag:>2}{days_str:>12}"
        )
    return "\n".join(lines) if watchlist else "(watchlist is empty)"


def format_levels_table(watchlist, indicators):
    """Support/resistance + reward:risk table, ranked by best reward:risk first."""
    def rr_sort_key(ticker):
        ind = indicators.get(ticker)
        rr = ind["reward_risk"]["reward_risk"] if ind and ind.get("reward_risk") else -999
        return rr

    lines = []
    header = (
        f"{'TICKER':8}{'PRICE':>10}{'NEAREST SUPP':>13}{'NEAREST RESIST':>15}"
        f"{'STOP-LOSS':>11}{'TAKE-PROFIT':>13}{'RISK':>8}{'REWARD':>9}{'R:R':>7}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for ticker in sorted(watchlist, key=rr_sort_key, reverse=True):
        ind = indicators.get(ticker)
        if not ind:
            lines.append(f"{ticker:8}  (no data)")
            continue
        support = ind["nearest_support"]
        resistance = ind["nearest_resistance"]
        rr = ind["reward_risk"]
        support_str = f"{support:.2f}" if support is not None else "n/a"
        resistance_str = f"{resistance:.2f}" if resistance is not None else "n/a"
        if rr:
            lines.append(
                f"{ticker:8}{ind['price']:>10.2f}{support_str:>13}{resistance_str:>15}"
                f"{rr['stop']:>11.2f}{rr['target']:>13.2f}{rr['risk']:>8.2f}"
                f"{rr['reward']:>9.2f}{rr['reward_risk']:>6.2f}x"
            )
        else:
            lines.append(
                f"{ticker:8}{ind['price']:>10.2f}{support_str:>13}{resistance_str:>15}"
                f"{'n/a':>11}{'n/a':>13}{'n/a':>8}{'n/a':>9}{'n/a':>7}"
            )
    return "\n".join(lines) if watchlist else "(watchlist is empty)"


def build_trade_plans(watchlist, indicators, trade_settings):
    """Computes a position-sizing trade plan for each shortlisted ticker,
    using its already-computed stop (support minus buffer) and target
    (resistance) from the reward:risk calc. Returns {ticker: plan_or_None}."""
    plans = {}
    for ticker in watchlist:
        ind = indicators.get(ticker)
        if not ind or not ind.get("reward_risk"):
            plans[ticker] = None
            continue
        rr = ind["reward_risk"]
        plans[ticker] = calculate_trade_plan(
            ticker=ticker,
            entry_price=ind["price"],
            stop_price=rr["stop"],
            target_price=rr["target"],
            portfolio_value=trade_settings["portfolio_value"],
            max_risk_pct=trade_settings["max_risk_pct"],
            max_position_pct=trade_settings["max_position_pct"],
        )
    return plans


def format_trade_plan_table(watchlist, plans):
    lines = []
    header = (
        f"{'TICKER':8}{'ENTRY':>9}{'STOP-LOSS':>11}{'TAKE-PROFIT':>13}"
        f"{'SHARES':>8}{'POSITION VALUE':>16}{'MAX LOSS':>10}{'MAX PROFIT':>12}{'R:R':>6}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for ticker in watchlist:
        plan = plans.get(ticker)
        if not plan:
            lines.append(f"{ticker:8}  (no valid trade plan - risk or reward not positive)")
            continue
        if plan["shares"] <= 0:
            lines.append(f"{ticker:8}  (position size rounds to 0 shares at current caps)")
            continue
        lines.append(
            f"{ticker:8}{plan['entry']:>9.2f}{plan['stop_loss']:>11.2f}{plan['target_sell']:>13.2f}"
            f"{plan['shares']:>8}{plan['investment']:>16.2f}{plan['max_loss']:>10.2f}"
            f"{plan['max_profit']:>12.2f}{plan['reward_risk']:>5.2f}x"
        )
    return "\n".join(lines) if watchlist else "(watchlist is empty)"


def humanize_exception(context, e):
    """Turns a raw exception into a short, clean, user-facing reason. The
    raw exception is never shown in the email - it's printed to the
    console/GitHub Actions log separately by the caller, for debugging."""
    msg = str(e)
    if "Missing required columns" in msg:
        clean = "Price-history columns missing from the data feed."
    elif "ATR is unavailable" in msg or "insufficient" in msg.lower():
        clean = "Not enough price history available to calculate ATR."
    elif "Stop price must be below" in msg or "Target price must be above" in msg:
        clean = "Stop/target levels were inconsistent with the entry price."
    elif "total_shares must be positive" in msg:
        clean = "Position size resolved to zero shares."
    else:
        # Unrecognized error type - still print the raw detail to the log,
        # but also surface a short version in the email instead of a
        # content-free generic phrase, so a new/unexpected failure mode
        # (e.g. auth, config, network) doesn't require a log dig every time.
        short_msg = msg if len(msg) <= 160 else msg[:157] + "..."
        clean = f"Unexpected error ({type(e).__name__}): {short_msg}"
    print(f"[data-quality] {context}: {msg}")  # full raw detail always goes to the log too
    return clean


def build_atr_trade_plans(watchlist, trade_settings, indicators=None, scores=None):
    """Re-fetches price history for each shortlisted ticker and runs it
    through build_atr_trade_plan. Kept as a separate fetch (rather than
    reusing compute_technical_indicators' cached results) since that
    function doesn't retain the raw OHLC frame needed for ATR.

    When a plan succeeds (has entry/stop/shares) and `indicators`/`scores`
    are supplied, also attaches a multi-target exit plan under the
    'exit_plan' key, using that ticker's RSI, volume ratio, and whether
    earnings fall within the next 5 trading days.

    Returns (plans, data_quality_alerts) - the second is a list of clean,
    human-readable strings for any failure, suitable for the email; the
    raw exception text is only ever printed to the console/log."""
    indicators = indicators or {}
    scores = scores or {}
    plans = {}
    alerts = []
    for ticker in watchlist:
        try:
            df = yf.download(
                tickers=ticker, period="15mo", interval="1d",
                progress=False, auto_adjust=False,
            )
            # yfinance sometimes returns MultiIndex columns even for a
            # single ticker - same defensive unwrap as
            # compute_technical_indicators, just never applied here before.
            if isinstance(df.columns, pd.MultiIndex):
                if ticker in df.columns.get_level_values(-1):
                    df = df.xs(ticker, axis=1, level=-1)
                elif ticker in df.columns.get_level_values(0):
                    df = df[ticker]
            df = df.dropna(subset=["Close", "High", "Low", "Volume"])
            if df is None or len(df) < 30:
                plans[ticker] = {
                    "ticker": ticker, "status": "PASS",
                    "reason": "Not enough price history available for ATR sizing.",
                }
                alerts.append(f"{ticker}: ATR plan unavailable - not enough price history.")
                continue
            plan = build_atr_trade_plan(
                df, ticker,
                portfolio_value=trade_settings["portfolio_value"],
                max_risk_pct=trade_settings["max_risk_pct"],
                max_position_pct=trade_settings["max_position_pct"],
            )
            if plan.get("shares", 0) and plan.get("entry") is not None:
                try:
                    ind = indicators.get(ticker, {})
                    s = scores.get(ticker, {})
                    earnings_days = s.get("earnings_days_away")
                    exit_plan = build_exit_plan(
                        df,
                        entry_price=plan["entry"],
                        stop_price=plan["stop_loss"],
                        total_shares=plan["shares"],
                        rsi=ind.get("rsi"),
                        volume_ratio=ind.get("vol_ratio"),
                        earnings_within_5_days=(earnings_days is not None and earnings_days <= 5),
                    )
                    plan["exit_plan"] = exit_plan
                except Exception as e:
                    clean_reason = humanize_exception(f"{ticker} exit plan", e)
                    plan["exit_plan"] = None
                    plan["exit_plan_reason"] = clean_reason
                    alerts.append(f"{ticker}: exit plan unavailable - {clean_reason}")
            plans[ticker] = plan
        except Exception as e:
            clean_reason = humanize_exception(f"{ticker} ATR trade plan", e)
            plans[ticker] = {"ticker": ticker, "status": "PASS", "reason": clean_reason}
            alerts.append(f"{ticker}: ATR plan unavailable - {clean_reason}")
    return plans, alerts


def format_atr_trade_plan_table(watchlist, plans):
    lines = []
    header = (
        f"{'TICKER':8}{'STATUS':>10}{'ENTRY':>9}{'STOP-LOSS':>11}{'TAKE-PROFIT':>13}"
        f"{'ATR':>7}{'SHARES':>8}{'R:R':>7}{'REASON':>50}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for ticker in watchlist:
        plan = plans.get(ticker)
        if not plan:
            lines.append(f"{ticker:8}  (no ATR trade plan available)")
            continue
        status = plan.get("status", "n/a")
        reason = plan.get("reason", "")
        if "entry" in plan:
            lines.append(
                f"{ticker:8}{status:>10}{plan['entry']:>9.2f}{plan['stop_loss']:>11.2f}"
                f"{plan['target_sell']:>13.2f}{plan['atr']:>7.2f}{plan['shares']:>8}"
                f"{plan['reward_risk']:>6.2f}x{reason:>50}"
            )
        else:
            lines.append(f"{ticker:8}{status:>10}{'':>48}{reason:>50}")
    return "\n".join(lines) if watchlist else "(watchlist is empty)"


def format_exit_plan_section(watchlist, atr_plans):
    """Multi-target scale-out plan per ticker: every scored target candidate
    (name, price, reward:risk, 0-100 ranking score, notes), which one (if
    any) is the primary target, and the fixed-percentage share allocation
    across target 1 / target 2 / runner. Collapses to a single clean line
    per ticker (or for the whole section) when no exit plan exists - no
    value in printing the full methodology when there's nothing to show."""
    if not watchlist:
        return "(watchlist is empty)"

    blocks = []
    any_succeeded = False
    for ticker in watchlist:
        plan = atr_plans.get(ticker)
        exit_plan = plan.get("exit_plan") if plan else None
        if not exit_plan:
            reason = plan.get("exit_plan_reason") if plan else None
            reason_text = reason or "the ATR trade plan did not succeed for this ticker"
            blocks.append(f"{ticker}: exit plan unavailable - {reason_text}.")
            continue

        any_succeeded = True
        lines = [
            f"{ticker} - status: {exit_plan['status']} - entry {exit_plan['entry_price']}, "
            f"stop-loss {exit_plan['stop_price']}, risk/share {exit_plan['risk_per_share']}, "
            f"ATR {exit_plan['atr']}"
        ]

        primary = exit_plan["primary_target"]
        primary_name = primary["name"] if primary else None

        for candidate in exit_plan["target_candidates"]:
            flag = "  <- PRIMARY TARGET" if candidate["name"] == primary_name else ""
            lines.append(
                f"  [{candidate['name']}] take-profit {candidate['price']}, "
                f"R:R {candidate['reward_risk']}x, score {candidate['ranking_score']}/100{flag}"
            )
            lines.append(f"      {candidate['notes']}")

        if primary is None and exit_plan["target_candidates"]:
            lines.append(
                "  No candidate met the minimum 2.5 reward:risk - none is endorsed as a primary target."
            )

        scale = exit_plan["scale_out_plan"]
        t1 = f"{scale['target_1_price']} = {scale['target_1_shares']} shares" if scale["target_1_price"] is not None else "n/a"
        t2 = f"{scale['target_2_price']} = {scale['target_2_shares']} shares" if scale["target_2_price"] is not None else "n/a"
        lines.append(f"  Scale-out: target 1 @ {t1}, target 2 @ {t2}, runner = {scale['runner_shares']} shares")

        blocks.append("\n".join(lines))

    if not any_succeeded:
        return "Exit plan unavailable for every shortlisted ticker - see Data-quality alerts below."

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Trade Planner (Google Drive) sync
# ---------------------------------------------------------------------------

def infer_setup_type(ind):
    """Rough, best-effort label for the Trade Planner's 'Setup Type' column,
    derived from data the screen already computes. Not a rigorous
    classification - feel free to hand-edit this column after the fact;
    the writer never overwrites a row it didn't just create."""
    if not ind:
        return "Screen Candidate"
    if ind.get("higher_highs") and ind.get("higher_lows"):
        return "Trend Continuation"
    price = ind.get("price")
    support = ind.get("nearest_support")
    if price is not None and support is not None and price <= support * 1.03:
        return "Pullback to Support"
    return "Momentum"


def build_trade_planner_candidates(watchlist, indicators, scores, trade_plans, atr_trade_plans):
    """Converts today's final watchlist into the dict shape
    trade_planner_writer.update_trade_planner() expects. Company name isn't
    available anywhere in this pipeline (only tickers), so it's left equal
    to the ticker - fill it in by hand in the sheet, or tell me and I'll
    add a ticker->company lookup table."""
    candidates = []
    for ticker in watchlist:
        ind = indicators.get(ticker) or {}
        s = scores.get(ticker) or {}
        fixed_plan = trade_plans.get(ticker) or {}
        atr_plan = atr_trade_plans.get(ticker) or {}
        rr = ind.get("reward_risk") or {}

        entry = fixed_plan.get("entry") or atr_plan.get("entry") or ind.get("price")
        stop = atr_plan.get("stop_loss") or fixed_plan.get("stop_loss") or rr.get("stop")
        target = fixed_plan.get("target_sell") or rr.get("target")

        exec_status = get_execution_status(ticker, trade_plans, atr_trade_plans)
        status = "Pass" if exec_status == "Ready" else "Watch"

        candidates.append({
            "ticker": ticker,
            "company": ticker,
            "market": TRADE_PLANNER_MARKET,
            "setup_type": infer_setup_type(ind),
            "trend_score": s.get("trend"),
            "momentum_score": s.get("momentum"),
            "earnings_score": s.get("earnings"),
            "location_score": s.get("location"),
            "entry": entry,
            "support": ind.get("nearest_support"),
            "atr": atr_plan.get("atr"),
            "stop": stop,
            "resistance": ind.get("nearest_resistance"),
            "target": target,
            "status": status,
            "notes": f"Auto-added by daily pipeline - {datetime.now().strftime('%Y-%m-%d')} - Execution: {exec_status}",
        })
    return candidates


def sync_trade_planner(watchlist, indicators, scores, trade_plans, atr_trade_plans):
    """Downloads the Trade Planner workbook from Drive, appends any new
    candidates below the last used row (never touching rows you've already
    annotated), and re-uploads only if something was actually added.
    Returns a clean, human-readable status string for the email; raises
    nothing - callers should still wrap this in try/except for the
    Data-Quality Alerts section, since Drive/network calls can fail."""
    candidates = build_trade_planner_candidates(watchlist, indicators, scores, trade_plans, atr_trade_plans)
    if not candidates:
        return "No candidates today - Trade Planner not touched."
    download_workbook(TRADE_PLANNER_LOCAL)
    added = update_trade_planner(TRADE_PLANNER_LOCAL, candidates)
    if added:
        upload_workbook(TRADE_PLANNER_LOCAL)
        return f"{added} new row(s) added to Trade Planner."
    return "All of today's candidates were already in Trade Planner - nothing added."


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
    watchlist_us = portfolio.get("watchlist_us", [])
    weekend = is_weekend()
    data_quality_alerts = []

    # --- Holdings: Trade Journal (Excel, via Drive) is the source of truth.
    #     portfolio.json's "holdings" list is only used as a fallback if the
    #     Drive read fails, so the pipeline still runs on a bad network day. ---
    holdings = portfolio.get("holdings", [])
    try:
        download_workbook(TRADE_PLANNER_LOCAL)
        journal_holdings = read_holdings_from_trade_journal(TRADE_PLANNER_LOCAL, market=TRADE_PLANNER_MARKET)
        holdings = journal_holdings
        portfolio["holdings"] = journal_holdings  # keep the fallback cache fresh for the next run
    except Exception as e:
        clean = humanize_exception("Trade Journal holdings read", e)
        data_quality_alerts.append(
            f"Trade Journal holdings read failed, using portfolio.json holdings as fallback - {clean}"
        )

    holdings_tickers = [h["ticker"] for h in holdings]

    # --- Holdings: live price + qualitative hold/sell read ---
    snapshots = {h["ticker"]: fetch_snapshot(h["ticker"]) for h in holdings}
    holdings_rows, total_cost, total_value = build_holdings_table(holdings, snapshots)
    holdings_table = format_holdings_table(holdings_rows) if holdings_rows else ""

    try:
        analysis, verdicts = get_claude_analysis(holdings_rows, total_cost, total_value)
    except Exception as e:
        clean = humanize_exception("holdings analysis", e)
        analysis = f"Holdings analysis unavailable - {clean}"
        verdicts = {}
        data_quality_alerts.append(f"Holdings analysis unavailable - {clean}")

    portfolio_action_count = sum(1 for v in verdicts.values() if v != "HOLD")

    # --- Watchlist: auto-remove anything now actually owned ---
    auto_removed = [t for t in watchlist_us if t in holdings_tickers]
    watchlist_us = [t for t in watchlist_us if t not in holdings_tickers]
    changes_log = [f"- Removed {t}: now an actual holding, tracked there instead" for t in auto_removed]

    # --- Watchlist: score-based technical screen (top 15 by total score) ---
    try:
        watchlist_us, screen_changes, indicators, scores, stage1_shortlist, stage2_eliminated = screen_watchlist(
            watchlist_us, holdings_tickers
        )
        changes_log += screen_changes
    except Exception as e:
        indicators, scores = {}, {}
        stage1_shortlist, stage2_eliminated = [], []
        clean = humanize_exception("watchlist screen", e)
        changes_log.append(f"- Watchlist screen failed: {clean}")
        data_quality_alerts.append(f"Watchlist screen failed - {clean}")

    portfolio["watchlist_us"] = watchlist_us
    save_portfolio(portfolio)  # always save: even "no changes" reflects a fresh screen run

    changes_text = "\n".join(changes_log) if changes_log else "(no changes today)"

    # --- Full Stage 1 / Stage 2 detail: console/log only, never in the email body ---
    print("=" * 70)
    print(f"FULL STAGE 1 SHORTLIST ({len(stage1_shortlist)}) - log only, not in email")
    print(format_stage1_table(stage1_shortlist))
    print()
    print(f"FULL STAGE 2 ELIMINATIONS ({len(stage2_eliminated)}) - log only, not in email")
    print(format_stage2_eliminated_table(stage2_eliminated))
    print("=" * 70)

    watchlist_table = format_watchlist_table(watchlist_us, indicators, scores)
    stage1_tickers = {c["ticker"] for c in stage1_shortlist}
    full_universe_table = format_full_universe_table(indicators, stage1_tickers)
    levels_table = format_levels_table(watchlist_us, indicators)

    trade_settings = portfolio["trade_settings"]
    trade_plans = build_trade_plans(watchlist_us, indicators, trade_settings)
    trade_plan_table = format_trade_plan_table(watchlist_us, trade_plans)

    atr_trade_plans, atr_alerts = build_atr_trade_plans(watchlist_us, trade_settings, indicators, scores)
    data_quality_alerts += atr_alerts
    exit_plan_section = format_exit_plan_section(watchlist_us, atr_trade_plans)
    atr_trade_plan_table = format_atr_trade_plan_table(watchlist_us, atr_trade_plans)

    # --- Trade Planner (Google Drive) sync: append-only, never blocks the email ---
    try:
        trade_planner_status = sync_trade_planner(
            watchlist_us, indicators, scores, trade_plans, atr_trade_plans
        )
    except Exception as e:
        clean = humanize_exception("Trade Planner sync", e)
        trade_planner_status = f"Sync failed - {clean}"
        data_quality_alerts.append(f"Trade Planner sync failed - {clean}")

    if weekend:
        premarket_section = "Not applicable - weekend, US market closed."
    else:
        try:
            premarket_gaps = build_premarket_gaps(watchlist_us)
            premarket_table, premarket_explanations = format_premarket_table(watchlist_us, premarket_gaps)
            premarket_section = (
                "gap % = (pre-market price - previous close) / previous close x 100\n"
                "<1%: OK - plan unchanged | 1-3%: Recalculate entry/RR | >3%: Review manually - large gap\n\n"
                f"{premarket_table}\n\nWhy the significant gaps happened:\n{premarket_explanations}"
            )
        except Exception as e:
            clean = humanize_exception("pre-market gap check", e)
            premarket_section = f"Pre-market gap check unavailable - {clean}"
            data_quality_alerts.append(f"Pre-market gap check unavailable - {clean}")

    overall_pl_pct = (
        round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0
    )

    # --- Decision summary: the single most important message, stated first ---
    decision_summary = build_decision_summary(
        holdings, portfolio_action_count, watchlist_us, scores, trade_plans,
        atr_trade_plans, weekend, len(data_quality_alerts),
    )

    rejects = closest_rejects(stage2_eliminated, n=5)
    if rejects:
        rejects_lines = "\n".join(
            f"  {r['ticker']}: {r['reward_risk']}x" if r["reward_risk"] is not None else f"  {r['ticker']}: n/a"
            for r in rejects
        )
    else:
        rejects_lines = "  (none)"

    data_quality_text = (
        "\n".join(f"- {a}" for a in data_quality_alerts) if data_quality_alerts else "None."
    )

    report_label = "WEEKEND STRATEGY REVIEW" if weekend else "DAILY PRE-MARKET NOTE"
    if weekend:
        price_data_line = f"Price data through: {last_trading_day_label()} US market close"
    else:
        price_data_line = f"Price data through: {datetime.now().strftime('%d %b %Y')} (most recent available)"
    premarket_freshness_line = (
        "Premarket data: Not applicable - weekend"
        if weekend else
        f"Premarket data: as of {datetime.now().strftime('%d %b %Y %H:%M')} run time"
    )

    # --- New Trade Candidates: setup status (always Qualified for this list)
    #     vs execution status (can we actually size/act on it today) ---
    candidate_lines = []
    for ticker in sorted(watchlist_us, key=lambda t: scores.get(t, {}).get("total", 0), reverse=True):
        s = scores.get(ticker, {})
        fixed_plan = trade_plans.get(ticker)
        exec_status = get_execution_status(ticker, trade_plans, atr_trade_plans)
        rr = fixed_plan["reward_risk"] if fixed_plan else "n/a"
        candidate_lines.append(
            f"{ticker}: score {s.get('total', 'n/a')}/85 | Setup status: Qualified | "
            f"Execution status: {exec_status} | Fixed-buffer reward:risk: {rr}x"
        )
    candidates_summary = "\n".join(candidate_lines) if candidate_lines else "(none)"

    holdings_section = (
        f"{holdings_table}\n\n{analysis}" if holdings_rows else "Current holdings: None\n\n" + analysis
    )

    body = f"""{report_label} - {datetime.now().strftime('%A, %d %B %Y')}

Generated: {datetime.now().strftime('%d %b %Y, %H:%M')} (server time)
{price_data_line}
{premarket_freshness_line}

====================================================
1. DECISION SUMMARY
====================================================
{decision_summary}

====================================================
2. PORTFOLIO ACTIONS
====================================================
Current holdings: {len(holdings)}
Portfolio actions required: {portfolio_action_count}

{holdings_section}

====================================================
3. NEW TRADE CANDIDATES
====================================================
Qualification: base score >= {BASE_SCORE_MINIMUM}/55 and reward:risk >= {REWARD_RISK_MINIMUM}x. Full methodology in the repo README.

{candidates_summary}

--- Fixed-buffer sizing ---
{trade_plan_table}

--- ATR-based sizing (volatility-adjusted stop-loss) ---
{atr_trade_plan_table}

--- Exit plan (multi-target scale-out) ---
{exit_plan_section}

--- Support / resistance detail ---
{levels_table}

--- Trade Planner (Google Sheet/Excel) sync ---
{trade_planner_status}

====================================================
4. REJECTED / WATCH NAMES
====================================================
Stage 1 passed: {len(stage1_shortlist)}
Rejected on reward:risk: {len(stage2_eliminated)}
Final candidates: {len(watchlist_us)}

Closest reward:risk rejects:
{rejects_lines}

Changes today:
{changes_text}

====================================================
5. FULL UNIVERSE SNAPSHOT ({len(indicators)} tickers)
====================================================
Every ticker in today's screening universe, technicals only - nothing filtered out.

{full_universe_table}

====================================================
6. MARKET AND PRE-MARKET VALIDATION
====================================================
{premarket_section}

====================================================
7. DATA-QUALITY ALERTS
====================================================
{data_quality_text}

--------------------------------------------------
This is an automated research note, not financial advice. Data may be delayed
or incomplete; verify anything before acting on it.
"""

    print(body)

    if os.environ.get("EMAIL_ADDRESS"):
        send_email(f"{report_label.title()} - {datetime.now().strftime('%Y-%m-%d')}", body)
        print("\nEmail sent.")
    else:
        print("\nEMAIL_ADDRESS not set - skipped sending email.")


if __name__ == "__main__":
    sys.exit(main())
