"""Triage has to rediscover the lapsed cohort from transactions alone.

The generator tuned the cohort to a target it never wrote into the ledger, and triage has
never seen data/ground_truth.json. If these pass, the arithmetic layer is finding a real
pattern rather than reading the answer key.
"""

from __future__ import annotations

import builtins

from core import triage

MERCHANT_ID = "tea_stall_01"

# The headline number on stage. Triage is not told it, it has to land near it.
DEMO_VALUE_AT_RISK = 9400.0
VALUE_TOLERANCE = 0.15


def test_triage_finds_exactly_the_ground_truth_cohort(triage_result, ground_truth):
    found = sorted(triage_result["signals"]["lapsed_regulars"]["customer_ids"])
    expected = sorted(ground_truth["lapsed_customer_ids"])
    assert found == expected


def test_triage_finds_twelve_of_them(triage_result):
    assert triage_result["signals"]["lapsed_regulars"]["count"] == 12


def test_value_at_risk_is_within_fifteen_percent_of_the_demo_number(triage_result):
    value = triage_result["signals"]["lapsed_regulars"]["value_at_risk_monthly"]
    error = abs(value - DEMO_VALUE_AT_RISK) / DEMO_VALUE_AT_RISK
    assert error <= VALUE_TOLERANCE, (
        "value at risk %.2f is %.1f percent off the %.0f the demo says out loud"
        % (value, 100 * error, DEMO_VALUE_AT_RISK))


def test_every_lapsed_customer_is_silent_and_was_a_regular(triage_result):
    for row in triage_result["signals"]["lapsed_regulars"]["detail"]:
        assert row["days_since_last_visit"] >= triage.LAPSE_MIN_DAYS
        assert row["visit_days_in_baseline"] >= triage.REGULAR_MIN_VISIT_DAYS
        assert row["median_gap_days"] <= triage.REGULAR_MAX_MEDIAN_GAP


def test_triage_does_not_read_the_ground_truth_file(conn, monkeypatch):
    """The answer key must be unreachable from the nightly loop.

    Booby traps open() rather than grepping the source, so it catches a read however it is
    spelled. Triage has to produce the same cohort with the file effectively missing.
    """
    real_open = builtins.open
    opened: list = []

    def guarded(file, *args, **kwargs):
        if "ground_truth" in str(file):
            opened.append(str(file))
            raise AssertionError("triage opened the answer key: %s" % file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded)
    result = triage.score(MERCHANT_ID, conn=conn)
    assert opened == []
    assert result["signals"]["lapsed_regulars"]["count"] == 12


def test_the_dead_tuesday_afternoon_is_found(triage_result):
    gaps = triage_result["signals"]["offpeak_gaps"]["detail"]
    assert gaps, "triage found no off peak gap at all"
    tuesday = [gap for gap in gaps if gap["weekday"] == 1]
    assert tuesday, "the dead Tuesday afternoon was not flagged"
    assert set(tuesday[0]["hours"]) & {14, 15, 16}
    assert tuesday[0]["shortfall_pct"] >= 50.0


def test_the_chai_snack_affinity_gap_is_found(triage_result):
    gaps = triage_result["signals"]["affinity_gaps"]["detail"]
    assert gaps, "triage found no affinity gap"
    chai = [gap for gap in gaps if gap["anchor_item"] == "chai"]
    assert chai, "chai was not flagged as under attaching"
    assert chai[0]["attach_rate"] < chai[0]["benchmark_attach_rate"]


def test_tonight_is_worth_a_call(triage_result):
    assert triage_result["worth_a_call"] is True
    assert triage_result["decision"] == "call"
    assert triage_result["opportunity_score"] >= triage_result["thresholds"]["min_monthly_rupees"]


def test_the_agent_stays_silent_before_the_cohort_lapses(conn, ground_truth):
    """A week before the cohort goes quiet there is nothing worth phoning about."""
    quiet_night = triage.score(MERCHANT_ID, "2026-08-20", conn=conn)
    assert quiet_night["signals"]["lapsed_regulars"]["count"] == 0
    assert quiet_night["worth_a_call"] is False
    assert quiet_night["decision"] == "no_call"
