"""PLACEHOLDER candidate actions, hand written to unblock the simulator.

  ####################################################################
  #  THESE ARE PLACEHOLDERS. In Step 3 core/generate.py replaces them #
  #  with candidates the LLM writes openly for this shop.            #
  #  Nothing here may be treated as a fixed menu of campaign types.  #
  ####################################################################

They exist so simulate.py can be built and tested before the LLM is wired in. The shape
of these dicts is the contract the LLM has to produce: an action type, a target segment
and an offer, and merchant facing copy carrying placeholders only. No candidate, written
by hand or by a model, ever contains a rupee figure, a rate or a percentage of lift. Every
number in this pipeline comes from simulate.py.
"""

from __future__ import annotations

PLACEHOLDER_CANDIDATES = [
    {
        "candidate_id": "winback_lapsed_chai_10",
        "action_type": "lapsed_winback",
        "title": "Win back the regulars who stopped coming",
        "rationale": "Customers with a daily habit went silent together. A small, familiar "
                     "offer on the thing they always bought is the cheapest way to restart "
                     "the habit.",
        "target_segment": {"kind": "lapsed_regulars"},
        "offer": {"type": "percent_discount", "value": 10, "applies_to": "chai",
                  "validity_days": 7},
        "message_template": "Namaste! Aapko {shop_name} pe {days_absent} din se dekha nahi. "
                            "Aapke liye chai pe {discount_pct}% chhoot, agle "
                            "{validity_days} din tak.",
    },
    {
        "candidate_id": "winback_lapsed_chai_50",
        "action_type": "lapsed_winback",
        "title": "Win back the same regulars with a deep discount",
        "rationale": "Same cohort, far bigger offer, on the theory that a large discount "
                     "buys back more of them.",
        "target_segment": {"kind": "lapsed_regulars"},
        "offer": {"type": "percent_discount", "value": 50, "applies_to": "chai",
                  "validity_days": 7},
        "message_template": "Wapas aaiye! Chai pe {discount_pct}% chhoot sirf "
                            "{validity_days} din ke liye.",
    },
    {
        "candidate_id": "offpeak_tuesday_afternoon_15",
        "action_type": "offpeak_fill",
        "title": "Fill the dead Tuesday afternoon",
        "rationale": "One weekday slot runs far below the same hours on every other day. "
                     "Nudging afternoon customers toward that window costs nothing in lost "
                     "peak trade.",
        "target_segment": {"kind": "offpeak_slot", "weekday": 1, "hours": [14, 15, 16]},
        "offer": {"type": "percent_discount", "value": 15, "applies_to": "chai",
                  "validity_days": 28},
        "message_template": "{weekday_name} dopahar {hour_from} se {hour_to} baje tak chai pe "
                            "{discount_pct}% chhoot. Aaram se aaiye, bheed nahi hogi.",
    },
    {
        "candidate_id": "attach_snack_with_chai_10",
        "action_type": "attach_upsell",
        "title": "Get a snack onto the chai order",
        "rationale": "Chai is the shop's anchor but it travels alone far more often than "
                     "the other drink does. Closing part of that gap is pure added basket.",
        "target_segment": {"kind": "anchor_buyers", "anchor_item": "chai"},
        "offer": {"type": "percent_discount", "value": 10, "applies_to": "snack",
                  "validity_days": 28},
        "message_template": "Chai ke saath {addon_name} lijiye, {discount_pct}% chhoot. "
                            "{shop_name} pe {validity_days} din tak.",
    },
]


# The call script the agent falls back to if the LLM is unreachable or its script keeps
# tripping the number guard. Same placeholders, same contract: code fills every figure.
FALLBACK_CALL_SCRIPT = (
    "नमस्ते {merchant_name} भाई। "
    "आपके {lapsed_count} रेगुलर "
    "ग्राहक काफी दिन "
    "से नहीं आए। महीने "
    "का लगभग {value_at_risk} का "
    "नुकसान हो रहा है। "
    "मैं {treated_count} लोगों को "
    "{offer} भेज दूं, और {holdout_count} "
    "लोगों को जानबूझकर "
    "छोड़ दूं ताकि पता "
    "चले कि ऑफर से फर्क "
    "पड़ा या नहीं। "
    "भेज दूं?"
)
