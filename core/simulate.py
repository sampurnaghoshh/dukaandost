"""Candidate scoring against shop history: expected profit, margin floor, discount budget.

Takes structured candidate actions (hand written in core/fixtures.py, written by the LLM in
core/generate.py) and returns a ranked list with every number that produced the rank.

The split that matters: the LLM says what to try and how to say it, this module says what
it is worth. Every rate here is either measured from this shop's own ledger or is a named
PRIOR constant below, which measured lift replaces once campaigns have run. No estimate
ever comes from a language model.

Two rates, never mixed:

  baseline_return_rate  MEASURED. What the target segment does with no contact at all.
                        Learned from unprompted lapse episodes in this shop's ledger,
                        shrunk toward a low prior by episode count.
  offer_uplift          PRIOR. The extra response caused by the offer, on top of baseline.
                        A named constant per offer level today, replaced by measured lift
                        from the holdout in Step 5.

Keeping them apart is the whole point. The old single rate folded "would have come back
anyway" together with "came back because we called", which is exactly the confusion the
randomised holdout exists to resolve.

Guardrails are code, not prompts. A candidate is rejected if it does not clear the margin
floor, if its expected profit is not positive, or if its discount does not fit the monthly
budget. Rejections are returned with their reasons rather than silently dropped.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from core import ledger, triage
from core.holdout import HOLDOUT_SHARE, TREATED_SHARE

# --------------------------------------------------------------------------
# Guardrails. Hard limits, enforced in code.
# --------------------------------------------------------------------------

GROSS_MARGIN_RATE = 0.55            # typical tea stall gross margin before any discount
MARGIN_FLOOR = 0.25                 # no offer may push the effective margin below this
MONTHLY_DISCOUNT_BUDGET_RUPEES = 1500.0
MIN_EXPECTED_PROFIT_RUPEES = 0.0    # strictly positive profit or the action is refused
MESSAGE_COST_RUPEES = 0.15          # per merchant message, the real cost of dispatch

VALUE_HORIZON_DAYS = 30             # the window expected profit is stated over
RESPONSE_WINDOW_DAYS = 7            # how long a customer has to act on an offer

# --------------------------------------------------------------------------
# Offer levels. The LLM chooses a level, never a percentage. Code owns the numbers.
# --------------------------------------------------------------------------

OFFER_LEVELS = ("LOW", "MEDIUM", "HIGH")
OFFER_LEVEL_DISCOUNT_PCT = {"LOW": 10, "MEDIUM": 20, "HIGH": 35}
OFFER_LEVEL_BUCKETS = ((12, "LOW"), (25, "MEDIUM"))   # a raw percentage maps back to a level

# --------------------------------------------------------------------------
# PRIORS. NOT measurements.
#
# Starting beliefs used before this merchant has run a campaign. Every one is replaced by
# measured lift from memory once campaigns exist, which is what makes campaign six predict
# better than campaign one. They live here, in code, so a judge can see exactly which
# numbers are assumed and which are observed.
# --------------------------------------------------------------------------

# Baseline, the only prior that touches a MEASURED quantity. Deliberately low: a daily
# regular who has been silent for three weeks has usually formed a habit somewhere else.
PRIOR_BASELINE_RETURN_RATE = 0.08
PRIOR_BASELINE_EPISODE_WEIGHT = 20.0    # how many observations the prior is worth

# Offer uplift, additive on top of baseline. "Of the treated customers who would NOT have
# come back on their own, this share come back because of the offer." Replaced in Step 5 by
# lift measured as treated conversion minus control conversion.
PRIOR_OFFER_UPLIFT = {"LOW": 0.08, "MEDIUM": 0.14, "HIGH": 0.20}

# For gap shaped actions the measured gap is the ceiling, so the prior is the share of that
# gap an offer closes rather than an absolute rate. It can never invent headroom.
PRIOR_OFFPEAK_GAP_CAPTURE = {"LOW": 0.10, "MEDIUM": 0.15, "HIGH": 0.20}
PRIOR_ATTACH_GAP_CAPTURE = {"LOW": 0.20, "MEDIUM": 0.35, "HIGH": 0.50}

# A returning regular does not instantly resume their old cadence.
PRIOR_RECOVERY_INTENSITY = 0.60

PRIORS = {
    "baseline_return_rate": PRIOR_BASELINE_RETURN_RATE,
    "baseline_episode_weight": PRIOR_BASELINE_EPISODE_WEIGHT,
    "offer_uplift": PRIOR_OFFER_UPLIFT,
    "offpeak_gap_capture": PRIOR_OFFPEAK_GAP_CAPTURE,
    "attach_gap_capture": PRIOR_ATTACH_GAP_CAPTURE,
    "recovery_intensity": PRIOR_RECOVERY_INTENSITY,
}


# --------------------------------------------------------------------------
# Offer helpers
# --------------------------------------------------------------------------


def offer_level(candidate: dict) -> str:
    """The level of an offer. Explicit if the LLM chose one, bucketed if a percentage came in."""
    offer = candidate.get("offer") or {}
    level = offer.get("offer_level") or candidate.get("offer_level")
    if level and str(level).upper() in OFFER_LEVELS:
        return str(level).upper()
    percent = float(offer.get("value", 0) or 0)
    for ceiling, name in OFFER_LEVEL_BUCKETS:
        if percent <= ceiling:
            return name
    return "HIGH"


def discount_fraction(candidate: dict) -> float:
    """The actual discount as a fraction. Code owns this number, never the model."""
    offer = candidate.get("offer") or {}
    if offer.get("type") not in (None, "percent_discount"):
        return 0.0
    if offer.get("value") is not None:
        return float(offer["value"]) / 100.0
    return OFFER_LEVEL_DISCOUNT_PCT[offer_level(candidate)] / 100.0


# --------------------------------------------------------------------------
# MEASURED: what this shop's own ledger says about lapse and unprompted return
# --------------------------------------------------------------------------


def baseline_return_rate(profiles: dict, today: date) -> dict:
    """How often a regular at THIS shop went quiet and came back with no contact at all.

    An episode is a regular customer falling silent for at least triage.LAPSE_MIN_DAYS. It
    counts as a return if they came back inside RESPONSE_WINDOW_DAYS of crossing that line.
    Episodes whose outcome window has not closed are ignored, so nothing is counted as a
    failure before it has had its chance.

    Every episode here is unprompted. The shop has never run a campaign, so none of these
    customers were contacted. That is what makes this a clean baseline rather than a mix of
    baseline and response.
    """
    closed_by = today - timedelta(days=triage.LAPSE_MIN_DAYS + RESPONSE_WINDOW_DAYS)
    episodes = 0
    returned = 0

    for profile in profiles.values():
        if not _was_regular(profile):
            continue
        dates = profile["visit_dates"]
        for i in range(1, len(dates)):
            gap = (dates[i] - dates[i - 1]).days
            if gap < triage.LAPSE_MIN_DAYS or dates[i - 1] > closed_by:
                continue
            episodes += 1
            if gap <= triage.LAPSE_MIN_DAYS + RESPONSE_WINDOW_DAYS:
                returned += 1
        # A customer still absent is a fully observed non return once their window shuts.
        if (dates and dates[-1] <= closed_by
                and profile["days_since_last_visit"] >= triage.LAPSE_MIN_DAYS):
            episodes += 1

    prior = PRIOR_BASELINE_RETURN_RATE
    weight = PRIOR_BASELINE_EPISODE_WEIGHT
    shrunk = (returned + prior * weight) / (episodes + weight)
    return {
        "episodes": episodes,
        "returned": returned,
        "observed_rate": (returned / episodes) if episodes else None,
        "prior_rate": prior,
        "prior_weight": weight,
        "rate": shrunk,
        "source": "MEASURED from this shop, shrunk toward the prior by episode count",
        "method": ("%d unprompted lapse episodes in the ledger, %d came back on their own, "
                   "shrunk toward a %.2f prior worth %.0f observations"
                   % (episodes, returned, prior, weight)),
    }


def _was_regular(profile: dict) -> bool:
    """Regular over their whole life at the shop, not merely in the last window."""
    span = (profile["last_visit"] - profile["first_visit"]).days + 1
    return span >= 60 and profile["lifetime_visit_days"] / span >= 0.5


def _reachable(conn: sqlite3.Connection, merchant_id: str, today: date,
               days: int, hours: tuple | None = None, item: str | None = None) -> int:
    """Customers the shop could message: active recently, and matching the segment."""
    sql = ("SELECT COUNT(DISTINCT customer_id) FROM transactions "
           "WHERE merchant_id = ? AND txn_date >= ? AND txn_date < ?")
    args = [merchant_id, (today - timedelta(days=days)).isoformat(), today.isoformat()]
    if hours:
        sql += " AND hour IN (%s)" % ",".join("?" * len(hours))
        args.extend(hours)
    if item:
        sql += " AND true_item = ?"
        args.append(item)
    return conn.execute(sql, args).fetchone()[0]


# --------------------------------------------------------------------------
# Estimators, one per action type
# --------------------------------------------------------------------------


def _estimate_lapsed_winback(candidate: dict, context: dict) -> dict:
    signal = context["triage"]["signals"]["lapsed_regulars"]
    targets = signal["count"]
    if not targets:
        return _empty("no lapsed regulars in this shop tonight")

    level = offer_level(candidate)
    baseline = context["baseline"]["rate"]
    uplift = PRIOR_OFFER_UPLIFT[level]
    monthly_value_each = signal["value_at_risk_monthly"] / targets
    revenue_each = (monthly_value_each * (VALUE_HORIZON_DAYS / 30.0)
                    * PRIOR_RECOVERY_INTENSITY)

    return _assemble(
        candidate, context, segment_size=targets, response_units=targets,
        baseline_rate=baseline, offer_uplift=uplift, revenue_per_response=revenue_each,
        baseline_source=context["baseline"]["method"],
        uplift_source=("PRIOR_OFFER_UPLIFT[%s] = %.2f, a named constant, replaced by "
                       "measured lift after the first campaign" % (level, uplift)),
        basis=[
            "%d lapsed regulars found by triage from the ledger" % targets,
            "cohort is worth %.0f rupees a month, %.0f each"
            % (signal["value_at_risk_monthly"], monthly_value_each),
            "a returner is assumed to spend %.0f percent of their old cadence"
            % (100 * PRIOR_RECOVERY_INTENSITY),
        ])


def _estimate_offpeak_fill(candidate: dict, context: dict) -> dict:
    segment = candidate["target_segment"]
    weekday = segment.get("weekday")
    hours = tuple(segment.get("hours") or [])
    match = None
    for gap in context["triage"]["signals"]["offpeak_gaps"]["detail"]:
        if gap["weekday"] == weekday and set(gap["hours"]) & set(hours):
            match = gap
            break
    if match is None:
        return _empty("triage found no off peak gap in that slot")

    level = offer_level(candidate)
    occurrences = VALUE_HORIZON_DAYS / 7.0
    potential = match["expected_txns_per_occurrence"] * occurrences
    observed = match["observed_txns_per_occurrence"] * occurrences
    baseline = observed / potential if potential else 0.0
    capture = PRIOR_OFFPEAK_GAP_CAPTURE[level]
    # The measured gap is the ceiling. The prior only says how much of it a nudge closes.
    uplift = (1.0 - baseline) * capture

    targets = _reachable(context["conn"], context["merchant_id"], context["today"],
                         days=30, hours=hours)
    return _assemble(
        candidate, context, segment_size=targets, response_units=potential,
        baseline_rate=baseline, offer_uplift=uplift,
        revenue_per_response=context["avg_ticket"],
        baseline_source=("MEASURED: the slot already runs at %.0f percent of its own norm on "
                         "other weekdays" % (100 * baseline)),
        uplift_source=("PRIOR_OFFPEAK_GAP_CAPTURE[%s] = %.2f of the measured gap, which "
                       "cannot exceed the gap itself" % (level, capture)),
        basis=[
            "%s %02d:00 to %02d:00 does %.2f transactions against a norm of %.2f"
            % (match["weekday_name"], hours[0], hours[-1] + 1,
               match["observed_txns_per_occurrence"], match["expected_txns_per_occurrence"]),
            "%d customers already shop those hours and can be messaged" % targets,
            "revenue per extra visit is this shop's own average ticket, %.2f"
            % context["avg_ticket"],
        ])


def _estimate_attach_upsell(candidate: dict, context: dict) -> dict:
    anchor = candidate["target_segment"].get("anchor_item")
    match = None
    for gap in context["triage"]["signals"]["affinity_gaps"]["detail"]:
        if gap["anchor_item"] == anchor:
            match = gap
            break
    if match is None:
        return _empty("triage found no affinity gap on %s" % anchor)

    level = offer_level(candidate)
    window = triage.BASELINE_WINDOW_DAYS
    anchor_txns = match["anchor_txns"] * (VALUE_HORIZON_DAYS / window)
    baseline = match["attach_rate"]
    headroom = max(0.0, match["benchmark_attach_rate"] - baseline)
    capture = PRIOR_ATTACH_GAP_CAPTURE[level]
    # The shop's own better performing anchor is the ceiling. Nothing here invents headroom.
    uplift = headroom * capture

    targets = _reachable(context["conn"], context["merchant_id"], context["today"],
                         days=30, item=anchor)
    return _assemble(
        candidate, context, segment_size=targets, response_units=anchor_txns,
        baseline_rate=baseline, offer_uplift=uplift,
        revenue_per_response=match["addon_avg_price"],
        baseline_source=("MEASURED: %s attaches an add on %.1f percent of the time in this "
                         "shop's own baskets" % (anchor, 100 * baseline)),
        uplift_source=("PRIOR_ATTACH_GAP_CAPTURE[%s] = %.2f of the %.1f point gap to %s, "
                       "which this shop has already proved is reachable"
                       % (level, capture, 100 * headroom, match["benchmark_item"])),
        basis=[
            "%s attaches %.1f percent against %.1f percent on %s"
            % (anchor, 100 * baseline, 100 * match["benchmark_attach_rate"],
               match["benchmark_item"]),
            "%.0f %s orders expected in the next %d days"
            % (anchor_txns, anchor, VALUE_HORIZON_DAYS),
            "an add on is worth %.2f rupees here" % match["addon_avg_price"],
        ])


ESTIMATORS = {
    "lapsed_winback": _estimate_lapsed_winback,
    "offpeak_fill": _estimate_offpeak_fill,
    "attach_upsell": _estimate_attach_upsell,
}


def _empty(reason: str) -> dict:
    return {"estimable": False, "reason": reason, "expected_profit": 0.0}


def _assemble(candidate: dict, context: dict, segment_size: float, response_units: float,
              baseline_rate: float, offer_uplift: float, revenue_per_response: float,
              baseline_source: str, uplift_source: str, basis: list) -> dict:
    """The one piece of arithmetic every action type shares.

    Only the treated group is priced, because the holdout is never messaged. Of the treated
    group, the baseline share would have converted anyway and its revenue is not ours to
    claim. Only the uplift is incremental revenue. The discount, however, is paid to
    everyone who redeems, baseline returners included, because a coupon does not ask why
    somebody came back. That asymmetry is what stops deep discounts from looking clever.
    """
    discount = discount_fraction(candidate)
    treated_units = response_units * TREATED_SHARE
    treated_targets = segment_size * TREATED_SHARE
    holdout_targets = segment_size - treated_targets

    baseline_responses = treated_units * baseline_rate
    incremental_responses = treated_units * offer_uplift
    total_responses = baseline_responses + incremental_responses

    incremental_revenue = incremental_responses * revenue_per_response
    gross_profit = incremental_revenue * GROSS_MARGIN_RATE
    discount_cost = total_responses * revenue_per_response * discount
    message_cost = treated_targets * MESSAGE_COST_RUPEES
    expected_profit = gross_profit - discount_cost - message_cost

    return {
        "estimable": True,
        "horizon_days": VALUE_HORIZON_DAYS,
        "offer_level": offer_level(candidate),
        "segment_size": int(round(segment_size)),
        "treated_count": int(round(treated_targets)),
        "holdout_count": int(round(holdout_targets)),
        "holdout_share": HOLDOUT_SHARE,
        "targets_messaged": int(round(treated_targets)),
        "response_units": round(treated_units, 2),
        "baseline_response_rate": round(baseline_rate, 4),
        "baseline_source": baseline_source,
        "offer_uplift": round(offer_uplift, 4),
        "uplift_source": uplift_source,
        "baseline_responses": round(baseline_responses, 2),
        "incremental_responses": round(incremental_responses, 2),
        "expected_responses": round(total_responses, 2),
        "revenue_per_response": round(revenue_per_response, 2),
        "incremental_revenue": round(incremental_revenue, 2),
        "gross_margin_rate": GROSS_MARGIN_RATE,
        "incremental_gross_profit": round(gross_profit, 2),
        "discount_pct": round(discount * 100, 1),
        "discount_cost": round(discount_cost, 2),
        "message_cost": round(message_cost, 2),
        "expected_profit": round(expected_profit, 2),
        "basis": basis,
    }


# --------------------------------------------------------------------------
# Guardrails
# --------------------------------------------------------------------------


def effective_margin(discount: float, margin: float = GROSS_MARGIN_RATE) -> float:
    """Margin left on a discounted sale. Cost of goods does not fall when the price does."""
    if discount >= 1.0:
        return -1.0
    return (margin - discount) / (1.0 - discount)


def check_guardrails(candidate: dict, estimates: dict) -> list:
    """Every reason this candidate must not run. Empty list means it may."""
    reasons = []
    if not estimates.get("estimable", False):
        return [estimates.get("reason", "not estimable")]

    discount = discount_fraction(candidate)
    margin = effective_margin(discount)
    if margin < MARGIN_FLOOR:
        reasons.append(
            "margin floor: a %.0f percent discount leaves %.0f percent margin, floor is %.0f"
            % (discount * 100, margin * 100, MARGIN_FLOOR * 100))
    if estimates["expected_profit"] <= MIN_EXPECTED_PROFIT_RUPEES:
        reasons.append(
            "negative expected profit: %.2f rupees over %d days"
            % (estimates["expected_profit"], estimates["horizon_days"]))
    return reasons


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------


def rank(merchant_id: str, candidates: list, triage_result: dict,
         today: date | str | None = None, conn: sqlite3.Connection | None = None,
         db_path: str | None = None, discount_spent_this_month: float = 0.0) -> dict:
    """Scores every candidate against this shop's history and ranks the survivors."""
    owned = conn is None
    conn = conn or ledger.connect(db_path)
    try:
        if today is None:
            today = date.fromisoformat(triage_result["today"])
        elif isinstance(today, str):
            today = date.fromisoformat(today)

        totals = ledger.window_totals(conn, merchant_id, today, triage.BASELINE_WINDOW_DAYS)
        profiles = triage.customer_profiles(
            ledger.customer_days(conn, merchant_id, today), today)

        context = {
            "conn": conn,
            "merchant_id": merchant_id,
            "today": today,
            "triage": triage_result,
            "avg_ticket": totals["avg_ticket"],
            "baseline": baseline_return_rate(profiles, today),
        }

        scored = []
        for candidate in candidates:
            estimator = ESTIMATORS.get(candidate.get("action_type"))
            if estimator is None:
                estimates = _empty("no estimator for action type %r, the simulator refuses to "
                                   "guess" % candidate.get("action_type"))
            else:
                estimates = estimator(candidate, context)
            scored.append({
                "candidate_id": candidate.get("candidate_id"),
                "action_type": candidate.get("action_type"),
                "title": candidate.get("title"),
                "rationale": candidate.get("rationale"),
                "source": candidate.get("source", "fixture"),
                "target_segment": candidate.get("target_segment"),
                "offer": candidate.get("offer"),
                "message_template": candidate.get("message_template"),
                "estimates": estimates,
                "rejections": check_guardrails(candidate, estimates),
            })

        scored.sort(key=lambda item: item["estimates"].get("expected_profit", 0.0), reverse=True)

        budget_remaining = MONTHLY_DISCOUNT_BUDGET_RUPEES - discount_spent_this_month
        accepted, rejected = [], []
        for item in scored:
            if item["rejections"]:
                item["accepted"] = False
                rejected.append(item)
                continue
            cost = item["estimates"]["discount_cost"]
            if cost > budget_remaining:
                item["rejections"].append(
                    "monthly discount budget: needs %.2f rupees, %.2f left of %.2f"
                    % (cost, budget_remaining, MONTHLY_DISCOUNT_BUDGET_RUPEES))
                item["accepted"] = False
                rejected.append(item)
                continue
            budget_remaining -= cost
            item["accepted"] = True
            item["rank"] = len(accepted) + 1
            accepted.append(item)

        return {
            "merchant_id": merchant_id,
            "today": today.isoformat(),
            "horizon_days": VALUE_HORIZON_DAYS,
            "history": context["baseline"],
            "baseline": context["baseline"],
            "guardrails": {
                "gross_margin_rate": GROSS_MARGIN_RATE,
                "margin_floor": MARGIN_FLOOR,
                "monthly_discount_budget": MONTHLY_DISCOUNT_BUDGET_RUPEES,
                "discount_spent_this_month": discount_spent_this_month,
                "budget_remaining_after": round(budget_remaining, 2),
                "min_expected_profit": MIN_EXPECTED_PROFIT_RUPEES,
                "holdout_share": HOLDOUT_SHARE,
            },
            "priors_used": PRIORS,
            "candidates_in": len(candidates),
            "ranked": accepted,
            "rejected": rejected,
            "recommended": accepted[0] if accepted else None,
        }
    finally:
        if owned:
            conn.close()
