"""The judge dashboard: one page, one endpoint, no build step, fully offline.

The thing that must not happen on stage is a panel throwing because a campaign has not run
yet. Most of these tests are about the empty state.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from core import simulate

MERCHANT_ID = "tea_stall_01"

# Every key the page reads out of the payload, panel by panel.
TOP_LEVEL = {"generated_at", "llm_mode", "merchant", "tonight", "reasoning", "call",
             "campaign", "learning"}
MERCHANT_KEYS = {"merchant_id", "name", "today", "baseline"}
TONIGHT_KEYS = {"decision", "worth_a_call", "opportunity_score", "revenue_share",
                "thresholds", "signals", "evidence", "silent_reason"}
REASONING_KEYS = {"candidate_source", "offer_levels", "note", "dropped", "grid",
                  "recommended_id"}
LEARNING_KEYS = {"campaigns", "series", "scale", "hidden_truth", "priors", "headline"}


@pytest.fixture
def fresh_client(no_network, tmp_path, monkeypatch):
    """A client with empty campaign memory, so every panel sees the nothing yet case."""
    from api import main
    monkeypatch.setattr(main.run_night, "LOG_PATH", str(tmp_path / "decisions.jsonl"))
    monkeypatch.setenv("DUKAAN_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(main, "LEARNING_EXPORT_PATH", str(tmp_path / "learning.json"))
    main._ANALYSIS_CACHE.clear()
    main.local_soundbox.PENDING.clear()
    return TestClient(main.app)


# ---------------------------------------------------------------- the page


def test_the_page_serves_from_one_url(fresh_client):
    for path in ("/", "/dashboard"):
        response = fresh_client.get(path)
        assert response.status_code == 200
        assert "Dukaan Dost" in response.text


def test_the_page_needs_no_build_step_and_no_cdn(fresh_client):
    """It has to open on venue wifi with nothing installed."""
    body = fresh_client.get("/dashboard").text
    assert "<script" in body and "</script>" in body
    for forbidden in ("http://", "https://", "cdn.", "react", "import ", "require("):
        assert forbidden not in body.lower(), "the page reaches for %r" % forbidden


def test_the_page_polls_the_single_state_endpoint(fresh_client):
    body = fresh_client.get("/dashboard").text
    assert "/dashboard/state" in body
    assert "setInterval(poll, 2000)" in body


def test_the_chart_is_inline_svg_not_a_library(fresh_client):
    body = fresh_client.get("/dashboard").text
    assert "<svg" in body
    assert "chart.js" not in body.lower() and "d3" not in body.lower()


# ---------------------------------------------------------------- the state


def test_state_returns_200_offline_with_every_key_the_page_reads(fresh_client):
    response = fresh_client.get("/dashboard/state")
    assert response.status_code == 200
    state = response.json()

    assert TOP_LEVEL <= set(state)
    assert MERCHANT_KEYS <= set(state["merchant"])
    assert TONIGHT_KEYS <= set(state["tonight"])
    assert REASONING_KEYS <= set(state["reasoning"])
    assert LEARNING_KEYS <= set(state["learning"])
    assert state["llm_mode"] == "replay"


def test_no_panel_throws_before_any_campaign_has_run(fresh_client):
    """Panels three, four, five and six all have to render an empty state."""
    state = fresh_client.get("/dashboard/state").json()
    assert state["call"] is None
    assert state["campaign"] is None
    assert state["learning"]["campaigns"] == 0
    assert state["learning"]["series"] == []
    assert state["learning"]["hidden_truth"] is None
    assert state["learning"]["headline"] is None
    assert state["learning"]["scale"]["source"] == "PRIOR"


def test_tonight_reads_as_sentences_not_json(fresh_client):
    tonight = fresh_client.get("/dashboard/state").json()["tonight"]
    assert tonight["worth_a_call"] is True
    assert tonight["decision"] == "call"
    assert tonight["silent_reason"] is None
    kinds = {row["kind"] for row in tonight["signals"]}
    assert kinds == {"lapsed regulars", "off peak gap", "basket affinity gap"}
    for row in tonight["signals"]:
        assert row["sentence"] and not row["sentence"].startswith("{")
        assert row["monthly_value"] > 0


def test_the_silent_night_is_explained_rather_than_blank(fresh_client, monkeypatch):
    """Triage staying quiet is a feature, so the payload has to say so in words."""
    from api import main
    main._ANALYSIS_CACHE.clear()
    quiet = main._tonight({
        "decision": "no_call", "worth_a_call": False, "opportunity_score": 419.0,
        "revenue_share": 0.012, "thresholds": {"min_monthly_rupees": 1500.0,
                                               "min_revenue_share": 0.03},
        "evidence": [],
        "signals": {"lapsed_regulars": {"count": 0, "detail": [], "value_at_risk_monthly": 0},
                    "offpeak_gaps": {"detail": []}, "affinity_gaps": {"detail": []}},
    })
    assert quiet["worth_a_call"] is False
    assert quiet["silent_reason"]
    assert "stays silent" in quiet["silent_reason"]


def test_the_grid_carries_the_winback_row_the_demo_talks_about(fresh_client):
    grid = fresh_client.get("/dashboard/state").json()["reasoning"]["grid"]
    assert grid
    winbacks = [row for row in grid if row["action_type"] == "lapsed_winback"]
    assert winbacks, "the winback went missing from the grid"

    row = winbacks[0]
    assert row["chosen_level"] == "MEDIUM"
    assert row["accepted"] is True
    assert row["levels"]["LOW"]["expected_profit"] == pytest.approx(148.03, abs=0.01)
    assert row["levels"]["MEDIUM"]["expected_profit"] == pytest.approx(186.62, abs=0.01)
    assert row["levels"]["HIGH"]["expected_profit"] == pytest.approx(107.02, abs=0.01)
    assert row["segment"] == "regulars who stopped coming"


def test_refused_candidates_keep_their_reason(fresh_client):
    grid = fresh_client.get("/dashboard/state").json()["reasoning"]["grid"]
    refused = [row for row in grid if not row["accepted"]]
    assert refused, "nothing was refused, so the struck through row cannot be shown"
    for row in refused:
        assert row["rejections"], "a refused candidate with no reason is useless on stage"
        assert row["chosen_level"] is None
        assert all(not row["levels"][level]["survives"] for level in ("LOW", "MEDIUM", "HIGH"))


def test_every_grid_row_has_all_three_levels(fresh_client):
    grid = fresh_client.get("/dashboard/state").json()["reasoning"]["grid"]
    for row in grid:
        assert set(row["levels"]) == set(simulate.OFFER_LEVELS)
        for level in simulate.OFFER_LEVELS:
            assert "expected_profit" in row["levels"][level]
            assert "survives" in row["levels"][level]


def test_the_note_about_who_decides_what_is_present(fresh_client):
    reasoning = fresh_client.get("/dashboard/state").json()["reasoning"]
    assert reasoning["note"] == "the model proposes who and what, code prices how deep"
    assert reasoning["offer_levels"] == simulate.OFFER_LEVEL_DISCOUNT_PCT


# ---------------------------------------------------------------- with a campaign


@pytest.fixture
def after_campaign(fresh_client):
    """Drives a real call, approval, dispatch and measurement through the API."""
    call = fresh_client.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    fresh_client.post("/soundbox/button", json={"call_id": call["call_id"], "answer": "haan"})
    launched = fresh_client.post("/campaign/launch", json={
        "merchant_id": MERCHANT_ID, "call_id": call["call_id"], "approved": True}).json()
    fresh_client.post("/measure", json={"campaign_id": launched["campaign_id"],
                                        "merchant_id": MERCHANT_ID})
    return fresh_client


def test_the_call_panel_shows_the_hindi_script_and_the_reply(after_campaign):
    call = after_campaign.get("/dashboard/state").json()["call"]
    assert call is not None
    assert call["settled"] is True
    assert call["approved"] is True
    assert call["transcript"]
    assert "नमस्ते" in call["script"], "the script is not Hindi"
    assert call["audio_url"], "cached audio should be replayable"


def test_dispatch_shows_the_holdout_rather_than_describing_it(after_campaign):
    campaign = after_campaign.get("/dashboard/state").json()["campaign"]
    assert len(campaign["treated"]) == 10
    assert len(campaign["control"]) == 2

    treated_ids = {row["customer_id"] for row in campaign["treated"]}
    control_ids = {row["customer_id"] for row in campaign["control"]}
    assert treated_ids & control_ids == set()

    assert all(row["body"] for row in campaign["treated"])
    assert all(row["body"] is None for row in campaign["control"]), (
        "a control customer with a message would break the experiment")
    assert campaign["production_channel"] == "whatsapp_business_api"


def test_measurement_is_the_subtraction_and_states_its_horizon(after_campaign):
    state = after_campaign.get("/dashboard/state").json()
    measurement = state["campaign"]["measurement"]
    assert measurement["lift"] == pytest.approx(
        measurement["treated_rate"] - measurement["control_rate"])
    assert measurement["treated_n"] + measurement["control_n"] == 12
    assert state["campaign"]["horizon_days"] == simulate.VALUE_HORIZON_DAYS


def test_learning_appears_once_a_campaign_is_measured(after_campaign):
    learning = after_campaign.get("/dashboard/state").json()["learning"]
    assert learning["campaigns"] == 1
    row = learning["series"][0]
    assert row["predicted_uplift"] is not None
    assert row["actual_uplift"] is not None
    assert learning["headline"]


def test_the_state_stays_cheap_enough_to_poll(after_campaign):
    """Two polls in a row must not recompute triage from scratch."""
    import time
    after_campaign.get("/dashboard/state")
    started = time.time()
    after_campaign.get("/dashboard/state")
    assert time.time() - started < 1.0, "polling every two seconds would fall behind"


def test_the_hidden_truth_reaches_the_chart_only_through_the_export(fresh_client, tmp_path):
    """The agent never imports the simulated world, so the truth arrives as a data file."""
    from api import main

    export = {"rows": [{"sequence": 1, "hidden_truth": 0.30, "belief_error_vs_truth": 0.16}]}
    with open(main.LEARNING_EXPORT_PATH, "w", encoding="utf-8") as handle:
        json.dump(export, handle)

    memory = main.store.connect()
    try:
        learning = main._learning(memory, MERCHANT_ID)
    finally:
        memory.close()
    assert learning["hidden_truth"] == pytest.approx(0.30)

    source = open(main.__file__, encoding="utf-8").read()
    assert "world.outcomes" not in source
    assert "from world" not in source


def test_a_corrupt_export_does_not_take_the_page_down(fresh_client):
    from api import main
    with open(main.LEARNING_EXPORT_PATH, "w", encoding="utf-8") as handle:
        handle.write("{not json at all")
    response = fresh_client.get("/dashboard/state")
    assert response.status_code == 200
    assert response.json()["learning"]["hidden_truth"] is None
