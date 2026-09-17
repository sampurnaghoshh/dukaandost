"""The simulator has to refuse actions that lose money, whoever proposed them.

Guardrails are code, not prompts. These tests hold that line: a candidate the LLM is
perfectly capable of inventing in Step 3 gets refused here on arithmetic alone.
"""

from __future__ import annotations

import copy

import pytest

from core import fixtures, simulate

MERCHANT_ID = "tea_stall_01"


@pytest.fixture(scope="session")
def ranking(conn, triage_result):
    return simulate.rank(MERCHANT_ID, fixtures.PLACEHOLDER_CANDIDATES, triage_result, conn=conn)


# ---------------------------------------------------------------- the profit guardrail


def negative_profit_candidate():
    """Generous enough to lose money, shallow enough to clear the margin floor.

    This isolates the profit guardrail: if the margin floor were the only thing stopping
    bad offers, this one would sail through.
    """
    return {
        "candidate_id": "attach_snack_with_chai_25",
        "action_type": "attach_upsell",
        "title": "Buy the attach rate with a quarter off every snack",
        "target_segment": {"kind": "anchor_buyers", "anchor_item": "chai"},
        "offer": {"type": "percent_discount", "value": 25, "applies_to": "snack",
                  "validity_days": 28},
        "message_template": "Chai ke saath snack pe {discount_pct}% chhoot.",
    }


def test_simulator_rejects_a_negative_profit_candidate(conn, triage_result):
    candidate = negative_profit_candidate()
    result = simulate.rank(MERCHANT_ID, [candidate], triage_result, conn=conn)

    assert result["ranked"] == [], "a money losing candidate was accepted"
    assert len(result["rejected"]) == 1
    refused = result["rejected"][0]
    assert refused["candidate_id"] == candidate["candidate_id"]
    assert refused["estimates"]["expected_profit"] < 0
    assert any("negative expected profit" in reason for reason in refused["rejections"])
    assert result["recommended"] is None


def test_the_profit_guardrail_fires_on_its_own(conn, triage_result):
    """The refusal above must not be the margin floor wearing a different hat."""
    candidate = negative_profit_candidate()
    result = simulate.rank(MERCHANT_ID, [candidate], triage_result, conn=conn)
    reasons = result["rejected"][0]["rejections"]
    assert not any("margin floor" in reason for reason in reasons), (
        "this candidate was meant to clear the margin floor, so the profit check is untested")
    assert simulate.effective_margin(0.25) >= simulate.MARGIN_FLOOR


def test_check_guardrails_refuses_zero_profit():
    """Break even is not a reason to spend a merchant's money."""
    candidate = {"offer": {"type": "percent_discount", "value": 5}}
    estimates = {"estimable": True, "expected_profit": 0.0, "horizon_days": 30}
    reasons = simulate.check_guardrails(candidate, estimates)
    assert any("negative expected profit" in reason for reason in reasons)


# ---------------------------------------------------------------- the margin floor


def test_every_offer_level_code_can_choose_already_clears_the_margin_floor():
    """Since Step 4 the depth is code's to pick, so the menu itself has to be legal.

    This replaces an earlier test that asserted a candidate asking for fifty percent off was
    refused on the margin floor. No candidate can ask for a depth any more, so that test was
    checking a route that no longer exists. The guarantee that matters now is stronger: not
    one of the depths the simulator is willing to offer breaches the floor.
    """
    for level, percent in simulate.OFFER_LEVEL_DISCOUNT_PCT.items():
        margin = simulate.effective_margin(percent / 100.0)
        assert margin >= simulate.MARGIN_FLOOR, (
            "level %s at %d percent leaves %.2f margin, under the %.2f floor"
            % (level, percent, margin, simulate.MARGIN_FLOOR))


def test_the_margin_floor_still_refuses_a_level_that_is_too_deep(conn, triage_result,
                                                                 monkeypatch):
    """Put an illegal depth on the menu and the grid must refuse that level and keep going."""
    monkeypatch.setattr(simulate, "OFFER_LEVEL_DISCOUNT_PCT",
                        {"LOW": 10, "MEDIUM": 20, "HIGH": 50})
    candidate = dict(fixtures.PLACEHOLDER_CANDIDATES[0])
    result = simulate.rank(MERCHANT_ID, [candidate], triage_result, conn=conn)

    item = (result["ranked"] + result["rejected"])[0]
    high = [row for row in item["level_grid"] if row["offer_level"] == "HIGH"][0]
    assert not high["survives"]
    assert any("margin floor" in reason for reason in high["rejections"])
    # The candidate still runs, just at a legal depth.
    assert item["accepted"] is True
    assert item["offer_level"] in ("LOW", "MEDIUM")


