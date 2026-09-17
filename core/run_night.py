"""Runs one merchant through the full nightly cycle from the command line.

    python -m core.run_night --merchant tea_stall_01

Step 2 covers triage and the simulator. Generation, the call, the holdout and the
measurement land in later steps and slot in after the ranking below.

Every decision the agent makes is appended to logs/decisions.jsonl as one JSON line,
including the nights it decides to stay silent. The judge dashboard reads that file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

from core import fixtures, ledger, simulate, triage

LOG_PATH = os.path.join(ledger.REPO_ROOT, "logs", "decisions.jsonl")
RUPEE = "₹"


def log_decision(record: dict, log_path: str = LOG_PATH) -> None:
    """One JSON line per decision. Append only, so the night's reasoning is never lost."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    record = dict(record, logged_at=datetime.now().isoformat(timespec="seconds"))
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _rule(title: str) -> None:
    print("")
    print(title)
    print("=" * len(title))


def print_triage(result: dict) -> None:
    base = result["merchant_baseline"]
    _rule("%s, night of %s" % (result["merchant_name"], result["today"]))
    print("  %s%.0f a month over the last %d days, %.0f transactions a month, "
          "average ticket %s%.2f"
          % (RUPEE, base["monthly_revenue"], base["window_days"],
             base["monthly_transactions"], RUPEE, base["avg_ticket"]))
    print("  %d customers active, %d of them regulars"
          % (base["active_customers"], base["regular_customers"]))

    lapsed = result["signals"]["lapsed_regulars"]
    _rule("Lapsed regulars")
    if lapsed["count"] == 0:
        print("  None. No regular is outside their own rhythm tonight.")
    else:
        print("  %-10s %-12s %8s %10s %12s" %
              ("customer", "last visit", "silent", "visit days", "monthly value"))
        for row in lapsed["detail"]:
            print("  %-10s %-12s %6d d %10d %11s%.0f"
                  % (row["customer_id"], row["last_visit"], row["days_since_last_visit"],
                     row["visit_days_in_baseline"], RUPEE, row["monthly_value"]))
        print("")
        print("  Value at risk: %s%.0f a month across %d customers."
              % (RUPEE, lapsed["value_at_risk_monthly"], lapsed["count"]))
        print("  How they were found: %s" % lapsed["method"])

    _rule("Other signals")
    for gap in result["signals"]["offpeak_gaps"]["detail"]:
        print("  Off peak  %s %02d:00 to %02d:00, %.2f transactions against a norm of %.2f, "
              "%.0f percent down, %s%.0f a month"
              % (gap["weekday_name"], gap["hours"][0], gap["hours"][-1] + 1,
                 gap["observed_txns_per_occurrence"], gap["expected_txns_per_occurrence"],
                 gap["shortfall_pct"], RUPEE, gap["monthly_upside"]))
    for gap in result["signals"]["affinity_gaps"]["detail"]:
        print("  Affinity  %s attaches an add on %.1f percent of the time, %s manages "
              "%.1f percent, %s%.0f a month"
              % (gap["anchor_item"], 100 * gap["attach_rate"], gap["benchmark_item"],
                 100 * gap["benchmark_attach_rate"], RUPEE, gap["monthly_upside"]))
    if not result["signals"]["offpeak_gaps"]["count"] and not result["signals"]["affinity_gaps"]["count"]:
        print("  None.")

    _rule("Triage decision")
    signals = result["signals"]
    print("  lapsed value at risk   %10.2f  x %.2f  =  %10.2f"
          % (signals["lapsed_regulars"]["value_at_risk_monthly"], triage.LAPSED_WEIGHT,
             signals["lapsed_regulars"]["weighted_contribution"]))
    print("  off peak upside        %10.2f  x %.2f  =  %10.2f"
          % (signals["offpeak_gaps"]["monthly_upside"], triage.OFFPEAK_WEIGHT,
             signals["offpeak_gaps"]["weighted_contribution"]))
    print("  affinity upside        %10.2f  x %.2f  =  %10.2f"
          % (signals["affinity_gaps"]["monthly_upside"], triage.AFFINITY_WEIGHT,
             signals["affinity_gaps"]["weighted_contribution"]))
    print("  %s" % ("-" * 52))
    print("  opportunity score      %10.2f rupees a month at stake, %.1f percent of revenue"
          % (result["opportunity_score"], 100 * result["revenue_share"]))
    print("  threshold              %10.2f and %.1f percent of revenue"
          % (result["thresholds"]["min_monthly_rupees"],
             100 * result["thresholds"]["min_revenue_share"]))
    print("")
    if result["worth_a_call"]:
        print("  WORTH A CALL. Both thresholds cleared.")
    else:
        print("  NO CALL TONIGHT. Nothing here is far enough from this shop's own norm.")


