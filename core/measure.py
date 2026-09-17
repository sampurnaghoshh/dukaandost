"""True lift at 72 hours: treated conversion minus control conversion, never a model estimate.

This module does arithmetic on what happened. It has no model, no prior and no coefficient.
If you ever find something here that resembles an estimate, that is a bug.

    lift = treated_returns / treated_n  -  control_returns / control_n

That subtraction is the answer to "how do you know it was you". The control group got the
same offer written for them and was deliberately never sent it, so whatever they did anyway
is what the treated group would have done anyway, and the difference is ours.

The prediction the simulator made is read back here only to record how wrong it was. It
never enters the measurement.
"""

from __future__ import annotations

import sqlite3

from memory import store

MEASUREMENT_HOURS = 72


def lift_from_outcomes(outcomes: list) -> dict:
    """Pure counting. Hand it rows with an arm and a returned flag and it does subtraction."""
    treated = [row for row in outcomes if row["arm"] == "treated"]
    control = [row for row in outcomes if row["arm"] == "control"]

    treated_n = len(treated)
    control_n = len(control)
    treated_returns = sum(1 for row in treated if row["returned"])
    control_returns = sum(1 for row in control if row["returned"])

    treated_rate = treated_returns / treated_n if treated_n else 0.0
    control_rate = control_returns / control_n if control_n else 0.0
    lift = treated_rate - control_rate

    treated_revenue = sum(row["revenue"] for row in treated)
    control_revenue = sum(row["revenue"] for row in control)
    revenue_per_control = (control_revenue / control_n) if control_n else 0.0

    # Incremental revenue is what the treated group produced beyond what the same number of
    # untouched customers produced. Scaling the control group up to treated size is the only
    # fair comparison when the arms are different sizes.
    incremental_revenue = treated_revenue - (revenue_per_control * treated_n)

    return {
        "treated_n": treated_n,
        "treated_returns": treated_returns,
        "treated_rate": treated_rate,
        "treated_revenue": round(treated_revenue, 2),
        "control_n": control_n,
        "control_returns": control_returns,
        "control_rate": control_rate,
        "control_revenue": round(control_revenue, 2),
        "lift": lift,
        "incremental_returns": round(lift * treated_n, 2),
        "incremental_revenue": round(incremental_revenue, 2),
        "method": ("treated conversion minus control conversion, %d of %d against %d of %d"
                   % (treated_returns, treated_n, control_returns, control_n)),
    }


def measure(conn: sqlite3.Connection, campaign_id: str, gross_margin_rate: float,
            message_cost_rupees: float) -> dict:
    """Measures a campaign from its recorded outcomes and files the result in memory."""
    record = store.campaign(conn, campaign_id)
    if record is None:
        raise LookupError("no campaign %s in memory" % campaign_id)
    outcomes = store.outcomes(conn, campaign_id)
    if not outcomes:
        raise LookupError("campaign %s has no recorded outcomes yet" % campaign_id)

    result = lift_from_outcomes(outcomes)

    # Actual profit uses the discount actually offered, charged on everyone who redeemed,
    # exactly as the prediction charged it.
    discount = (record["discount_pct"] or 0.0) / 100.0
    treated_revenue = result["treated_revenue"]
    actual_profit = (result["incremental_revenue"] * gross_margin_rate
                     - treated_revenue * discount
                     - result["treated_n"] * message_cost_rupees)

    predicted_uplift = record["predicted_uplift"]
    predicted_profit = record["predicted_profit"]
    measurement = {
        "campaign_id": campaign_id,
        "merchant_id": record["merchant_id"],
        "offer_level": record["offer_level"],
        "treated_n": result["treated_n"],
        "treated_returns": result["treated_returns"],
        "treated_rate": result["treated_rate"],
        "control_n": result["control_n"],
        "control_returns": result["control_returns"],
        "control_rate": result["control_rate"],
        "lift": result["lift"],
        "incremental_returns": result["incremental_returns"],
        "incremental_revenue": result["incremental_revenue"],
        "actual_profit": round(actual_profit, 2),
        "predicted_uplift": predicted_uplift,
        "predicted_profit": predicted_profit,
        "uplift_error": (abs(predicted_uplift - result["lift"])
                         if predicted_uplift is not None else None),
        "profit_error": (abs(predicted_profit - actual_profit)
                         if predicted_profit is not None else None),
    }
    store.save_measurement(conn, measurement)
    store.set_status(conn, campaign_id, "measured")

    measurement["measured_at_hours"] = MEASUREMENT_HOURS
    measurement["method"] = result["method"]
    return measurement
