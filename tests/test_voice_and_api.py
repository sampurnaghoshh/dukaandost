"""The soundbox decision paths and every FastAPI endpoint, all offline.

Nothing here touches Sarvam. The fallback buttons need neither the microphone nor the
network by design, which is exactly why they are the demo day insurance.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from core import fixtures, simulate, triage
from voice import local_soundbox
from voice.base import Brief, BriefNumbers

MERCHANT_ID = "tea_stall_01"
HAAN = "हां"
NAHI = "नहीं"


# ---------------------------------------------------------------- reading the reply


@pytest.mark.parametrize("reply", [
    "haan", "Haan bhej do", "ji haan", "theek hai", "bhej do", "ha", "accha bhej dijiye",
    HAAN, "जी " + HAAN, "भेज दो", "OK",
])
def test_approvals_are_heard_as_approval(reply):
    assert local_soundbox.classify_reply(reply)["outcome"] == "approved"


@pytest.mark.parametrize("reply", [
    "nahi", "nahin", "mat bhejo", "abhi nahi", "rehne do", "no", NAHI,
    "मत भेजो", "nahi chahiye",
])
def test_refusals_are_heard_as_refusal(reply):
    assert local_soundbox.classify_reply(reply)["outcome"] == "declined"


@pytest.mark.parametrize("reply", [
    "", "   ", "kya bol rahe ho", "zara soch ke batata hoon", "ek minute",
])
def test_anything_else_is_unclear(reply):
    assert local_soundbox.classify_reply(reply)["outcome"] == "unclear"


def test_a_reply_holding_both_answers_is_unclear():
    """Spending a merchant's money on an ambiguous answer is the one unrecoverable mistake."""
    verdict = local_soundbox.classify_reply("haan nahi nahi rehne do")
    assert verdict["outcome"] == "unclear"
    assert "both" in verdict["reason"]


def test_a_refusal_inside_a_longer_word_does_not_count():
    """Whole word matching, so nahi hiding inside another word is not a refusal."""
    assert local_soundbox.classify_reply("nahiyat")["outcome"] == "unclear"


# ---------------------------------------------------------------- the soundbox paths


@pytest.fixture
def open_call(monkeypatch):
    """A pending call with no audio, so nothing here needs Sarvam."""
    monkeypatch.setattr(local_soundbox.speech, "speak",
                        lambda *a, **k: {"audio_path": "", "source": "stub"})
    brief = Brief(
        merchant_id=MERCHANT_ID, merchant_name="Ramesh Tea Stall", today="2026-09-17",
        candidate_id="winback_test", action_type="lapsed_winback", title="Winback",
        rationale="Bring the regulars back.", script="Namaste Ramesh ji", script_template="x",
        customer_message_template="Namaste {customer_name}",
        numbers=BriefNumbers(
            lapsed_count=12, value_at_risk_monthly=9460.0, discount_pct=20.0,
            offer_applies_to="chai", segment_size=12, treated_count=10, holdout_count=2,
            holdout_share=0.15, baseline_response_rate=0.05, offer_uplift=0.14,
            incremental_responses=1.43, incremental_revenue=675.42, discount_cost=183.33,
            expected_profit=186.62, horizon_days=30))
    decision = local_soundbox.dial(MERCHANT_ID, brief)
    return decision.call_id


def test_the_haan_button_approves_without_mic_or_network(open_call):
    decision = local_soundbox.press_button(open_call, "haan")
    assert decision.approved is True
    assert decision.outcome == "approved"
    assert decision.channel == "local_soundbox"
    assert decision.modifications["via"] == "button"
    assert decision.candidate_id == "winback_test"


def test_the_nahi_button_declines_without_mic_or_network(open_call):
    decision = local_soundbox.press_button(open_call, "nahi")
    assert decision.approved is False
    assert decision.outcome == "declined"
    assert decision.modifications["via"] == "button"


