"""
Daily SG (SGX) watchlist agent.

Same scoring engine as analyze.py (Trend/Momentum/Earnings/Location, the
two-stage gate, pivot-based support/resistance, and position sizing) but
run against a curated universe of liquid SGX-listed stocks instead of US
names, and emailed separately.

What it does, every time it runs:
  1. Loads portfolio.json for your SG holdings (if any) and watchlist_sg.
  2. Runs the same two-stage screen used for the US watchlist:
       Stage 1: Trend + Momentum + Earnings must score >= 45/55, top 20 kept.
       Stage 2: of those, reward:risk must be >= 2.5.
       Then ranks survivors by total score (out of 85) and keeps the top 10.
  3. Computes support/resistance, reward:risk, and a position-sizing trade
     plan for each shortlisted ticker, same as the US email.
  4. Emails you a plain-text note with all of the above.

Note: this intentionally does NOT include a pre-market gap section. Yahoo
Finance's "preMarketPrice" field is a US-market concept and does not
reliably populate for SGX (.SI) tickers - faking that section would be
misleading, so it's left out here.

This is a research/screening tool, not a trading bot - it never places
trades. It is not financial advice; treat its output as one input among
several.

Required environment variables (same secrets as analyze.py):
  ANTHROPIC_API_KEY, EMAIL_ADDRESS, EMAIL_APP_PASSWORD, TO_EMAIL
"""

import os
import sys
from datetime import datetime

import analyze  # reuses the shared scoring engine, pivots, and helpers

MODEL = analyze.MODEL
WATCHLIST_MAX_SG = 10
STAGE1_SHORTLIST_SIZE_SG = 15

# Curated from the current 30 STI (Straits Times Index) constituents -
# the most liquid, widely-traded names on SGX. A far smaller universe than
# the US list by necessity, since SGX has much less liquid breadth overall.
SG_CANDIDATE_UNIVERSE = [
    "A17U.SI",  # CapitaLand Ascendas REIT
    "C38U.SI",  # CapitaLand Integrated Commercial Trust
    "9CI.SI",   # CapitaLand Investment
    "C09.SI",   # City Developments
    "D05.SI",   # DBS Group Holdings
    "D01.SI",   # DFI Retail Group
    "J69U.SI",  # Frasers Centrepoint Trust
    "BUOU.SI",  # Frasers Logistics & Commercial Trust
    "G13.SI",   # Genting Singapore
    "H78.SI",   # Hongkong Land Holdings
    "J36.SI",   # Jardine Matheson Holdings
    "BN4.SI",   # Keppel Corporation
    "AJBU.SI",  # Keppel DC REIT
    "ME8U.SI",  # Mapletree Industrial Trust
    "M44U.SI",  # Mapletree Logistics Trust
    "N2IU.SI",  # Mapletree Pan Asia Commercial Trust
    "O39.SI",   # OCBC Bank
    "S58.SI",   # SATS
    "5E2.SI",   # Seatrium
    "U96.SI",   # Sembcorp Industries
    "C6L.SI",   # Singapore Airlines
    "S68.SI",   # Singapore Exchange
    "Z74.SI",   # Singtel
    "S63.SI",   # ST Engineering
    "Y92.SI",   # Thai Beverages
    "U11.SI",   # United Overseas Bank
    "U14.SI",   # UOL Group
    "V03.SI",   # Venture Corporation
    "F34.SI",   # Wilmar International
    "BS6.SI",   # Yangzijiang Shipbuilding
]


