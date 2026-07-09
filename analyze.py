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
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText

import yfinance as yf
import pandas as pd
import numpy as np
import anthropic

PORTFOLIO_FILE = "portfolio.json"
MODEL = "claude-sonnet-4-6"
WATCHLIST_MAX_US = 15

# A broad, liquid US candidate universe to screen daily for the watchlist.
# Not an exhaustive S&P 500 list, but a representative, liquid cross-section
# across sectors so the screen has enough breadth to find real candidates.
US_CANDIDATE_UNIVERSE = [
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META","AVGO","TSLA","AMD","CRM",
    "ORCL","ADBE","INTC","QCOM","TXN","MU","AMAT","LRCX","KLAC","CSCO",
    "IBM","NOW","PANW","SNPS","CDNS","INTU","FTNT","PLTR","UBER","ABNB",
    "SHOP","NET","DDOG","SNOW","CRWD","ZS","MDB","TEAM","WDAY","ADSK",
    "JPM","BAC","WFC","GS","MS","C","SCHW","BLK","AXP","V",
    "MA","PYPL","SPGI","ICE","CME","BX","KKR","APO","COF","USB",
    "UNH","JNJ","LLY","PFE","MRK","ABBV","TMO","ABT","DHR","BMY",
    "AMGN","GILD","VRTX","REGN","ISRG","CVS","CI","HUM","MDT","SYK",
    "XOM","CVX","COP","SLB","EOG","PXD","OXY","MPC","PSX","VLO",
    "HD","LOW","MCD","SBUX","NKE","TJX","TGT","COST","WMT","PG",
    "KO","PEP","PM","MO","CL","KMB","GIS","MDLZ","EL","STZ",
    "BA","CAT","DE","HON","GE","RTX","LMT","NOC","GD","UPS",
    "FDX","UNP","CSX","NSC","WM","ETN","EMR","ITW","PH","ROK",
    "DIS","NFLX","CMCSA","T","VZ","TMUS","CHTR","WBD","EA","TTWO",
    "LIN","APD","SHW","ECL","NEM","FCX","NUE","DOW","DD","PPG",
    "NEE","DUK","SO","D","AEP","EXC","SRE","PCG","XEL","ED",
    "PLD","AMT","EQIX","PSA","SPG","O","WELL","DLR","AVB","EQR",
]


def load_portfolio():
    with open(PORTFOLIO_FILE, "r") as f:
        portfolio = json.load(f)
    # Migrate any old key names from earlier versions of this tool.
    if "watchlist" in portfolio and "watchlist_us" not in portfolio:
        portfolio["watchlist_us"] = portfolio.pop("watchlist")
    portfolio.setdefault("watchlist_us", [])
    portfolio.setdefault("holdings", [])
    portfolio.setdefault("closed_positions", [])
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
padding it."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )

    text_parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(text_parts)


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


def add_pivots(df, left=2, right=2):
    """Marks pivot highs/lows: a bar whose High (Low) is the max (min) within
    a window of `left` bars before and `right` bars after it. Standard swing
    high/low definition used for reading market structure."""
    df = df.copy()
    df["pivot_high"] = False
    df["pivot_low"] = False
    for i in range(left, len(df) - right):
        high_window = df["High"].iloc[i - left:i + right + 1]
        low_window = df["Low"].iloc[i - left:i + right + 1]
        df.loc[df.index[i], "pivot_high"] = df["High"].iloc[i] == high_window.max()
        df.loc[df.index[i], "pivot_low"] = df["Low"].iloc[i] == low_window.min()
    return df


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
            if len(tickers) == 1:
                df = data
            else:
                df = data[ticker]
            df = df.dropna(subset=["Close", "High", "Low", "Volume"])
            if df is None or len(df) < 55:
                results[ticker] = None
                continue

            close = df["Close"]
            volume = df["Volume"]
            ema20 = close.ewm(span=20, adjust=False).mean()
            ema50 = close.ewm(span=50, adjust=False).mean()
            rsi = compute_rsi(close, 14)

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


