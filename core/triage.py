"""Arithmetic opportunity score, runs on every merchant every night. No LLM.

score(merchant_id, today) reads the transactions table and returns the opportunity score,
the evidence behind it and whether the merchant is worth a call tonight. Cheap enough to
run on every merchant, which is the answer to "what does this cost at scale".

Three signals, all pure arithmetic:
  1. lapsed regulars       value at risk from customers who had a cadence and stopped
  2. off peak gaps         weekday and hour slots running well below their own norm
  3. basket affinity gaps  an anchor product that attaches add ons worse than its peers

Nothing in this module reads data/ground_truth.json. It has to find the cohort itself.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from core import ledger

# --------------------------------------------------------------------------
# Thresholds. Guardrails are code, not prompts.
# --------------------------------------------------------------------------

BASELINE_WINDOW_DAYS = 180      # how far back we look to learn a customer's habits
VALUE_WINDOW_DAYS = 180         # how far back we look to value a customer

# What counts as a regular: someone who shows up most days, not merely often.
REGULAR_MIN_VISIT_DAYS = 55     # distinct visit days inside the baseline window
REGULAR_MAX_MEDIAN_GAP = 3.0    # days between visits, typical case

# What counts as lapsed: silent for far longer than that customer's own rhythm.
LAPSE_MIN_DAYS = 14
LAPSE_GAP_MULTIPLE = 5.0

# Off peak gap: a slot running at or below this share of the same hour on other weekdays.
OFFPEAK_GAP_RATIO = 0.50
OFFPEAK_MIN_NORM_PER_DAY = 1.0  # ignore hours that are quiet everywhere
OFFPEAK_MIN_MONTHLY_RUPEES = 200.0

# Basket affinity gap: an anchor attaching add ons worse than the best anchor in the shop.
ANCHOR_MIN_TXNS = 200           # an anchor needs enough volume to compare
ANCHOR_SOLO_SHARE = 0.50        # an anchor is bought on its own most of the time
AFFINITY_MIN_GAP = 0.02
AFFINITY_MIN_MONTHLY_RUPEES = 200.0

# Softer signals are discounted before they enter the score. Lapsed value is money the
# shop already earned and is now losing, so it counts in full.
LAPSED_WEIGHT = 1.00
OFFPEAK_WEIGHT = 0.20
AFFINITY_WEIGHT = 0.20

# Worth a call only if the money at stake is both material in rupees and material to this
# shop. Both have to clear, which is what keeps the call volume near three percent.
CALL_THRESHOLD_MONTHLY_RUPEES = 1500.0
CALL_THRESHOLD_REVENUE_SHARE = 0.03


# --------------------------------------------------------------------------
# Signal 1: lapsed regulars
# --------------------------------------------------------------------------


def customer_profiles(days_by_customer: dict, today: date) -> dict:
    """Cadence and value for every customer, from visit history alone."""
    baseline_start = today - timedelta(days=BASELINE_WINDOW_DAYS)
    profiles = {}
    for customer_id, days in days_by_customer.items():
        visit_dates = [day for day, _, _ in days]
        recent = [day for day in visit_dates if day >= baseline_start]
        if not visit_dates:
            continue
        last_visit = visit_dates[-1]
        value_start = last_visit - timedelta(days=VALUE_WINDOW_DAYS - 1)
        profiles[customer_id] = {
            "customer_id": customer_id,
            "first_visit": visit_dates[0],
            "last_visit": last_visit,
            "days_since_last_visit": (today - last_visit).days,
            "visit_days_in_baseline": len(recent),
            "median_gap_days": ledger.median_gap_days(recent if len(recent) > 1 else visit_dates),
            "lifetime_visit_days": len(visit_dates),
            "monthly_value": ledger.spend_between(days, value_start, last_visit)
            / (VALUE_WINDOW_DAYS / 30.0),
            "visit_dates": visit_dates,
        }
    return profiles


def is_regular(profile: dict) -> bool:
    return (profile["visit_days_in_baseline"] >= REGULAR_MIN_VISIT_DAYS
            and profile["median_gap_days"] <= REGULAR_MAX_MEDIAN_GAP)


def lapse_threshold_days(profile: dict) -> float:
    """Silence long enough to mean something, measured against this customer's own rhythm."""
    return max(float(LAPSE_MIN_DAYS), LAPSE_GAP_MULTIPLE * profile["median_gap_days"])