def main():
    portfolio = analyze.load_portfolio()
    holdings = portfolio.get("holdings", [])
    watchlist_sg = portfolio.get("watchlist_sg", [])
    holdings_tickers = [h["ticker"] for h in holdings]

    try:
        watchlist_sg, screen_changes, indicators, scores, stage1_shortlist, stage2_eliminated = (
            analyze.screen_watchlist(
                watchlist_sg,
                holdings_tickers,
                universe_list=SG_CANDIDATE_UNIVERSE,
                max_size=WATCHLIST_MAX_SG,
                stage1_size=STAGE1_SHORTLIST_SIZE_SG,
            )
        )
    except Exception as e:
        watchlist_sg, screen_changes = [], [f"[SG watchlist screen failed: {e}]"]
        indicators, scores, stage1_shortlist, stage2_eliminated = {}, {}, [], []

    portfolio["watchlist_sg"] = watchlist_sg
    analyze.save_portfolio(portfolio)

    changes_text = "\n".join(screen_changes) if screen_changes else "(no changes today)"
    stage1_table = analyze.format_stage1_table(stage1_shortlist)
    stage2_table = analyze.format_stage2_eliminated_table(stage2_eliminated)
    watchlist_table = analyze.format_watchlist_table(watchlist_sg, indicators, scores)
    levels_table = analyze.format_levels_table(watchlist_sg, indicators)

    trade_settings = portfolio["trade_settings"]
    trade_plans = analyze.build_trade_plans(watchlist_sg, indicators, trade_settings)
    trade_plan_table = analyze.format_trade_plan_table(watchlist_sg, trade_plans)

    body = f"""DAILY SG (SGX) WATCHLIST - {datetime.now().strftime('%A, %B %d, %Y')}

Universe: {len(SG_CANDIDATE_UNIVERSE)} liquid SGX names (current STI constituents).
This note covers the watchlist only - for SG holdings P&L, see the evening
US/portfolio email, which covers all holdings regardless of market.

--------------------------------------------------
STAGE 1 - PASSED BASE SCORE MINIMUM (top {STAGE1_SHORTLIST_SIZE_SG}, ranked by Trend+Momentum+Earnings, min {analyze.BASE_SCORE_MINIMUM}/55)
--------------------------------------------------
{stage1_table}

--------------------------------------------------
STAGE 2 - ELIMINATED ON REWARD:RISK (from the Stage 1 shortlist above, min {analyze.REWARD_RISK_MINIMUM}x)
--------------------------------------------------
{stage2_table}

--------------------------------------------------
WATCHLIST (SG) - FINAL SHORTLIST (top {WATCHLIST_MAX_SG} by total score, out of 85)
--------------------------------------------------
Trend (25): price>20EMA +10, 20EMA>50EMA +10, higher highs +5
Momentum (20): RSI 50-60 +10 / 60-65 +8 / 65-70 +5 / >70 +0; Volume vs 20-day avg: above +10 / in line with +5 / below +0
Earnings (10): earnings within 5 trading days +0, within 6-10 +5, else +10
Location (30): near support (<=3% +15 / <=6% +10 / <=10% +5), room below resistance (>=8% +5 / >=5% +3), reward:risk (>=4 +10 / >=3 +8 / >=2.5 +5)
Two-stage gate: Stage 1 rejects anything below 45/55 on Trend+Momentum+Earnings alone. Stage 2 (of what's left) rejects reward:risk below 2.5. Survivors are ranked by total score out of 85 (Location included).

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
TRADE PLAN (position sizing on shortlisted names)
--------------------------------------------------
Assumes portfolio value ${trade_settings['portfolio_value']:,.0f}, max risk per trade
{trade_settings['max_risk_pct']*100:.1f}% (${trade_settings['portfolio_value']*trade_settings['max_risk_pct']:,.2f}),
max position size {trade_settings['max_position_pct']*100:.1f}% (${trade_settings['portfolio_value']*trade_settings['max_position_pct']:,.2f}).
Adjust these in portfolio.json under "trade_settings" (shared with the US
watchlist). Shares = the smaller of (risk cap / risk per share) and
(position cap / entry price), rounded down.

{trade_plan_table}

--------------------------------------------------
This is an automated research note, not financial advice. Data may be delayed
or incomplete; verify anything before acting on it. Earnings-date coverage
for SGX names via Yahoo Finance is less complete than for US names, so the
Earnings score defaults to favorable (+10) more often here when no date is
found - treat that category with a bit more caution for SG names.
"""

    print(body)

    if os.environ.get("EMAIL_ADDRESS"):
        analyze.send_email(f"Daily SG Watchlist - {datetime.now().strftime('%Y-%m-%d')}", body)
        print("\nEmail sent.")
    else:
        print("\nEMAIL_ADDRESS not set - skipped sending email.")


if __name__ == "__main__":
    sys.exit(main())