def screen_watchlist(current_watchlist, holdings_tickers):
    """Scores the full candidate universe and takes the top WATCHLIST_MAX_US
    scorers as the new watchlist, after first rejecting any candidate whose
    reward:risk is below 2.5. Returns (new_watchlist, changes_log,
    indicators_by_ticker, scores_by_ticker).
    """
    universe = sorted(set(US_CANDIDATE_UNIVERSE) | set(current_watchlist))
    universe = [t for t in universe if t not in holdings_tickers]

    indicators = compute_technical_indicators(universe)

    scores = {}
    rejected_rr = {}  # ticker -> actual R:R, for tickers rejected by the R:R floor
    for ticker in universe:
        ind = indicators.get(ticker)
        if not ind:
            continue

        rr_data = ind.get("reward_risk")
        if rr_data is not None and rr_data["reward_risk"] < 2.5:
            rejected_rr[ticker] = rr_data["reward_risk"]
            continue  # hard reject: R:R below 2.5, don't even score it in

        earnings_days = get_earnings_trading_days_away(ticker)
        scores[ticker] = score_ticker(ind, earnings_days)

    ranked = sorted(scores.items(), key=lambda kv: kv[1]["total"], reverse=True)
    new_watchlist = [ticker for ticker, _ in ranked[:WATCHLIST_MAX_US]]

    changes = []
    old_set = set(current_watchlist)
    new_set = set(new_watchlist)

    for ticker in old_set - new_set:
        if ticker in rejected_rr:
            changes.append(f"- Dropped {ticker}: reward:risk {rejected_rr[ticker]} below the 2.5 floor")
            continue
        s = scores.get(ticker)
        if s:
            changes.append(f"- Dropped {ticker}: score {s['total']}/85, no longer in the top {WATCHLIST_MAX_US}")
        else:
            changes.append(f"- Dropped {ticker}: insufficient data to score")

    for ticker in new_set - old_set:
        s = scores[ticker]
        changes.append(
            f"- Added {ticker}: score {s['total']}/85 "
            f"(Trend {s['trend']}/25, Momentum {s['momentum']}/20, "
            f"Earnings {s['earnings']}/10, Location {s['location']}/30)"
        )

    return new_watchlist, changes, indicators, scores