def is_lapsed(profile: dict) -> bool:
    return profile["days_since_last_visit"] >= lapse_threshold_days(profile)


def lapsed_regulars(profiles: dict) -> list:
    found = []
    for profile in profiles.values():
        if is_regular(profile) and is_lapsed(profile):
            found.append(profile)
    found.sort(key=lambda p: p["monthly_value"], reverse=True)
    return found


# --------------------------------------------------------------------------
# Signal 2: off peak gaps
# --------------------------------------------------------------------------


def offpeak_gaps(counts: dict, occurrences: dict, avg_ticket: float) -> list:
    """Weekday and hour slots running well below the same hour on other weekdays."""
    hours = sorted({hour for _, hour in counts})
    per_day = {}
    for weekday in range(7):
        for hour in hours:
            occurred = occurrences.get(weekday, 0)
            per_day[(weekday, hour)] = counts.get((weekday, hour), 0) / occurred if occurred else 0.0

    flagged = {}
    for weekday in range(7):
        for hour in hours:
            others = [per_day[(other, hour)] for other in range(7) if other != weekday]
            norm = sum(others) / len(others)
            if norm < OFFPEAK_MIN_NORM_PER_DAY:
                continue
            observed = per_day[(weekday, hour)]
            if observed <= OFFPEAK_GAP_RATIO * norm:
                flagged.setdefault(weekday, []).append((hour, observed, norm))

    gaps = []
    for weekday, entries in flagged.items():
        entries.sort()
        run: list = []
        for entry in entries + [None]:
            if run and (entry is None or entry[0] != run[-1][0] + 1):
                gaps.append(_build_gap(weekday, run, avg_ticket))
                run = []
            if entry is not None:
                run.append(entry)
    gaps = [gap for gap in gaps if gap["monthly_upside"] >= OFFPEAK_MIN_MONTHLY_RUPEES]
    gaps.sort(key=lambda g: g["monthly_upside"], reverse=True)
    return gaps


def _build_gap(weekday: int, run: list, avg_ticket: float) -> dict:
    observed = sum(entry[1] for entry in run)
    expected = sum(entry[2] for entry in run)
    shortfall = max(0.0, expected - observed)
    # One occurrence of that weekday per week, so roughly 30/7 occurrences per month.
    monthly_upside = shortfall * avg_ticket * (30.0 / 7.0)
    return {
        "weekday": weekday,
        "weekday_name": ledger.WEEKDAY_NAMES[weekday],
        "hours": [entry[0] for entry in run],
        "observed_txns_per_occurrence": round(observed, 2),
        "expected_txns_per_occurrence": round(expected, 2),
        "shortfall_pct": round(100.0 * shortfall / expected, 1) if expected else 0.0,
        "monthly_upside": monthly_upside,
    }


# --------------------------------------------------------------------------
# Signal 3: basket affinity gaps
# --------------------------------------------------------------------------


def classify_items(basket_map: dict) -> tuple:
    """Splits the menu into anchors (bought on their own) and add ons, from the data."""
    solo = {}
    total = {}
    for items in basket_map.values():
        alone = len(items) == 1
        for item in items:
            total[item] = total.get(item, 0) + 1
            if alone:
                solo[item] = solo.get(item, 0) + 1
    anchors, addons = [], []
    for item, count in total.items():
        share = solo.get(item, 0) / count
        (anchors if share >= ANCHOR_SOLO_SHARE else addons).append(item)
    return sorted(anchors), sorted(addons), total


