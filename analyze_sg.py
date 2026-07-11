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
    weekend = analyze.is_weekend()
    data_quality_alerts = []

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
        clean = analyze.humanize_exception("SG watchlist screen", e)
        watchlist_sg, screen_changes = [], [f"- SG watchlist screen failed: {clean}"]
        indicators, scores, stage1_shortlist, stage2_eliminated = {}, {}, [], []
        data_quality_alerts.append(f"SG watchlist screen failed - {clean}")

    portfolio["watchlist_sg"] = watchlist_sg
    analyze.save_portfolio(portfolio)

    changes_text = "\n".join(screen_changes) if screen_changes else "(no changes today)"

    print("=" * 70)
    print(f"FULL STAGE 1 SHORTLIST ({len(stage1_shortlist)}) - log only, not in email")
    print(analyze.format_stage1_table(stage1_shortlist))
    print()
    print(f"FULL STAGE 2 ELIMINATIONS ({len(stage2_eliminated)}) - log only, not in email")
    print(analyze.format_stage2_eliminated_table(stage2_eliminated))
    print("=" * 70)

    watchlist_table = analyze.format_watchlist_table(watchlist_sg, indicators, scores)
    levels_table = analyze.format_levels_table(watchlist_sg, indicators)

    trade_settings = portfolio["trade_settings"]
    trade_plans = analyze.build_trade_plans(watchlist_sg, indicators, trade_settings)
    trade_plan_table = analyze.format_trade_plan_table(watchlist_sg, trade_plans)

    atr_trade_plans, atr_alerts = analyze.build_atr_trade_plans(watchlist_sg, trade_settings, indicators, scores)
    data_quality_alerts += atr_alerts
    exit_plan_section = analyze.format_exit_plan_section(watchlist_sg, atr_trade_plans)
    atr_trade_plan_table = analyze.format_atr_trade_plan_table(watchlist_sg, atr_trade_plans)

    decision_summary = analyze.build_decision_summary(
        holdings=[], portfolio_action_count=0, watchlist=watchlist_sg, scores=scores,
        trade_plans=trade_plans, atr_trade_plans=atr_trade_plans, weekend=weekend,
        data_quality_count=len(data_quality_alerts),
    )
    # This script never reports on holdings (see the note in the email below),
    # so the leading "No current holdings" clause from the shared summary
    # builder doesn't apply here - drop it rather than show a misleading line.
    decision_summary = decision_summary.replace("No current holdings. ", "")

    rejects = analyze.closest_rejects(stage2_eliminated, n=5)
    if rejects:
        rejects_lines = "\n".join(
            f"  {r['ticker']}: {r['reward_risk']}x" if r["reward_risk"] is not None else f"  {r['ticker']}: n/a"
            for r in rejects
        )
    else:
        rejects_lines = "  (none)"

    data_quality_text = "\n".join(f"- {a}" for a in data_quality_alerts) if data_quality_alerts else "None."

    report_label = "WEEKEND SG STRATEGY REVIEW" if weekend else "DAILY SG PRE-MARKET NOTE"
    if weekend:
        price_data_line = f"Price data through: {analyze.last_trading_day_label()} SGX close"
    else:
        price_data_line = f"Price data through: {datetime.now().strftime('%d %b %Y')} (most recent available)"

    candidate_lines = []
    for ticker in sorted(watchlist_sg, key=lambda t: scores.get(t, {}).get("total", 0), reverse=True):
        s = scores.get(ticker, {})
        fixed_plan = trade_plans.get(ticker)
        exec_status = analyze.get_execution_status(ticker, trade_plans, atr_trade_plans)
        rr = fixed_plan["reward_risk"] if fixed_plan else "n/a"
        candidate_lines.append(
            f"{ticker}: score {s.get('total', 'n/a')}/85 | Setup status: Qualified | "
            f"Execution status: {exec_status} | Fixed-buffer reward:risk: {rr}x"
        )
    candidates_summary = "\n".join(candidate_lines) if candidate_lines else "(none)"

    body = f"""{report_label} - {datetime.now().strftime('%A, %d %B %Y')}

Generated: {datetime.now().strftime('%d %b %Y, %H:%M')} (server time)
{price_data_line}
Universe: {len(SG_CANDIDATE_UNIVERSE)} liquid SGX names (current STI constituents).
This note covers the watchlist only - for SG holdings P&L, see the evening
US/portfolio email, which covers all holdings regardless of market.

====================================================
1. DECISION SUMMARY
====================================================
{decision_summary}

====================================================
2. NEW TRADE CANDIDATES
====================================================
Qualification: base score >= {analyze.BASE_SCORE_MINIMUM}/55 and reward:risk >= {analyze.REWARD_RISK_MINIMUM}x. Full methodology in the repo README.

{candidates_summary}

--- Fixed-buffer sizing ---
{trade_plan_table}

--- ATR-based sizing (volatility-adjusted stop-loss) ---
{atr_trade_plan_table}

--- Exit plan (multi-target scale-out) ---
{exit_plan_section}

--- Support / resistance detail ---
{levels_table}

====================================================
3. REJECTED / WATCH NAMES
====================================================
Stage 1 passed: {len(stage1_shortlist)}
Rejected on reward:risk: {len(stage2_eliminated)}
Final candidates: {len(watchlist_sg)}

Closest reward:risk rejects:
{rejects_lines}

Changes today:
{changes_text}

====================================================
4. DATA-QUALITY ALERTS
====================================================
{data_quality_text}

--------------------------------------------------
This is an automated research note, not financial advice. Data may be delayed
or incomplete; verify anything before acting on it. Earnings-date coverage
for SGX names via Yahoo Finance is less complete than for US names, so the
Earnings score defaults to favorable (+10) more often here when no date is
found - treat that category with a bit more caution for SG names.
"""

    print(body)

    if os.environ.get("EMAIL_ADDRESS"):
        analyze.send_email(f"{report_label.title()} - {datetime.now().strftime('%Y-%m-%d')}", body)
        print("\nEmail sent.")
    else:
        print("\nEMAIL_ADDRESS not set - skipped sending email.")


if __name__ == "__main__":
    sys.exit(main())
