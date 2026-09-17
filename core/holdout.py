"""Random 85/15 treated and control assignment, plus frequency cap and quiet hour guardrails.

Step 3 defines the split constant only. The assignment logic, the frequency caps and the
quiet hours land in Step 5, alongside measurement.

The holdout is not a formality. It is the answer to "how do you know it was you": a random
fifteen percent of the target segment is deliberately left alone, and lift is measured as
treated conversion minus control conversion. Because the control group is never messaged,
the simulator prices the treated group only. Anything else would be charging the merchant
for people it decided not to contact.
"""

from __future__ import annotations

HOLDOUT_SHARE = 0.15
TREATED_SHARE = 1.0 - HOLDOUT_SHARE


def split_counts(segment_size: int) -> tuple:
    """How many of a segment are treated and how many are held back. Counting only."""
    holdout = round(segment_size * HOLDOUT_SHARE)
    return segment_size - holdout, holdout
