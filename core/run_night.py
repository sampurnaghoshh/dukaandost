"""Runs one merchant through the full nightly cycle from the command line.

    python -m core.run_night --merchant tea_stall_01
    python -m core.run_night --llm-mode replay      # no network at all

Step 3 covers triage, LLM candidate generation, the simulator and the call brief. The call
itself, the holdout assignment and the measurement land in later steps and slot in after
the brief below.

Every decision the agent makes is appended to logs/decisions.jsonl as one JSON line,
including the nights it decides to stay silent, the candidates the number guard dropped and
the actions the guardrails refused. The judge dashboard reads that file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

from core import fixtures, generate, ledger, llm, simulate, triage
from core.holdout import HOLDOUT_SHARE
from voice.base import Brief, BriefNumbers

LOG_PATH = os.path.join(ledger.REPO_ROOT, "logs", "decisions.jsonl")
RUPEE = "₹"
SAMPLE_MESSAGE_COUNT = 3


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


# --------------------------------------------------------------------------
# Triage
# --------------------------------------------------------------------------


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
    if not (result["signals"]["offpeak_gaps"]["count"]
            or result["signals"]["affinity_gaps"]["count"]):
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
    print("  WORTH A CALL. Both thresholds cleared." if result["worth_a_call"]
          else "  NO CALL TONIGHT. Nothing here is far enough from this shop's own norm.")


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def generate_candidates(conn, merchant_id: str, triage_result: dict) -> dict:
    """Asks the LLM for candidates. Falls back to the fixtures if nothing usable comes back."""
    events: list = []
    record = {"item_resolution": None, "dropped": [], "errors": [], "source": "llm"}

    item_map = None
    try:
        raw_names = ledger.raw_item_names(conn, merchant_id)
        item_map = generate.resolve_items(raw_names, on_event=events.append)
        record["item_resolution"] = {
            "raw_names": len(item_map),
            "canonical_keys": sorted(set(item_map.values())),
            "mapping": item_map,
        }
    except llm.LLMError as exc:
        record["errors"].append("resolve_items: %s: %s" % (type(exc).__name__, exc))

    candidates: list = []
    try:
        evidence = generate.build_evidence(triage_result, item_map)
        record["evidence_sent"] = evidence
        proposal = generate.propose(evidence, on_event=events.append)
        record["dropped"] = proposal["dropped"]
        products = evidence.get("products_on_the_menu")
        candidates = [generate.normalise(candidate, triage_result, index, products)
                      for index, candidate in enumerate(proposal["kept"])]
    except llm.LLMError as exc:
        record["errors"].append("propose: %s: %s" % (type(exc).__name__, exc))

    if not candidates:
        candidates = [dict(item, source="fixture") for item in fixtures.PLACEHOLDER_CANDIDATES]
        record["source"] = "fixture"
        record["fallback_reason"] = ("no valid LLM candidates, fell back to the Step 2 "
                                     "placeholders")

    record["events"] = events
    record["candidates"] = candidates
    return record


def print_generation(record: dict) -> None:
    _rule("Candidate generation")
    resolution = record.get("item_resolution")
    if resolution:
        print("  Item resolution: %d raw spellings collapsed to %d products, %s"
              % (resolution["raw_names"], len(resolution["canonical_keys"]),
                 ", ".join(resolution["canonical_keys"])))
    for error in record["errors"]:
        print("  LLM problem: %s" % error)
    if record["source"] == "fixture":
        print("  Using the hand written placeholders: %s" % record.get("fallback_reason", ""))
    else:
        print("  The model proposed %d usable candidates." % len(record["candidates"]))
    for dropped in record["dropped"]:
        print("  DROPPED %r: %s" % (dropped["title"], "; ".join(dropped["reasons"])))


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------


def print_ranking(ranking: dict) -> None:
    baseline = ranking["baseline"]
    _rule("Simulator")
    print("  Two rates, kept apart on purpose:")
    print("    baseline return rate  %.4f   MEASURED  %s" % (baseline["rate"], baseline["method"]))
    print("    offer uplift          per level  PRIOR     PRIOR_OFFER_UPLIFT %s, replaced by "
          "measured lift once the holdout reports" % json.dumps(simulate.PRIOR_OFFER_UPLIFT))
    print("  Guardrails: margin floor %.0f percent, monthly discount budget %s%.0f, "
          "positive expected profit required, %.0f percent held back unmessaged."
          % (100 * simulate.MARGIN_FLOOR, RUPEE, simulate.MONTHLY_DISCOUNT_BUDGET_RUPEES,
             100 * HOLDOUT_SHARE))

    _rule("Offer level grid, expected profit over %d days" % ranking["horizon_days"])
    print("  The model proposes the action. Code prices every depth and keeps the best one")
    print("  that survives the guardrails. An x means that level was refused.")
    print("")
    print("  %-38s %10s %10s %10s   chosen"
          % ("candidate", "LOW 10pc", "MED 20pc", "HIGH 35pc"))
    for row in ranking["level_grid"]:
        cells = []
        for level in ("LOW", "MEDIUM", "HIGH"):
            cell = row["levels"][level]
            cells.append("%9.2f%s" % (cell["expected_profit"], " " if cell["survives"] else "x"))
        print("  %-38s %10s %10s %10s   %s"
              % (row["candidate_id"][:38], cells[0], cells[1], cells[2],
                 row["chosen_level"] or "REFUSED"))

    _rule("Ranked actions, expected profit over %d days" % ranking["horizon_days"])
    if not ranking["ranked"]:
        print("  Nothing clears the guardrails. The agent proposes doing nothing tonight.")
    for item in ranking["ranked"]:
        _print_candidate(item)

    if ranking["rejected"]:
        _rule("Refused")
        for item in ranking["rejected"]:
            print("  %s   [%s], refused at every level" % (item["title"], item["candidate_id"]))
            for reason in item["rejections"]:
                print("     refused: %s" % reason)


def _print_candidate(item: dict) -> None:
    estimates = item["estimates"]
    print("")
    print("  %d. %s   [%s, from the %s, level %s]"
          % (item["rank"], item["title"], item["candidate_id"], item["source"],
             item["offer_level"]))
    if item.get("rationale"):
        print("     why            %s" % item["rationale"])
    print("     offer          %s" % _offer_text(item["offer"]))
    print("     segment        %d customers, %d treated and %d held back at %.0f percent"
          % (estimates["segment_size"], estimates["treated_count"], estimates["holdout_count"],
             100 * estimates["holdout_share"]))
    print("     baseline       %.2f percent of the treated respond with no offer at all"
          % (100 * estimates["baseline_response_rate"]))
    print("       source       %s" % estimates["baseline_source"])
    print("     offer uplift   %.2f percentage points on top of that"
          % (100 * estimates["offer_uplift"]))
    print("       source       %s" % estimates["uplift_source"])
    print("     responses      %.2f baseline plus %.2f incremental, %.2f redeem in total"
          % (estimates["baseline_responses"], estimates["incremental_responses"],
             estimates["expected_responses"]))
    print("     revenue        %s%.2f incremental only, the baseline was never ours to claim"
          % (RUPEE, estimates["incremental_revenue"]))
    print("     gross profit   %s%.2f at %.0f percent margin"
          % (RUPEE, estimates["incremental_gross_profit"], 100 * estimates["gross_margin_rate"]))
    print("     discount cost  %s%.2f, paid on all %.2f redemptions including the baseline"
          % (RUPEE, estimates["discount_cost"], estimates["expected_responses"]))
    print("     message cost   %s%.2f, treated group only" % (RUPEE, estimates["message_cost"]))
    print("     EXPECTED PROFIT %s%.2f" % (RUPEE, estimates["expected_profit"]))
    for line in estimates["basis"]:
        print("       based on: %s" % line)


def _offer_text(offer: dict) -> str:
    if not offer:
        return "none"
    if offer.get("type") == "percent_discount":
        return ("%s percent off %s for %s days, level %s"
                % (offer.get("value"), offer.get("applies_to"), offer.get("validity_days"),
                   offer.get("offer_level", "from the percentage")))
    return json.dumps(offer, ensure_ascii=False)


# --------------------------------------------------------------------------
# The brief
# --------------------------------------------------------------------------


def build_brief(merchant_id: str, merchant_name: str, triage_result: dict, chosen: dict,
                script: dict) -> Brief:
    estimates = chosen["estimates"]
    lapsed = triage_result["signals"]["lapsed_regulars"]
    filled = generate.fill_call_script(script["template"], merchant_name, triage_result, chosen)
    return Brief(
        merchant_id=merchant_id,
        merchant_name=merchant_name,
        today=triage_result["today"],
        candidate_id=chosen["candidate_id"],
        action_type=chosen["action_type"],
        title=chosen["title"] or "",
        rationale=chosen.get("rationale") or "",
        script=filled,
        script_template=script["template"],
        script_source=script["source"],
        customer_message_template=chosen.get("message_template") or "",
        numbers=BriefNumbers(
            lapsed_count=lapsed["count"],
            value_at_risk_monthly=lapsed["value_at_risk_monthly"],
            discount_pct=estimates["discount_pct"],
            offer_applies_to=chosen["offer"].get("applies_to", ""),
            segment_size=estimates["segment_size"],
            treated_count=estimates["treated_count"],
            holdout_count=estimates["holdout_count"],
            holdout_share=estimates["holdout_share"],
            baseline_response_rate=estimates["baseline_response_rate"],
            offer_uplift=estimates["offer_uplift"],
            incremental_responses=estimates["incremental_responses"],
            incremental_revenue=estimates["incremental_revenue"],
            discount_cost=estimates["discount_cost"],
            expected_profit=estimates["expected_profit"],
            horizon_days=estimates["horizon_days"],
        ),
        evidence=triage_result["evidence"],
    )


def print_brief(brief: Brief, triage_result: dict, chosen: dict,
                customer_names: dict | None = None) -> None:
    _rule("Call brief")
    print("  action     %s   [%s]" % (brief.title, brief.candidate_id))
    print("  script     written by the %s, every figure filled by code" % brief.script_source)
    print("")
    print("  TEMPLATE, as the model wrote it, with no numbers in it at all:")
    print("    %s" % brief.script_template)
    print("")
    print("  FILLED, as the agent will speak it:")
    print("    %s" % brief.script)

    if brief.customer_message_template:
        _rule("Customer messages")
        print("  TEMPLATE: %s" % brief.customer_message_template)
        print("")
        names = customer_names or {}
        for row in triage_result["signals"]["lapsed_regulars"]["detail"][:SAMPLE_MESSAGE_COUNT]:
            name = names.get(row["customer_id"], row["customer_id"])
            filled = generate.fill_customer_message(
                brief.customer_message_template, name, brief.merchant_name,
                chosen, days_absent=row["days_since_last_visit"])
            print("  %-10s %s" % (row["customer_id"], filled))
        print("")
        print("  The name is resolved at dispatch and nowhere earlier. Triage, the simulator")
        print("  and the holdout all work on ids, and the merchant never sees a phone number.")


# --------------------------------------------------------------------------


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one merchant through one nightly cycle.")
    parser.add_argument("--merchant", default="tea_stall_01", help="merchant id in the ledger")
    parser.add_argument("--today", default=None,
                        help="YYYY-MM-DD, defaults to the ledger's data_as_of")
    parser.add_argument("--db", default=None, help="path to the SQLite ledger")
    parser.add_argument("--log", default=LOG_PATH, help="decision log to append to")
    parser.add_argument("--llm-mode", default=None, choices=["live", "record", "replay"],
                        help="overrides LLM_MODE for this run")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip generation entirely and use the fixtures")
    parser.add_argument("--json", action="store_true", help="dump the raw dicts instead")
    args = parser.parse_args(argv)

    if args.llm_mode:
        os.environ["LLM_MODE"] = args.llm_mode
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    conn = ledger.connect(args.db)
    try:
        today = (date.fromisoformat(args.today) if args.today
                 else ledger.data_as_of(conn, args.merchant))
        merchant_name = ledger.merchant(conn, args.merchant)["name"]
        triage_result = triage.score(args.merchant, today, conn=conn)

        log_decision({
            "merchant_id": args.merchant,
            "today": triage_result["today"],
            "stage": "triage",
            "decision": triage_result["decision"],
            "headline": "worth a call" if triage_result["worth_a_call"] else "no call tonight",
            "opportunity_score": triage_result["opportunity_score"],
            "revenue_share": triage_result["revenue_share"],
            "thresholds": triage_result["thresholds"],
            "signals": triage_result["signals"],
            "evidence": triage_result["evidence"],
        }, args.log)

        generation = ranking = brief = None
        if triage_result["worth_a_call"]:
            if args.no_llm:
                generation = {"source": "fixture", "candidates": list(fixtures.PLACEHOLDER_CANDIDATES),
                              "dropped": [], "errors": [], "events": [],
                              "fallback_reason": "generation skipped by --no-llm"}
            else:
                generation = generate_candidates(conn, args.merchant, triage_result)

            log_decision({
                "merchant_id": args.merchant,
                "today": triage_result["today"],
                "stage": "generate",
                "decision": "candidates_ready" if generation["candidates"] else "nothing_generated",
                "headline": "%d candidates from the %s"
                            % (len(generation["candidates"]), generation["source"]),
                "llm_mode": llm.mode(),
                "llm_model": llm.model_id(),
                "source": generation["source"],
                "item_resolution": generation.get("item_resolution"),
                "evidence_sent": generation.get("evidence_sent"),
                "generated": generation["candidates"],
                "dropped": generation["dropped"],
                "errors": generation["errors"],
                "events": generation["events"],
            }, args.log)

            ranking = simulate.rank(args.merchant, generation["candidates"], triage_result,
                                    today=today, conn=conn)
            recommended = ranking["recommended"]

            script = None
            if recommended:
                script = ({"template": fixtures.FALLBACK_CALL_SCRIPT, "source": "fixture",
                           "problems": ["generation skipped by --no-llm"]} if args.no_llm
                          else generate.call_script(on_event=generation["events"].append))
                brief = build_brief(args.merchant, merchant_name, triage_result,
                                    recommended, script)

            log_decision({
                "merchant_id": args.merchant,
                "today": ranking["today"],
                "stage": "simulate",
                "decision": "recommend" if recommended else "no_viable_action",
                "headline": (recommended["title"] if recommended
                             else "nothing clears the guardrails"),
                "candidate_source": generation["source"],
                "guardrails": ranking["guardrails"],
                "baseline": ranking["baseline"],
                "priors_used": ranking["priors_used"],
                "recommended": recommended,
                "ranked": ranking["ranked"],
                "rejected": ranking["rejected"],
                "brief": json.loads(brief.model_dump_json()) if brief else None,
                "script_source": script["source"] if script else None,
            }, args.log)

        if args.json:
            print(json.dumps(
                {"triage": triage_result, "generation": generation, "simulation": ranking,
                 "brief": json.loads(brief.model_dump_json()) if brief else None},
                indent=2, ensure_ascii=False, default=str))
            return 0

        print_triage(triage_result)
        if generation is not None:
            print_generation(generation)
        if ranking is not None:
            print_ranking(ranking)
            _rule("Recommendation")
            if ranking["recommended"]:
                best = ranking["recommended"]
                print("  %s, expected profit %s%.2f over %d days."
                      % (best["title"], RUPEE, best["estimates"]["expected_profit"],
                         ranking["horizon_days"]))
            else:
                print("  Say nothing tonight. Every candidate loses money or breaks a guardrail.")
        if brief is not None:
            print_brief(brief, triage_result, ranking["recommended"],
                        ledger.customer_names(conn, args.merchant))
        if ranking is None:
            _rule("Generation and simulator")
            print("  Skipped. Triage decided this merchant is not worth a call tonight, so no")
            print("  LLM runs at all. That is the whole cost argument.")

        _rule("Decision log")
        print("  Appended to %s" % os.path.relpath(args.log, ledger.REPO_ROOT))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