def test_the_button_and_the_voice_paths_produce_the_same_decision_shape(open_call, monkeypatch):
    spoken = local_soundbox.handle_reply(open_call, transcript="haan bhej do")
    pressed_call = local_soundbox.dial(MERCHANT_ID, local_soundbox.PENDING[open_call]["brief"])
    pressed = local_soundbox.press_button(pressed_call.call_id, "haan")

    assert spoken["status"] == "settled"
    voice = spoken["decision"]
    assert voice.approved == pressed.approved
    assert voice.outcome == pressed.outcome
    assert set(voice.model_dump()) == set(pressed.model_dump())


def test_an_unclear_reply_reprompts_once_then_gives_up(open_call):
    first = local_soundbox.handle_reply(open_call, transcript="kya bol rahe ho")
    assert first["status"] == "reprompt"
    assert first["reprompts"] == 1
    assert first["reprompt_text"]

    second = local_soundbox.handle_reply(open_call, transcript="kuch samajh aaya kya")
    assert second["status"] == "settled"
    assert second["decision"].approved is False
    assert second["decision"].modifications["undecided"] is True


def test_a_clear_answer_after_a_reprompt_still_settles(open_call):
    assert local_soundbox.handle_reply(open_call, transcript="hmm")["status"] == "reprompt"
    second = local_soundbox.handle_reply(open_call, transcript="haan bhej do")
    assert second["status"] == "settled"
    assert second["decision"].approved is True
    assert second["decision"].modifications["reprompts"] == 1


# ---------------------------------------------------------------- the ranked winner


def test_the_brief_uses_the_ranked_winner_not_the_first_proposal(conn, triage_result):
    """The brief must follow expected profit, not the order the model happened to answer in.

    The attach upsell is put first and the winback last. The winback is worth more, so it is
    the one that has to end up in the brief.
    """
    by_id = {item["candidate_id"]: item for item in fixtures.PLACEHOLDER_CANDIDATES}
    shuffled = [by_id["attach_snack_with_chai_10"], by_id["offpeak_tuesday_afternoon_15"],
                by_id["winback_lapsed_chai_10"]]
    ranking = simulate.rank(MERCHANT_ID, shuffled, triage_result, conn=conn)

    assert ranking["recommended"]["candidate_id"] == "winback_lapsed_chai_10"
    assert ranking["recommended"] is ranking["ranked"][0]
    profits = [item["estimates"]["expected_profit"] for item in ranking["ranked"]]
    assert profits == sorted(profits, reverse=True)


def test_the_recommended_candidate_is_the_highest_profit_survivor(conn, triage_result):
    ranking = simulate.rank(MERCHANT_ID, fixtures.PLACEHOLDER_CANDIDATES, triage_result,
                            conn=conn)
    best = max(item["estimates"]["expected_profit"] for item in ranking["ranked"])
    assert ranking["recommended"]["estimates"]["expected_profit"] == best
    for item in ranking["rejected"]:
        assert not item.get("accepted")


# ---------------------------------------------------------------- the API


@pytest.fixture
def client(no_network, tmp_path, monkeypatch):
    from api import main
    monkeypatch.setattr(main.run_night, "LOG_PATH", str(tmp_path / "decisions.jsonl"))
    # Campaign memory goes in the temp directory. Without this the launch tests write real
    # campaigns into data/memory.db, which is the database the demo reads, and a full test
    # run quietly pushes the learning curve from six campaigns to eleven.
    monkeypatch.setenv("DUKAAN_MEMORY_PATH", str(tmp_path / "memory.db"))
    main._ANALYSIS_CACHE.clear()
    main.local_soundbox.PENDING.clear()
    return TestClient(main.app)