def format_watchlist_table(watchlist, indicators, scores):
    lines = []
    header = (
        f"{'TICKER':8}{'SCORE':>9}{'TREND':>8}{'MOM':>6}{'EARN':>6}{'LOC':>7}"
        f"{'PRICE':>10}{'20EMA':>9}{'50EMA':>9}{'RSI':>7}{'VOLx(20d)':>11}"
        f"{'PIVOT HI':>10}{'PIVOT LO':>10}{'EARN(days)':>12}"
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
            f"{pivot_hi_str:>8}{hh_flag:>2}{pivot_lo_str:>8}{hl_flag:>2}{days_str:>12}"
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
        f"{'TICKER':8}{'PRICE':>10}{'SUPPORT':>10}{'RESIST':>10}"
        f"{'STOP':>10}{'TARGET':>10}{'RISK':>8}{'REWARD':>9}{'R:R':>7}"
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
                f"{ticker:8}{ind['price']:>10.2f}{support_str:>10}{resistance_str:>10}"
                f"{rr['stop']:>10.2f}{rr['target']:>10.2f}{rr['risk']:>8.2f}"
                f"{rr['reward']:>9.2f}{rr['reward_risk']:>6.2f}x"
            )
        else:
            lines.append(
                f"{ticker:8}{ind['price']:>10.2f}{support_str:>10}{resistance_str:>10}"
                f"{'n/a':>10}{'n/a':>10}{'n/a':>8}{'n/a':>9}{'n/a':>7}"
            )
    return "\n".join(lines) if watchlist else "(watchlist is empty)"



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
    watchlist_us = portfolio.get("watchlist_us", [])
    holdings_tickers = [h["ticker"] for h in holdings]

    # --- Holdings: live price + qualitative hold/sell read ---
    snapshots = {h["ticker"]: fetch_snapshot(h["ticker"]) for h in holdings}
    holdings_rows, total_cost, total_value = build_holdings_table(holdings, snapshots)
    holdings_table = format_holdings_table(holdings_rows) if holdings_rows else "(no holdings logged yet)"

    try:
        analysis = get_claude_analysis(holdings_rows, total_cost, total_value)
    except Exception as e:
        analysis = f"[Claude analysis failed: {e}]"

    # --- Watchlist: auto-remove anything now actually owned ---
    auto_removed = [t for t in watchlist_us if t in holdings_tickers]
    watchlist_us = [t for t in watchlist_us if t not in holdings_tickers]
    changes_log = [f"- Removed {t}: now an actual holding, tracked there instead" for t in auto_removed]

    # --- Watchlist: score-based technical screen (top 15 by total score) ---
    try:
        watchlist_us, screen_changes, indicators, scores = screen_watchlist(watchlist_us, holdings_tickers)
        changes_log += screen_changes
    except Exception as e:
        indicators, scores = {}, {}
        changes_log.append(f"[Watchlist screen failed: {e}]")

    portfolio["watchlist_us"] = watchlist_us
    save_portfolio(portfolio)  # always save: even "no changes" reflects a fresh screen run

    changes_text = "\n".join(changes_log) if changes_log else "(no changes today)"
    watchlist_table = format_watchlist_table(watchlist_us, indicators, scores)
    levels_table = format_levels_table(watchlist_us, indicators)

    overall_pl_pct = (
        round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0
    )

    body = f"""DAILY STOCK NOTE - {datetime.now().strftime('%A, %B %d, %Y')}

PORTFOLIO SNAPSHOT
Total cost basis: ${total_cost:,.2f}
Total current value: ${total_value:,.2f}
Overall unrealized P/L: {overall_pl_pct}%

{holdings_table}

--------------------------------------------------
ANALYSIS
--------------------------------------------------
{analysis}

--------------------------------------------------
WATCHLIST (US) - SCORED TECHNICAL SCREEN (top {WATCHLIST_MAX_US} by score, out of 85)
--------------------------------------------------
Trend (25): price>20EMA +10, 20EMA>50EMA +10, higher highs +5
Momentum (20): RSI 50-60 +10 / 60-65 +8 / 65-70 +5 / >70 +0; Volume vs 20-day avg: above +10 / in line with +5 / below +0
Earnings (10): earnings within 5 trading days +0, within 6-10 +5, else +10
Location (30): near support (<=3% +15 / <=6% +10 / <=10% +5), room below resistance (>=8% +5 / >=5% +3), reward:risk (>=4 +10 / >=3 +8 / >=2.5 +5)
Any ticker with reward:risk below 2.5 is rejected outright and never scored in.

{watchlist_table}

CHANGES TODAY
{changes_text}

--------------------------------------------------
SUPPORT / RESISTANCE & REWARD:RISK (sorted best R:R first)
--------------------------------------------------
Support/resistance = nearest confirmed pivot low/high in the last 60 sessions.
Stop = support minus a 0.5% buffer. Target = nearest resistance. R:R = reward / risk.
"n/a" means no confirmed pivot was found on that side within the lookback window.

{levels_table}

--------------------------------------------------
This is an automated research note, not financial advice. Data may be delayed
or incomplete; verify anything before acting on it.
"""

    print(body)

    if os.environ.get("EMAIL_ADDRESS"):
        send_email(f"Daily Stock Note - {datetime.now().strftime('%Y-%m-%d')}", body)
        print("\nEmail sent.")
    else:
        print("\nEMAIL_ADDRESS not set - skipped sending email.")


if __name__ == "__main__":
    sys.exit(main())
