"""The dial() interface with the Brief and Decision contracts shared by both voice backends.

    def dial(merchant_id: str, brief: Brief) -> Decision

One interface, two implementations, one config flag. SarvamTelephony places a real outbound
call. LocalSoundbox speaks the same script through Sarvam TTS in a browser tab and listens
on the microphone. Same Brief in, same Decision out, so triage, the simulator, the holdout,
dispatch and measurement cannot tell which one ran. That is the demo day insurance policy,
and it is why this file contains the contract and no implementation.

Step 3 defines the models. The two backends land in Step 4.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BriefNumbers(BaseModel):
    """Every figure the agent may say out loud, all of it from arithmetic.

    The language model wrote the sentence around these. It did not produce a single one of
    them, and nothing here may ever be populated from model output.
    """

    lapsed_count: int
    value_at_risk_monthly: float
    discount_pct: float
    offer_applies_to: str
    segment_size: int
    treated_count: int
    holdout_count: int
    holdout_share: float
    baseline_response_rate: float
    offer_uplift: float
    incremental_responses: float
    incremental_revenue: float
    discount_cost: float
    expected_profit: float
    horizon_days: int


class Brief(BaseModel):
    """What the agent takes into the call. Everything is decided before the phone rings."""

    merchant_id: str
    merchant_name: str
    today: str
    language: str = "hi-IN"

    candidate_id: str
    action_type: str
    title: str
    rationale: str

    script: str                      # the filled Hindi script, ready to speak
    script_template: str             # the same script before code filled the placeholders
    script_source: Literal["llm", "fixture"] = "llm"
    customer_message_template: str = ""

    numbers: BriefNumbers
    evidence: list = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)


class Decision(BaseModel):
    """What came back off the call.

    outcome is three way on purpose. A merchant who says no and a merchant who never picked
    up are different facts, handled by different branches of the n8n workflow, and collapsing
    them into one boolean would throw away the more useful of the two.
    """

    approved: bool
    outcome: Literal["approved", "declined", "no_answer", "error"] = "no_answer"
    modifications: dict = Field(default_factory=dict)
    transcript: str = ""

    merchant_id: str = ""
    candidate_id: str = ""
    call_id: str = ""
    duration_seconds: float = 0.0
    channel: Literal["sarvam_telephony", "local_soundbox", "none"] = "none"
    decided_at: datetime = Field(default_factory=datetime.now)


def dial(merchant_id: str, brief: Brief) -> Decision:
    """Place the approval call and return what the merchant decided.

    Not implemented in Step 3. voice/sarvam_telephony.py and voice/local_soundbox.py both
    implement this signature in Step 4, and a config flag picks between them.
    """
    raise NotImplementedError(
        "no voice backend yet. Step 4 adds SarvamTelephony and LocalSoundbox, both "
        "satisfying this signature.")