def test_health_is_200_and_never_reveals_the_key(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["llm_mode"] == "replay"
    assert isinstance(payload["key_present"], bool)
    assert "SARVAM_API_KEY" not in response.text
    assert "api-subscription-key" not in response.text


def test_triage_endpoint(client):
    response = client.post("/triage", json={"merchant_id": MERCHANT_ID})
    assert response.status_code == 200
    payload = response.json()
    assert payload["signals"]["lapsed_regulars"]["count"] == 12
    assert payload["worth_a_call"] is True


def test_generate_endpoint(client):
    response = client.post("/generate", json={"merchant_id": MERCHANT_ID})
    assert response.status_code == 200
    payload = response.json()
    assert payload["candidates"], "no candidates came back at all"
    assert payload["source"] in ("llm", "fixture")


def test_simulate_endpoint_returns_the_level_grid(client):
    response = client.post("/simulate", json={"merchant_id": MERCHANT_ID})
    assert response.status_code == 200
    payload = response.json()
    assert payload["level_grid"], "the dashboard needs the grid"
    for row in payload["level_grid"]:
        assert set(row["levels"]) == {"LOW", "MEDIUM", "HIGH"}
    assert payload["recommended"]["estimates"]["expected_profit"] > 0


def test_call_then_button_then_launch_then_measure(client):
    call = client.post("/call", json={"merchant_id": MERCHANT_ID})
    assert call.status_code == 200
    body = call.json()
    assert body["status"] == "awaiting_reply"
    call_id = body["call_id"]
    assert body["brief"]["numbers"]["lapsed_count"] == 12

    state = client.get("/soundbox/state", params={"call_id": call_id})
    assert state.status_code == 200
    assert state.json()["settled"] is False

    pressed = client.post("/soundbox/button", json={"call_id": call_id, "answer": "haan"})
    assert pressed.status_code == 200
    decision = pressed.json()["decision"]
    assert decision["approved"] is True

    launched = client.post("/campaign/launch", json={
        "merchant_id": MERCHANT_ID, "call_id": call_id,
        "candidate_id": decision["candidate_id"], "approved": True,
        "transcript": decision["transcript"]})
    assert launched.status_code == 200
    payload = launched.json()
    assert payload["status"] == "approved"
    campaign_id = payload["campaign_id"]
    assert payload["treated_count"] + payload["holdout_count"] == 12

    # Step 5 made /measure real. It used to answer not_ready from a stub.
    measured = client.post("/measure", json={"campaign_id": campaign_id,
                                             "merchant_id": MERCHANT_ID})
    assert measured.status_code == 200
    body = measured.json()
    assert body["status"] == "measured"
    measurement = body["measurement"]
    assert measurement["treated_n"] + measurement["control_n"] == 12
    assert measurement["lift"] == pytest.approx(
        measurement["treated_rate"] - measurement["control_rate"])


def test_declining_launches_nothing(client):
    call = client.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    client.post("/soundbox/button", json={"call_id": call["call_id"], "answer": "nahi"})
    launched = client.post("/campaign/launch", json={
        "merchant_id": MERCHANT_ID, "call_id": call["call_id"], "approved": False})
    assert launched.status_code == 200
    assert launched.json()["status"] == "declined"
    assert launched.json()["campaign_id"] is None


def test_soundbox_page_serves_and_carries_both_buttons(client):
    response = client.get("/soundbox", params={"call_id": "anything"})
    assert response.status_code == 200
    assert "HAAN" in response.text and "NAHI" in response.text


def test_decisions_endpoint_tails_the_log(client):
    client.post("/triage", json={"merchant_id": MERCHANT_ID})
    response = client.get("/decisions", params={"limit": 10})
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] >= 1
    assert any(row["stage"] == "triage" for row in payload["decisions"])


def test_every_endpoint_logs_one_decision_line(client, tmp_path):
    from api import main
    client.post("/triage", json={"merchant_id": MERCHANT_ID})
    lines = [json.loads(line) for line in
             open(main.run_night.LOG_PATH, encoding="utf-8").read().splitlines() if line]
    assert lines
    assert all("stage" in row and "decision" in row for row in lines)


