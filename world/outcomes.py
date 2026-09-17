"""THE SIMULATED WORLD. What would have happened, so measurement has something to measure.

  ####################################################################
  #  THIS IS NOT PART OF THE AGENT.                                  #
  #                                                                  #
  #  core/triage.py, core/generate.py and core/simulate.py must      #
  #  never import this module. It holds the answers. An agent that   #
  #  can read the answer key is not predicting anything, and the     #
  #  learning curve it draws would be a lie.                         #
  #                                                                  #
  #  A test asserts the agent modules do not import it.              #
  ####################################################################

On demo day this stands where reality would stand. A returning customer walks back into the
shop, or does not, and 72 hours later the transaction log says which. Until the product has
real merchants, this module rolls that dice.

The hidden truth below is deliberately set well above what the agent believes at the start.
That gap is the whole point of the learning curve: campaign one predicts with a prior that
is wrong in a known direction, the holdout measures how wrong, and campaign six predicts
with a number the merchant's own customers taught it.
"""

from __future__ import annotations

import hashlib

# --------------------------------------------------------------------------
# THE HIDDEN TRUTH. The agent never sees these.
#
# core/simulate.py starts from PRIOR_OFFER_UPLIFT = LOW 0.08, MEDIUM 0.14, HIGH 0.20.
# Every number here sits well above its prior, so the agent begins by underestimating what
# a winback is worth and has to be taught otherwise by measurement.
# --------------------------------------------------------------------------

TRUE_OFFER_UPLIFT = {"LOW": 0.18, "MEDIUM": 0.30, "HIGH": 0.38}

# What a lapsed regular does with no contact at all. The agent estimates this from the
# ledger and lands near 0.05, which is close, because it is the one quantity it can
# actually observe without running an experiment.
TRUE_BASELINE_RETURN_RATE = 0.06

# A returner does not instantly resume their old habit.
TRUE_RECOVERY_INTENSITY = 0.65

MEASUREMENT_HOURS = 72


def _draw(campaign_id: str, customer_id: str, salt: str) -> float:
    """A stable uniform draw. The same customer in the same campaign always does the same
    thing, so a rerun of the measurement gives the same answer."""
    digest = hashlib.sha256(
        ("%s|%s|%s" % (campaign_id, customer_id, salt)).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(16 ** 12)


def simulate_outcomes(campaign_id: str, assignment: dict, offer_level: str,
                      monthly_value_per_customer: float,
                      horizon_days: int = 30) -> list:
    """Rolls what each customer actually did in the measurement window.

    The control group returns at the true baseline, because nobody contacted them. The
    treated group returns at baseline plus the true uplift for the offer level they were
    sent. That difference is precisely what the holdout is designed to recover, and
    core/measure.py recovers it by subtraction without ever reading this file.
    """
    uplift = TRUE_OFFER_UPLIFT.get(str(offer_level).upper(), 0.0)
    spend = monthly_value_per_customer * (horizon_days / 30.0) * TRUE_RECOVERY_INTENSITY

    rows = []
    for arm, members in (("treated", assignment["treated"]),
                         ("control", assignment["control"])):
        probability = TRUE_BASELINE_RETURN_RATE + (uplift if arm == "treated" else 0.0)
        for customer_id in members:
            returned = _draw(campaign_id, customer_id, "return") < probability
            # A returner's spend wobbles either side of their old average.
            wobble = 0.7 + 0.6 * _draw(campaign_id, customer_id, "spend")
            rows.append({
                "customer_id": customer_id,
                "arm": arm,
                "returned": bool(returned),
                "revenue": round(spend * wobble, 2) if returned else 0.0,
            })
    return rows


def truth_for(offer_level: str) -> float:
    """Only for reporting how close the agent got. Never used to make a decision."""
    return TRUE_OFFER_UPLIFT.get(str(offer_level).upper(), 0.0)