def affinity_gaps(basket_map: dict, prices: dict, window_days: int) -> list:
    """An anchor that attaches add ons worse than the best anchor in the same shop."""
    anchors, addons, total = classify_items(basket_map)
    if not addons:
        return []
    addon_set = set(addons)

    rates = {}
    for anchor in anchors:
        if total.get(anchor, 0) < ANCHOR_MIN_TXNS:
            continue
        with_anchor = [items for items in basket_map.values() if anchor in items]
        attached = sum(1 for items in with_anchor if items & addon_set)
        rates[anchor] = {
            "txns": len(with_anchor),
            "attached": attached,
            "attach_rate": attached / len(with_anchor) if with_anchor else 0.0,
        }
    if len(rates) < 2:
        return []

    benchmark_item = max(rates, key=lambda item: rates[item]["attach_rate"])
    benchmark = rates[benchmark_item]["attach_rate"]
    addon_price = (sum(prices.get(item, 0.0) for item in addons) / len(addons)) if addons else 0.0

    gaps = []
    for anchor, stats in rates.items():
        gap = benchmark - stats["attach_rate"]
        if anchor == benchmark_item or gap < AFFINITY_MIN_GAP:
            continue
        monthly_txns = stats["txns"] / (window_days / 30.0)
        monthly_upside = gap * monthly_txns * addon_price
        if monthly_upside < AFFINITY_MIN_MONTHLY_RUPEES:
            continue
        gaps.append({
            "anchor_item": anchor,
            "anchor_txns": stats["txns"],
            "attach_rate": round(stats["attach_rate"], 4),
            "benchmark_item": benchmark_item,
            "benchmark_attach_rate": round(benchmark, 4),
            "gap": round(gap, 4),
            "addon_avg_price": round(addon_price, 2),
            "monthly_upside": monthly_upside,
        })
    gaps.sort(key=lambda g: g["monthly_upside"], reverse=True)
    return gaps


# --------------------------------------------------------------------------
# The score
# --------------------------------------------------------------------------