def print_ranking(ranking: dict) -> None:
    history = ranking["history"]
    _rule("Simulator")
    print("  Return rate learned from this shop: %d lapse episodes, %d came back on their "
          "own, shrunk to %.3f against a %.2f prior."
          % (history["episodes"], history["returned"], history["rate"], history["prior_rate"]))
    print("  Guardrails: margin floor %.0f percent, monthly discount budget %s%.0f, "
          "positive expected profit required."
          % (100 * simulate.MARGIN_FLOOR, RUPEE, simulate.MONTHLY_DISCOUNT_BUDGET_RUPEES))

    _rule("Ranked actions, expected profit over %d days" % ranking["horizon_days"])
    if not ranking["ranked"]:
        print("  Nothing clears the guardrails. The agent proposes doing nothing tonight.")
    for item in ranking["ranked"]:
        estimates = item["estimates"]
        print("")
        print("  %d. %s   [%s]" % (item["rank"], item["title"], item["candidate_id"]))
        print("     offer          %s" % _offer_text(item["offer"]))
        print("     reach          %d customers messaged" % estimates["targets_messaged"])
        print("     baseline       %.2f percent respond with no contact"
              % (100 * estimates["baseline_response_rate"]))
        print("     uplift         x%.2f expected from the message and the offer"
              % estimates["uplift_multiplier"])
        print("     incremental    %.2f responses beyond what would have happened anyway"
              % estimates["incremental_responses"])
        print("     revenue        %s%.2f incremental" % (RUPEE, estimates["incremental_revenue"]))
        print("     gross profit   %s%.2f at %.0f percent margin"
              % (RUPEE, estimates["incremental_gross_profit"], 100 * estimates["gross_margin_rate"]))
        print("     discount cost  %s%.2f, paid on every redemption including the "
              "customers who were coming back anyway" % (RUPEE, estimates["discount_cost"]))
        print("     message cost   %s%.2f" % (RUPEE, estimates["message_cost"]))
        print("     EXPECTED PROFIT %s%.2f" % (RUPEE, estimates["expected_profit"]))
        for line in estimates["basis"]:
            print("       based on: %s" % line)

    if ranking["rejected"]:
        _rule("Refused")
        for item in ranking["rejected"]:
            print("  %s   [%s]" % (item["title"], item["candidate_id"]))
            for reason in item["rejections"]:
                print("     refused: %s" % reason)

    _rule("Recommendation")
    if ranking["recommended"]:
        best = ranking["recommended"]
        print("  %s, expected profit %s%.2f over %d days."
              % (best["title"], RUPEE, best["estimates"]["expected_profit"],
                 ranking["horizon_days"]))
        print("  Next step, once Step 3 is in: the LLM writes this in Hindi with placeholders,")
        print("  the merchant approves it by voice, and a 15 percent holdout decides the truth.")
    else:
        print("  Say nothing tonight. Every candidate either loses money or breaks a guardrail.")


def _offer_text(offer: dict) -> str:
    if not offer:
        return "none"
    if offer.get("type") == "percent_discount":
        return ("%s percent off %s for %s days"
                % (offer.get("value"), offer.get("applies_to"), offer.get("validity_days")))
    return json.dumps(offer, ensure_ascii=False)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one merchant through one nightly cycle.")
    parser.add_argument("--merchant", default="tea_stall_01", help="merchant id in the ledger")
    parser.add_argument("--today", default=None,
                        help="YYYY-MM-DD, defaults to the ledger's data_as_of")
    parser.add_argument("--db", default=None, help="path to the SQLite ledger")
    parser.add_argument("--log", default=LOG_PATH, help="decision log to append to")
    parser.add_argument("--json", action="store_true", help="dump the raw dicts instead")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    conn = ledger.connect(args.db)
    try:
        today = date.fromisoformat(args.today) if args.today else ledger.data_as_of(conn, args.merchant)
        triage_result = triage.score(args.merchant, today, conn=conn)

        log_decision({
            "merchant_id": args.merchant,
            "today": triage_result["today"],
            "stage": "triage",
            "decision": triage_result["decision"],
            "headline": ("worth a call" if triage_result["worth_a_call"] else "no call tonight"),
            "opportunity_score": triage_result["opportunity_score"],
            "revenue_share": triage_result["revenue_share"],
            "thresholds": triage_result["thresholds"],
            "signals": triage_result["signals"],
            "evidence": triage_result["evidence"],
        }, args.log)

        ranking = None
        if triage_result["worth_a_call"]:
            ranking = simulate.rank(args.merchant, fixtures.PLACEHOLDER_CANDIDATES,
                                    triage_result, today=today, conn=conn)
            recommended = ranking["recommended"]
            log_decision({
                "merchant_id": args.merchant,
                "today": ranking["today"],
                "stage": "simulate",
                "decision": "recommend" if recommended else "no_viable_action",
                "headline": (recommended["title"] if recommended
                             else "nothing clears the guardrails"),
                "candidate_source": "core/fixtures.py placeholders, LLM generation lands in Step 3",
                "guardrails": ranking["guardrails"],
                "history": ranking["history"],
                "recommended": recommended,
                "ranked": ranking["ranked"],
                "rejected": ranking["rejected"],
            }, args.log)

        if args.json:
            print(json.dumps({"triage": triage_result, "simulation": ranking},
                             indent=2, ensure_ascii=False, default=str))
            return 0

        print_triage(triage_result)
        if ranking is not None:
            print_ranking(ranking)
        else:
            _rule("Simulator")
            print("  Skipped. Triage decided this merchant is not worth a call tonight,")
            print("  so no LLM and no simulation runs. That is the whole cost argument.")

        _rule("Decision log")
        print("  Appended to %s" % os.path.relpath(args.log, ledger.REPO_ROOT))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