def test_the_deep_discount_fixture_now_survives_at_a_shallower_level(ranking):
    """The fifty percent placeholder is no longer expressible, so it runs at a sane depth."""
    deep = [item for item in ranking["ranked"] + ranking["rejected"]
            if item["candidate_id"] == "winback_lapsed_chai_50"]
    assert deep, "the fixture went missing"
    assert deep[0]["offer"]["value"] in simulate.OFFER_LEVEL_DISCOUNT_PCT.values()
    assert deep[0]["offer"]["value"] < 50


def test_effective_margin_falls_faster_than_the_discount():
    assert simulate.effective_margin(0.0) == pytest.approx(simulate.GROSS_MARGIN_RATE)
    assert simulate.effective_margin(0.50) < simulate.MARGIN_FLOOR
    assert simulate.effective_margin(0.10) > simulate.effective_margin(0.30)


# ---------------------------------------------------------------- the discount budget


def test_monthly_discount_budget_is_enforced(conn, triage_result):
    """With the month's budget already spent, nothing may be dispatched."""
    result = simulate.rank(
        MERCHANT_ID, fixtures.PLACEHOLDER_CANDIDATES, triage_result, conn=conn,
        discount_spent_this_month=simulate.MONTHLY_DISCOUNT_BUDGET_RUPEES)
    assert result["ranked"] == []
    assert any(any("discount budget" in reason for reason in item["rejections"])
               for item in result["rejected"])


def test_accepted_discounts_fit_inside_the_budget(ranking):
    spent = sum(item["estimates"]["discount_cost"] for item in ranking["ranked"])
    assert spent <= simulate.MONTHLY_DISCOUNT_BUDGET_RUPEES


# ---------------------------------------------------------------- ranking and arithmetic


def test_the_winback_is_the_recommended_action(ranking):
    assert ranking["recommended"] is not None
    assert ranking["recommended"]["action_type"] == "lapsed_winback"
    assert ranking["recommended"]["estimates"]["expected_profit"] > 0


def test_ranking_is_ordered_by_expected_profit(ranking):
    profits = [item["estimates"]["expected_profit"] for item in ranking["ranked"]]
    assert profits == sorted(profits, reverse=True)
    assert [item["rank"] for item in ranking["ranked"]] == list(range(1, len(profits) + 1))


def test_only_incremental_revenue_counts_but_every_discount_is_paid(ranking):
    """The control group would have converted anyway, so its revenue is not ours to claim."""
    for item in ranking["ranked"]:
        estimates = item["estimates"]
        assert estimates["incremental_responses"] < estimates["expected_responses"]
        assert estimates["expected_profit"] == pytest.approx(
            estimates["incremental_gross_profit"] - estimates["discount_cost"]
            - estimates["message_cost"], abs=0.02)


def test_return_rate_comes_from_this_shop_shrunk_toward_the_prior(ranking):
    history = ranking["history"]
    assert history["episodes"] > 0, "no lapse episodes were mined from the ledger"
    assert 0.0 < history["rate"] < 1.0
    low, high = sorted([history["observed_rate"], history["prior_rate"]])
    assert low <= history["rate"] <= high, "shrinkage landed outside the two inputs"


def test_simulator_refuses_an_action_type_it_cannot_price(conn, triage_result):
    """An open action space means the LLM will eventually invent something new."""
    candidate = copy.deepcopy(fixtures.PLACEHOLDER_CANDIDATES[0])
    candidate["action_type"] = "hire_a_dancing_mascot"
    result = simulate.rank(MERCHANT_ID, [candidate], triage_result, conn=conn)
    assert result["ranked"] == []
    assert any("refuses to guess" in reason for reason in result["rejected"][0]["rejections"])


def test_no_candidate_carries_a_number_of_its_own():
    """Candidates say what to do and how to say it. Numbers come from the simulator."""
    for candidate in fixtures.PLACEHOLDER_CANDIDATES:
        assert set(candidate["offer"]) <= {"type", "value", "applies_to", "validity_days"}
        assert "expected_profit" not in candidate
        assert "{" in candidate["message_template"], (
            "merchant facing copy must carry placeholders, not baked in numbers")