def score(merchant_id: str, today: date | str | None = None,
          conn: sqlite3.Connection | None = None, db_path: str | None = None) -> dict:
    """Opportunity score for one merchant on one night. Pure arithmetic, no LLM."""
    owned = conn is None
    conn = conn or ledger.connect(db_path)
    try:
        if today is None:
            today = ledger.data_as_of(conn, merchant_id)
        elif isinstance(today, str):
            today = date.fromisoformat(today)

        info = ledger.merchant(conn, merchant_id)
        totals = ledger.window_totals(conn, merchant_id, today, BASELINE_WINDOW_DAYS)
        days_by_customer = ledger.customer_days(conn, merchant_id, today)
        profiles = customer_profiles(days_by_customer, today)

        lapsed = lapsed_regulars(profiles)
        value_at_risk = sum(profile["monthly_value"] for profile in lapsed)

        counts, occurrences = ledger.slot_counts(conn, merchant_id, today, BASELINE_WINDOW_DAYS)
        gaps = offpeak_gaps(counts, occurrences, totals["avg_ticket"])
        offpeak_upside = sum(gap["monthly_upside"] for gap in gaps)

        basket_map = ledger.baskets(conn, merchant_id, today, BASELINE_WINDOW_DAYS)
        prices = ledger.item_prices(conn, merchant_id, today, BASELINE_WINDOW_DAYS)
        affinity = affinity_gaps(basket_map, prices, BASELINE_WINDOW_DAYS)
        affinity_upside = sum(gap["monthly_upside"] for gap in affinity)

        opportunity = (value_at_risk * LAPSED_WEIGHT
                       + offpeak_upside * OFFPEAK_WEIGHT
                       + affinity_upside * AFFINITY_WEIGHT)
        monthly_revenue = totals["monthly_revenue"]
        revenue_share = opportunity / monthly_revenue if monthly_revenue else 0.0
        worth_a_call = (opportunity >= CALL_THRESHOLD_MONTHLY_RUPEES
                        and revenue_share >= CALL_THRESHOLD_REVENUE_SHARE)

        regulars = [p for p in profiles.values() if is_regular(p)]
        result = {
            "merchant_id": merchant_id,
            "merchant_name": info["name"],
            "today": today.isoformat(),
            "opportunity_score": round(opportunity, 2),
            "score_unit": "rupees_per_month_at_stake",
            "revenue_share": round(revenue_share, 4),
            "worth_a_call": worth_a_call,
            "decision": "call" if worth_a_call else "no_call",
            "thresholds": {
                "min_monthly_rupees": CALL_THRESHOLD_MONTHLY_RUPEES,
                "min_revenue_share": CALL_THRESHOLD_REVENUE_SHARE,
            },
            "merchant_baseline": {
                "window_days": BASELINE_WINDOW_DAYS,
                "monthly_revenue": round(monthly_revenue, 2),
                "monthly_transactions": round(totals["monthly_transactions"], 1),
                "avg_ticket": round(totals["avg_ticket"], 2),
                "active_customers": totals["active_customers"],
                "regular_customers": len(regulars),
            },
            "signals": {
                "lapsed_regulars": {
                    "count": len(lapsed),
                    "customer_ids": [p["customer_id"] for p in lapsed],
                    "value_at_risk_monthly": round(value_at_risk, 2),
                    "weighted_contribution": round(value_at_risk * LAPSED_WEIGHT, 2),
                    "method": ("a regular visits at least %d days in %d and typically waits at "
                               "most %.1f days, lapsed means silent for at least %d days or %.0f "
                               "times that customer's own gap"
                               % (REGULAR_MIN_VISIT_DAYS, BASELINE_WINDOW_DAYS,
                                  REGULAR_MAX_MEDIAN_GAP, LAPSE_MIN_DAYS, LAPSE_GAP_MULTIPLE)),
                    "detail": [
                        {
                            "customer_id": p["customer_id"],
                            "last_visit": p["last_visit"].isoformat(),
                            "days_since_last_visit": p["days_since_last_visit"],
                            "visit_days_in_baseline": p["visit_days_in_baseline"],
                            "median_gap_days": p["median_gap_days"],
                            "monthly_value": round(p["monthly_value"], 2),
                        }
                        for p in lapsed
                    ],
                },
                "offpeak_gaps": {
                    "count": len(gaps),
                    "monthly_upside": round(offpeak_upside, 2),
                    "weighted_contribution": round(offpeak_upside * OFFPEAK_WEIGHT, 2),
                    "detail": [dict(gap, monthly_upside=round(gap["monthly_upside"], 2))
                               for gap in gaps],
                },
                "affinity_gaps": {
                    "count": len(affinity),
                    "monthly_upside": round(affinity_upside, 2),
                    "weighted_contribution": round(affinity_upside * AFFINITY_WEIGHT, 2),
                    "detail": [dict(gap, monthly_upside=round(gap["monthly_upside"], 2))
                               for gap in affinity],
                },
            },
        }
        result["evidence"] = _evidence_lines(result)
        return result
    finally:
        if owned:
            conn.close()


def _evidence_lines(result: dict) -> list:
    """Plain sentences the dashboard and the voice brief can both use. No numbers invented."""
    lines = []
    lapsed = result["signals"]["lapsed_regulars"]
    if lapsed["count"]:
        gaps = [d["days_since_last_visit"] for d in lapsed["detail"]]
        lines.append(
            "%d regular customers have stopped coming, between %d and %d days of silence each, "
            "worth %.0f rupees a month between them."
            % (lapsed["count"], min(gaps), max(gaps), lapsed["value_at_risk_monthly"])
        )
    for gap in result["signals"]["offpeak_gaps"]["detail"]:
        lines.append(
            "%s %02d:00 to %02d:00 runs %.0f percent below the same hours on other days, "
            "about %.0f rupees a month."
            % (gap["weekday_name"], gap["hours"][0], gap["hours"][-1] + 1,
               gap["shortfall_pct"], gap["monthly_upside"])
        )
    for gap in result["signals"]["affinity_gaps"]["detail"]:
        lines.append(
            "%s orders add a snack %.1f percent of the time against %.1f percent on %s orders, "
            "about %.0f rupees a month."
            % (gap["anchor_item"], 100 * gap["attach_rate"], 100 * gap["benchmark_attach_rate"],
               gap["benchmark_item"], gap["monthly_upside"])
        )
    if not lines:
        lines.append("Nothing at this shop is far enough from its own norm to be worth a call.")
    return lines