def test_an_unknown_call_id_is_a_404_not_a_crash(client):
    assert client.get("/soundbox/state", params={"call_id": "call_nope"}).status_code == 404
    assert client.post("/soundbox/button",
                       json={"call_id": "call_nope", "answer": "haan"}).status_code == 404


# ---------------------------------------------------------------- call_id fallback
#
# The Sarvam agent tool fires mid conversation and does not reliably have the call id to
# hand, so an absent one falls back to the most recent open call for that merchant.


def test_launch_without_a_call_id_falls_back_to_the_latest_open_call(client):
    call = client.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    client.post("/soundbox/button", json={"call_id": call["call_id"], "answer": "haan"})

    launched = client.post("/campaign/launch", json={
        "merchant_id": MERCHANT_ID, "approved": True}).json()

    assert launched["status"] == "approved"
    assert launched["call_id"] == call["call_id"]
    assert launched["call_id_source"] == "resolved_latest_open"
    assert launched["treated_count"] == 10


def test_the_fallback_picks_the_most_recent_of_several_open_calls(client):
    older = client.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    newer = client.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    assert older["call_id"] != newer["call_id"]

    launched = client.post("/campaign/launch", json={
        "merchant_id": MERCHANT_ID, "approved": True}).json()
    assert launched["call_id"] == newer["call_id"]


def test_an_explicit_call_id_still_wins_over_the_fallback(client):
    wanted = client.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    client.post("/call", json={"merchant_id": MERCHANT_ID})   # a newer call it must ignore

    launched = client.post("/campaign/launch", json={
        "merchant_id": MERCHANT_ID, "call_id": wanted["call_id"], "approved": True}).json()

    assert launched["call_id"] == wanted["call_id"]
    assert launched["call_id_source"] == "explicit"


def test_launch_404s_when_there_is_genuinely_no_open_call(client):
    from voice import local_soundbox as soundbox
    soundbox.PENDING.clear()

    response = client.post("/campaign/launch", json={"merchant_id": MERCHANT_ID,
                                                     "approved": True})
    assert response.status_code == 404
    assert MERCHANT_ID in response.json()["detail"]


def test_an_unknown_explicit_call_id_still_404s(client):
    """The fallback must not paper over a caller naming a call that does not exist."""
    client.post("/call", json={"merchant_id": MERCHANT_ID})
    response = client.post("/campaign/launch", json={
        "merchant_id": MERCHANT_ID, "call_id": "call_does_not_exist", "approved": True})
    assert response.status_code == 404
    assert "call_does_not_exist" in response.json()["detail"]


def test_the_fallback_ignores_calls_belonging_to_another_merchant(client):
    from voice import local_soundbox as soundbox
    soundbox.PENDING.clear()
    call = client.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    soundbox.PENDING[call["call_id"]]["merchant_id"] = "some_other_shop"

    response = client.post("/campaign/launch", json={"merchant_id": MERCHANT_ID,
                                                     "approved": True})
    assert response.status_code == 404


def test_the_resolved_call_id_is_written_to_the_decision_log(client):
    from api import main
    call = client.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    client.post("/campaign/launch", json={"merchant_id": MERCHANT_ID, "approved": True})

    lines = [json.loads(line) for line in
             open(main.run_night.LOG_PATH, encoding="utf-8").read().splitlines() if line]
    launches = [row for row in lines if row["stage"] == "campaign_launch"]
    assert launches
    assert launches[-1]["call_id"] == call["call_id"]
    assert launches[-1]["call_id_source"] == "resolved_latest_open"


def test_a_decline_without_a_call_id_does_not_404(client):
    """A no still gets logged against whichever call it was, and dispatches nothing."""
    call = client.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    declined = client.post("/campaign/launch", json={
        "merchant_id": MERCHANT_ID, "approved": False}).json()

    assert declined["status"] == "declined"
    assert declined["campaign_id"] is None
    assert declined["call_id"] == call["call_id"]
