"""Runs one approved campaign from assignment through dispatch, outcomes and measurement.

The sequence, and the order matters:

  1. assign     85 treated, 15 held back, deterministic and disjoint, persisted
  2. predict    record what the simulator expects BEFORE anything is sent
  3. dispatch   render a Hindi message per treated customer, never for the control group
  4. observe    72 hours later, what each customer actually did
  5. measure    lift = treated conversion minus control conversion, by subtraction
  6. learn      pooled lift, shrunk toward the prior, becomes the next campaign's estimate

Step 2 happens before step 3 on purpose. A prediction recorded after the outcome is known
is not a prediction, and the learning curve would be worthless.

Step 4 is the only part that is simulated. It calls world/outcomes.py, which holds the
hidden truth, and is the single place in the nightly loop that does. Everything after it
does arithmetic on the rows it produced, with no idea where they came from.
"""

from __future__ import annotations

import sqlite3
import uuid

from core import dispatch, holdout, measure, simulate
from memory import store


def new_campaign_id() -> str:
    return "camp_%s" % uuid.uuid4().hex[:10]


def launch(memory: sqlite3.Connection, merchant_id: str, chosen: dict, shop_name: str,
           customer_ids: list, customer_facts: dict | None = None,
           campaign_id: str | None = None, seed: int = holdout.DEFAULT_SEED,
           customer_names: dict | None = None) -> dict:
    """Assign, record the prediction, render the messages. Everything before the waiting."""
    campaign_id = campaign_id or new_campaign_id()

    assignment = holdout.assign_and_save(memory, campaign_id, customer_ids, seed=seed)
    record = store.record_campaign(memory, campaign_id, merchant_id, chosen)
    sent = dispatch.dispatch(memory, campaign_id, assignment, chosen, shop_name,
                             customer_facts, customer_names=customer_names)

    return {
        "campaign_id": campaign_id,
        "sequence": record["sequence"],
        "assignment": assignment,
        "dispatch": sent,
        "predicted": {
            "offer_level": chosen["estimates"]["offer_level"],
            "discount_pct": chosen["estimates"]["discount_pct"],
            "uplift": chosen["estimates"]["offer_uplift"],
            "uplift_source": chosen["estimates"]["uplift_source"],
            "incremental_responses": chosen["estimates"]["incremental_responses"],
            "incremental_revenue": chosen["estimates"]["incremental_revenue"],
            "expected_profit": chosen["estimates"]["expected_profit"],
        },
    }


def observe(memory: sqlite3.Connection, campaign_id: str, assignment: dict,
            offer_level: str, monthly_value_per_customer: float,
            horizon_days: int = simulate.VALUE_HORIZON_DAYS) -> list:
    """Seventy two hours pass. This is the only simulated step in the whole loop."""
    from world import outcomes as world_outcomes

    rows = world_outcomes.simulate_outcomes(
        campaign_id, assignment, offer_level, monthly_value_per_customer, horizon_days)
    store.save_outcomes(memory, campaign_id, rows)
    return rows


def close(memory: sqlite3.Connection, campaign_id: str) -> dict:
    """Measures and files the result. Pure arithmetic on the recorded outcomes."""
    return measure.measure(memory, campaign_id,
                           gross_margin_rate=simulate.GROSS_MARGIN_RATE,
                           message_cost_rupees=simulate.MESSAGE_COST_RUPEES)
