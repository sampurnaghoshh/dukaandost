"""Runs six campaigns end to end and shows the prediction error shrinking.

    python -m scripts.run_campaigns --campaigns 6

Each campaign: triage, simulate with whatever the merchant's own history has taught so far,
assign a holdout, dispatch, wait 72 hours, measure lift by subtraction, write predicted and
actual back to memory. The next campaign starts from the updated number.

Writes the series to dashboard/learning.json for the chart.
"""

from __future__ import annotations

import argparse
import os
import sys

from core import campaign, holdout, ledger, simulate, triage
from core.run_night import generate_candidates
from memory import store

LEARNING_PATH = os.path.join(ledger.REPO_ROOT, "dashboard", "learning.json")
RUPEE = "₹"


def _truth(offer_level: str) -> float:
    """The simulated world's hidden answer, for scoring only. Imported here, in a script,
    never anywhere the agent can reach it."""
    from world import outcomes as world_outcomes
    return world_outcomes.truth_for(offer_level)


def run(merchant_id: str, campaigns: int, memory_path: str | None,
        fresh: bool = True, use_fixtures: bool = False) -> dict:
    if fresh:
        store.reset(memory_path)

    conn = ledger.connect()
    memory = store.connect(memory_path)
    try:
        today = ledger.data_as_of(conn, merchant_id)
        shop_name = ledger.merchant(conn, merchant_id)["name"]
        triage_result = triage.score(merchant_id, today, conn=conn)

        lapsed = triage_result["signals"]["lapsed_regulars"]
        customer_ids = list(lapsed["customer_ids"])
        facts = {row["customer_id"]: row for row in lapsed["detail"]}
        value_each = lapsed["value_at_risk_monthly"] / max(1, lapsed["count"])

        if use_fixtures:
            from core import fixtures
            candidates = [dict(item, source="fixture")
                          for item in fixtures.PLACEHOLDER_CANDIDATES]
        else:
            candidates = generate_candidates(conn, merchant_id, triage_result)["candidates"]

        rows = []
        for number in range(1, campaigns + 1):
            # Rank afresh every night, so each campaign is priced with everything measured
            # up to that point and nothing after it.
            ranking = simulate.rank(merchant_id, candidates, triage_result, today=today,
                                    conn=conn, memory=memory)
            chosen = ranking["recommended"]
            if chosen is None:
                print("campaign %d: nothing clears the guardrails, stopping" % number)
                break

            campaign_id = "camp_%s_%02d" % (merchant_id, number)
            launched = campaign.launch(memory, merchant_id, chosen, shop_name, customer_ids,
                                       customer_facts=facts, campaign_id=campaign_id,
                                       seed=holdout.DEFAULT_SEED + number)
            campaign.observe(memory, campaign_id, launched["assignment"],
                             chosen["estimates"]["offer_level"], value_each)
            measurement = campaign.close(memory, campaign_id)

            rows.append({
                "sequence": number,
                "campaign_id": campaign_id,
                "offer_level": chosen["estimates"]["offer_level"],
                "predicted_uplift": chosen["estimates"]["offer_uplift"],
                "actual_uplift": measurement["lift"],
                "uplift_error": measurement["uplift_error"],
                "predicted_profit": chosen["estimates"]["expected_profit"],
                "actual_profit": measurement["actual_profit"],
                "profit_error": measurement["profit_error"],
                "treated": measurement["treated_n"],
                "treated_returns": measurement["treated_returns"],
                "control": measurement["control_n"],
                "control_returns": measurement["control_returns"],
                "uplift_source": chosen["estimates"]["uplift_source"],
                # Evaluation overlay only. The agent cannot see this and never uses it.
                "hidden_truth": _truth(chosen["estimates"]["offer_level"]),
                "belief_error_vs_truth": abs(chosen["estimates"]["offer_uplift"]
                                             - _truth(chosen["estimates"]["offer_level"])),
            })

        payload = store.export_learning(memory, merchant_id, LEARNING_PATH)
        payload["rows"] = rows
        return payload
    finally:
        conn.close()
        memory.close()


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run campaigns and chart the learning.")
    parser.add_argument("--merchant", default="tea_stall_01")
    parser.add_argument("--campaigns", type=int, default=6)
    parser.add_argument("--memory", default=None, help="path to the campaign memory db")
    parser.add_argument("--keep", action="store_true", help="do not wipe existing memory")
    parser.add_argument("--fixtures", action="store_true", help="skip the LLM entirely")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    payload = run(args.merchant, args.campaigns, args.memory, fresh=not args.keep,
                  use_fixtures=args.fixtures)
    rows = payload["rows"]

    print("")
    print("Six campaigns, same twelve customers, same action, learning as it goes")
    print("=" * 78)
    print("  %-3s %-7s %9s %9s %10s %9s   %10s %10s"
          % ("no", "level", "believed", "measured", "treated", "control",
             "pred prof", "actual"))
    for row in rows:
        print("  %-3d %-7s %9.4f %9.4f %6d/%-3d %6d/%-2d   %10.2f %10.2f"
              % (row["sequence"], row["offer_level"], row["predicted_uplift"],
                 row["actual_uplift"], row["treated_returns"], row["treated"],
                 row["control_returns"], row["control"],
                 row["predicted_profit"], row["actual_profit"]))

    print("")
    print("  Belief against the hidden truth, which is the learning curve that matters:")
    print("  %-3s %10s %10s %12s" % ("no", "believed", "truth", "error"))
    for row in rows:
        print("  %-3d %10.4f %10.4f %12.4f"
              % (row["sequence"], row["predicted_uplift"], row["hidden_truth"],
                 row["belief_error_vs_truth"]))

    if rows:
        from world import outcomes as world_outcomes
        truth = world_outcomes.truth_for(rows[0]["offer_level"])
        print("")
        print("  The hidden truth for %s is %.2f. The agent never sees it."
              % (rows[0]["offer_level"], truth))
        print("  Campaign 1 predicted %.4f, off by %.4f."
              % (rows[0]["predicted_uplift"], abs(rows[0]["predicted_uplift"] - truth)))
        print("  Campaign %d predicted %.4f, off by %.4f."
              % (rows[-1]["sequence"], rows[-1]["predicted_uplift"],
                 abs(rows[-1]["predicted_uplift"] - truth)))
        print("")
        print("  %-34s %s" % ("first campaign uplift source:", rows[0]["uplift_source"][:80]))
        print("  %-34s %s" % ("last campaign uplift source:", rows[-1]["uplift_source"][:80]))

    print("")
    print("  Series written to %s" % os.path.relpath(LEARNING_PATH, ledger.REPO_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
