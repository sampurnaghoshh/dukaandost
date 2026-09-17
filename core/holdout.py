"""Random 85/15 treated and control assignment, plus frequency cap and quiet hour guardrails.

The holdout is not a formality. It is the answer to "how do you know it was you": a random
fifteen percent of the target segment is deliberately never messaged, and lift is measured
as treated conversion minus control conversion. Because the control group is never
contacted, the simulator prices the treated group only.

Two properties this module has to guarantee, and both are tested:

  deterministic  the same campaign id and seed always produce the same split, so a rerun,
                 a retry from n8n, or a second look from the dashboard all agree
  disjoint       a customer is in exactly one arm of a campaign, never both, never neither

Assignment is by a hash of the campaign id and customer id rather than by shuffling a list.
Hashing means the arm for a customer can be recomputed from scratch at any point without
replaying the original run, which is what makes a retry safe.
"""

from __future__ import annotations

import hashlib
import sqlite3

HOLDOUT_SHARE = 0.15
TREATED_SHARE = 1.0 - HOLDOUT_SHARE
DEFAULT_SEED = 20260919

# Guardrails that belong to dispatch rather than to the simulator. Enforced in code, not
# requested of a model. Wired into dispatch in Step 6 alongside the n8n orchestration.
MAX_MESSAGES_PER_CUSTOMER_PER_MONTH = 2
QUIET_HOURS = (21, 22, 23, 0, 1, 2, 3, 4, 5, 6)


def split_counts(segment_size: int) -> tuple:
    """How many of a segment are treated and how many are held back. Counting only."""
    holdout = round(segment_size * HOLDOUT_SHARE)
    return segment_size - holdout, holdout


def _score(campaign_id: str, customer_id: str, seed: int) -> float:
    """A stable number in [0, 1) for this customer in this campaign.

    Different campaigns give the same customer a different score, so a customer held back
    once is not held back forever.
    """
    digest = hashlib.sha256(
        ("%s|%s|%d" % (campaign_id, customer_id, seed)).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(16 ** 12)


def assign(campaign_id: str, customer_ids: list, seed: int = DEFAULT_SEED) -> dict:
    """Splits a segment into treated and control. Deterministic, disjoint, exhaustive.

    The lowest scoring customers are held back rather than everyone under a threshold, so
    the control group is exactly the intended size however the hashes happen to fall.
    """
    unique = sorted(set(customer_ids))
    treated_n, holdout_n = split_counts(len(unique))

    ranked = sorted(unique, key=lambda customer_id: _score(campaign_id, customer_id, seed))
    control = sorted(ranked[:holdout_n])
    treated = sorted(ranked[holdout_n:])

    return {
        "campaign_id": campaign_id,
        "seed": seed,
        "segment_size": len(unique),
        "treated": treated,
        "control": control,
        "treated_count": len(treated),
        "holdout_count": len(control),
        "holdout_share": HOLDOUT_SHARE,
        "method": ("sha256 of campaign id, customer id and seed, lowest %d held back of %d"
                   % (holdout_n, len(unique))),
    }


def arm_of(assignment: dict, customer_id: str) -> str:
    if customer_id in set(assignment["treated"]):
        return "treated"
    if customer_id in set(assignment["control"]):
        return "control"
    return "unassigned"


def validate(assignment: dict) -> list:
    """Every way an assignment could be wrong. Empty list means it is sound."""
    problems = []
    treated = set(assignment["treated"])
    control = set(assignment["control"])

    overlap = treated & control
    if overlap:
        problems.append("%d customers are in both arms: %s"
                        % (len(overlap), ", ".join(sorted(overlap)[:5])))
    if len(treated) + len(control) != assignment["segment_size"]:
        problems.append("arms hold %d customers but the segment has %d"
                        % (len(treated) + len(control), assignment["segment_size"]))
    expected_treated, expected_holdout = split_counts(assignment["segment_size"])
    if len(treated) != expected_treated or len(control) != expected_holdout:
        problems.append("split is %d and %d, expected %d and %d"
                        % (len(treated), len(control), expected_treated, expected_holdout))
    return problems


def assign_and_save(conn: sqlite3.Connection, campaign_id: str, customer_ids: list,
                    seed: int = DEFAULT_SEED) -> dict:
    """Assigns, checks the split is sound, then persists it. Refuses to save a broken one."""
    from memory import store

    assignment = assign(campaign_id, customer_ids, seed)
    problems = validate(assignment)
    if problems:
        raise ValueError("refusing to save a broken assignment: %s" % "; ".join(problems))
    store.save_assignments(conn, campaign_id, assignment)
    return assignment
