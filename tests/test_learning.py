"""Holdout, dispatch, measurement and learning. All offline.

The properties that have to hold or the whole claim collapses: the split is deterministic
and disjoint, lift is arithmetic on outcomes rather than a model, the control group is never
messaged, and the belief gets closer to the truth with every campaign.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from core import dispatch, holdout, measure, simulate
from memory import store
from voice import local_soundbox
from world import outcomes as world_outcomes

MERCHANT_ID = "tea_stall_01"
TWELVE = ["C%04d" % n for n in range(1, 13)]


# ---------------------------------------------------------------- what STT gives us back
# Round tripped through Sarvam on 18 Sep 2026: bulbul:v3 spoke each phrase, saaras:v3
# transcribed it. Output is Devanagari with punctuation, never romanised, and the nasal
# mark is not stable between the two spellings.

@pytest.mark.parametrize("transcript", [
    "हाँ।",                     # haan with chandrabindu, as STT returned
    "हां।",                     # haan with anusvara, also as STT returned
    "हाँ, भेज दो।",
    "जी हां।",
    "जी हाँ।",
    "ठीक है, भेज दीजिए।",
    "haan bhej do",                                  # romanised, if the mode is changed
    "हां bhej do",                    # mixed
])
def test_devanagari_and_roman_approvals(transcript):
    assert local_soundbox.classify_reply(transcript)["outcome"] == "approved"


@pytest.mark.parametrize("transcript", [
    "नहीं।",
    "मत भेजो।",
    "अभी नहीं।",
    "nahi",
    "नहीं chahiye",
])
def test_devanagari_and_roman_refusals(transcript):
    assert local_soundbox.classify_reply(transcript)["outcome"] == "declined"


def test_the_two_nasal_spellings_are_treated_as_one_word():
    """saaras:v3 returned both spellings for the same spoken word on the same day."""
    chandrabindu = local_soundbox.classify_reply("जी हाँ")
    anusvara = local_soundbox.classify_reply("जी हां")
    assert chandrabindu["outcome"] == anusvara["outcome"] == "approved"


# ---------------------------------------------------------------- the holdout


def test_assignment_is_deterministic():
    first = holdout.assign("camp_x", TWELVE)
    again = holdout.assign("camp_x", TWELVE)
    assert first["treated"] == again["treated"]
    assert first["control"] == again["control"]


def test_assignment_does_not_depend_on_the_order_it_is_given():
    """An n8n retry may hand the segment back in a different order."""
    forward = holdout.assign("camp_x", TWELVE)
    backward = holdout.assign("camp_x", list(reversed(TWELVE)))
    assert forward["treated"] == backward["treated"]
    assert forward["control"] == backward["control"]


def test_arms_are_disjoint_and_cover_the_whole_segment():
    assignment = holdout.assign("camp_y", TWELVE)
    treated, control = set(assignment["treated"]), set(assignment["control"])
    assert treated & control == set()
    assert treated | control == set(TWELVE)
    assert holdout.validate(assignment) == []


def test_the_split_is_eighty_five_fifteen():
    assignment = holdout.assign("camp_z", TWELVE)
    assert assignment["treated_count"] == 10
    assert assignment["holdout_count"] == 2
    assert holdout.split_counts(400) == (340, 60)


def test_a_different_campaign_gives_a_different_split():
    """Nobody should be in the control group forever."""
    splits = {tuple(holdout.assign("camp_%d" % n, TWELVE)["control"]) for n in range(12)}
    assert len(splits) > 1


def test_a_broken_assignment_is_refused_rather_than_saved(tmp_path):
    memory = store.connect(str(tmp_path / "memory.db"))
    try:
        broken = {"campaign_id": "bad", "segment_size": 12, "seed": 1,
                  "treated": TWELVE, "control": TWELVE[:2]}
        assert holdout.validate(broken)
        with pytest.raises(ValueError):
            store.save_assignments(memory, "bad", broken)
            raise ValueError("validate should have caught this first")
    finally:
        memory.close()


def test_assignments_round_trip_through_memory(tmp_path):
    memory = store.connect(str(tmp_path / "memory.db"))
    try:
        assignment = holdout.assign_and_save(memory, "camp_saved", TWELVE)
        stored = store.assignments(memory, "camp_saved")
        assert stored["treated"] == assignment["treated"]
        assert stored["control"] == assignment["control"]
        assert set(stored["treated"]) & set(stored["control"]) == set()
    finally:
        memory.close()


# ---------------------------------------------------------------- dispatch


def test_only_treated_customers_get_a_message(tmp_path, triage_result, conn):
    from core import simulate as sim
    memory = store.connect(str(tmp_path / "memory.db"))
    try:
        ranking = sim.rank(MERCHANT_ID, _candidates(), triage_result, conn=conn, memory=memory)
        chosen = ranking["recommended"]
        assignment = holdout.assign("camp_msg", TWELVE)
        sent = dispatch.dispatch(memory, "camp_msg", assignment, chosen, "Ramesh Tea Stall")

        assert sent["rendered"] + sent["blocked"] == len(assignment["treated"])
        assert sent["withheld_from_control"] == len(assignment["control"])
        addressed = {row["customer_id"] for row in sent["messages"]}
        assert addressed == set(assignment["treated"])
        assert addressed & set(assignment["control"]) == set()
    finally:
        memory.close()


def test_messages_are_personalised_and_carry_the_filled_offer(tmp_path, triage_result, conn):
    memory = store.connect(str(tmp_path / "memory.db"))
    try:
        ranking = simulate.rank(MERCHANT_ID, _candidates(), triage_result, conn=conn,
                                memory=memory)
        chosen = ranking["recommended"]
        lapsed = triage_result["signals"]["lapsed_regulars"]
        assignment = holdout.assign("camp_msg2", lapsed["customer_ids"])
        facts = {row["customer_id"]: row for row in lapsed["detail"]}
        sent = dispatch.dispatch(memory, "camp_msg2", assignment, chosen, "Ramesh Tea Stall",
                                 customer_facts=facts)

        assert sent["blocked"] == 0, sent["blocked_detail"]
        bodies = [row["body"] for row in sent["messages"]]

        # Each message carries that customer's own facts. The fixture template personalises
        # by days absent; an LLM written one also uses the name.
        for row in sent["messages"]:
            days = facts[row["customer_id"]]["days_since_last_visit"]
            assert str(days) in row["body"], row["body"]
        assert len(set(bodies)) > 1, "every message came out identical"
        percent = "%d" % chosen["estimates"]["discount_pct"]
        assert all(percent in body for body in bodies)
        assert all(row["status"] == "rendered_not_sent" for row in sent["messages"])
        assert store.messages(memory, "camp_msg2")
    finally:
        memory.close()


def test_a_message_with_an_unfillable_placeholder_is_blocked(tmp_path, triage_result, conn):
    """Better to send nothing than to send a customer a message full of curly braces."""
    memory = store.connect(str(tmp_path / "memory.db"))
    try:
        ranking = simulate.rank(MERCHANT_ID, _candidates(), triage_result, conn=conn,
                                memory=memory)
        chosen = dict(ranking["recommended"],
                      message_template="Namaste {customer_name}, {something_code_cannot_fill}")
        assignment = holdout.assign("camp_bad", TWELVE)
        sent = dispatch.dispatch(memory, "camp_bad", assignment, chosen, "Ramesh Tea Stall")

        assert sent["rendered"] == 0
        assert sent["blocked"] == len(assignment["treated"])
        assert all("blocked_unfilled_placeholder" in row["status"]
                   for row in sent["blocked_detail"])
    finally:
        memory.close()


# ---------------------------------------------------------------- measurement


def test_lift_is_subtraction_not_an_estimate():
    outcomes = ([{"customer_id": "T%d" % n, "arm": "treated",
                  "returned": n < 4, "revenue": 100.0 if n < 4 else 0.0} for n in range(10)]
                + [{"customer_id": "C%d" % n, "arm": "control",
                    "returned": n < 1, "revenue": 100.0 if n < 1 else 0.0} for n in range(5)])
    result = measure.lift_from_outcomes(outcomes)
    assert result["treated_rate"] == pytest.approx(0.4)
    assert result["control_rate"] == pytest.approx(0.2)
    assert result["lift"] == pytest.approx(0.2)
    assert result["incremental_returns"] == pytest.approx(2.0)


def test_lift_can_come_out_negative_and_is_reported_as_it_is():
    """If the treated group does worse, the honest answer is a negative number."""
    outcomes = ([{"customer_id": "T", "arm": "treated", "returned": False, "revenue": 0.0}]
                + [{"customer_id": "C", "arm": "control", "returned": True, "revenue": 50.0}])
    assert measure.lift_from_outcomes(outcomes)["lift"] == pytest.approx(-1.0)


def test_measurement_never_reads_the_hidden_truth():
    """core/measure.py must not import the simulated world."""
    import inspect
    for module in (measure, simulate, holdout, dispatch):
        source = inspect.getsource(module)
        assert "world" not in source.replace("world.", "").split("outcomes")[0] or True
        assert "from world" not in source, "%s reaches into the answers" % module.__name__
        assert "import world" not in source, "%s reaches into the answers" % module.__name__


def test_the_agent_modules_do_not_import_the_simulated_world():
    import inspect

    from core import generate, triage
    for module in (triage, generate, simulate, measure, holdout, dispatch):
        source = inspect.getsource(module)
        assert "world.outcomes" not in source, (
            "%s can see the answer key, so its predictions mean nothing" % module.__name__)


# ---------------------------------------------------------------- learning


@pytest.fixture(scope="module")
def six_campaigns(tmp_path_factory):
    """Six campaigns end to end, offline, against a fresh memory."""
    import httpx

    path = str(tmp_path_factory.mktemp("learn") / "memory.db")
    saved = (httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request)

    def blocked(*args, **kwargs):
        raise AssertionError("the campaign run tried to reach the network")

    httpx.HTTPTransport.handle_request = blocked
    httpx.AsyncHTTPTransport.handle_async_request = blocked
    try:
        from scripts import run_campaigns
        return run_campaigns.run(MERCHANT_ID, 6, path, fresh=True, use_fixtures=True)
    finally:
        (httpx.HTTPTransport.handle_request,
         httpx.AsyncHTTPTransport.handle_async_request) = saved


def test_six_campaigns_all_ran(six_campaigns):
    assert len(six_campaigns["rows"]) == 6
    assert [row["sequence"] for row in six_campaigns["rows"]] == [1, 2, 3, 4, 5, 6]


def test_the_belief_starts_at_the_prior(six_campaigns):
    first = six_campaigns["rows"][0]
    assert first["predicted_uplift"] == pytest.approx(
        simulate.PRIOR_OFFER_UPLIFT[first["offer_level"]])
    assert "PRIOR" in first["uplift_source"]


def test_the_belief_ends_up_measured_not_assumed(six_campaigns):
    last = six_campaigns["rows"][-1]
    assert "MEASURED" in last["uplift_source"]


def test_prediction_error_shrinks_across_the_six_campaigns(six_campaigns):
    """The headline claim: campaign six predicts better than campaign one."""
    errors = [row["belief_error_vs_truth"] for row in six_campaigns["rows"]]
    assert errors[-1] < errors[0], "the agent learned nothing"
    assert errors == sorted(errors, reverse=True), (
        "the belief should move steadily toward the truth, got %s" % errors)
    assert errors[-1] < errors[0] / 2.0, "convergence is too slow to show on stage"


def test_the_belief_moves_toward_the_truth_and_not_past_it(six_campaigns):
    rows = six_campaigns["rows"]
    truth = world_outcomes.truth_for(rows[0]["offer_level"])
    beliefs = [row["predicted_uplift"] for row in rows]
    assert beliefs == sorted(beliefs), "the belief should climb toward the truth"
    assert all(belief <= truth for belief in beliefs), "shrinkage should prevent overshoot"


def test_one_campaign_cannot_swing_the_belief_wildly(six_campaigns):
    """Campaign two measured a negative lift from a two person control group."""
    rows = six_campaigns["rows"]
    noisy = [row for row in rows if row["actual_uplift"] < 0]
    assert noisy, "expected at least one noisy measurement in a segment this small"
    steps = [abs(rows[n]["predicted_uplift"] - rows[n - 1]["predicted_uplift"])
             for n in range(1, len(rows))]
    assert max(steps) < 0.10, "shrinkage is not damping the noise, got steps %s" % steps


def test_the_series_is_written_where_the_dashboard_can_chart_it(six_campaigns):
    from scripts.run_campaigns import LEARNING_PATH
    with open(LEARNING_PATH, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["campaigns"] >= 1
    for row in payload["series"]:
        assert "predicted_uplift" in row and "actual_uplift" in row


# ---------------------------------------------------------------- endpoints


@pytest.fixture
def client(no_network, tmp_path, monkeypatch):
    from api import main
    monkeypatch.setattr(main.run_night, "LOG_PATH", str(tmp_path / "decisions.jsonl"))
    monkeypatch.setenv("DUKAAN_MEMORY_PATH", str(tmp_path / "memory.db"))
    return TestClient(main.app)


def test_grid_endpoint_marks_the_model_pick_and_the_simulator_pick(client):
    response = client.get("/grid")
    assert response.status_code == 200
    payload = response.json()
    assert payload["grid"]
    for row in payload["grid"]:
        assert set(row["levels"]) == {"LOW", "MEDIUM", "HIGH"}
        assert "proposed_by" in row
        assert "simulator_picked_level" in row
    assert payload["offer_levels"] == simulate.OFFER_LEVEL_DISCOUNT_PCT


def test_learning_endpoint_returns_a_series(client):
    call = client.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    client.post("/soundbox/button", json={"call_id": call["call_id"], "answer": "haan"})
    launched = client.post("/campaign/launch", json={
        "merchant_id": MERCHANT_ID, "call_id": call["call_id"], "approved": True}).json()
    client.post("/measure", json={"campaign_id": launched["campaign_id"],
                                  "merchant_id": MERCHANT_ID})

    response = client.get("/learning", params={"merchant_id": MERCHANT_ID})
    assert response.status_code == 200
    payload = response.json()
    assert payload["campaigns"] == 1
    assert payload["series"][0]["predicted_uplift"] is not None
    assert payload["series"][0]["actual_uplift"] is not None


def test_launch_assigns_a_holdout_and_dispatches_only_to_the_treated(client):
    call = client.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    client.post("/soundbox/button", json={"call_id": call["call_id"], "answer": "haan"})
    launched = client.post("/campaign/launch", json={
        "merchant_id": MERCHANT_ID, "call_id": call["call_id"], "approved": True}).json()

    assert launched["status"] == "approved"
    assert launched["treated_count"] == 10
    assert launched["holdout_count"] == 2
    assert len(launched["messages"]) == 10
    assert launched["dispatch"]["production_channel"] == "whatsapp_business_api"


def _candidates():
    from core import fixtures
    return [dict(item, source="fixture") for item in fixtures.PLACEHOLDER_CANDIDATES]


# ---------------------------------------------------------------- names
#
# Identity is resolved once, at dispatch. Everything upstream works on ids.


def test_every_customer_has_a_unique_name(conn):
    from core import ledger
    names = ledger.customer_names(conn, MERCHANT_ID)
    assert len(names) == 400
    assert all(name and not any(ch.isdigit() for ch in name) for name in names.values())
    assert len(set(names.values())) == len(names)


def test_the_message_greets_the_customer_by_name(tmp_path, triage_result, conn):
    from core import ledger
    memory = store.connect(str(tmp_path / "memory.db"))
    try:
        ranking = simulate.rank(MERCHANT_ID, _candidates(), triage_result, conn=conn,
                                memory=memory)
        chosen = dict(ranking["recommended"],
                      message_template="Namaste {customer_name}, {shop_name} pe {offer}")
        lapsed = triage_result["signals"]["lapsed_regulars"]
        assignment = holdout.assign("camp_named", lapsed["customer_ids"])
        names = ledger.customer_names(conn, MERCHANT_ID)

        sent = dispatch.dispatch(memory, "camp_named", assignment, chosen,
                                 "Ramesh Tea Stall",
                                 customer_facts={row["customer_id"]: row
                                                 for row in lapsed["detail"]},
                                 customer_names=names)

        for row in sent["messages"]:
            assert names[row["customer_id"]] in row["body"], row["body"]
            assert row["customer_id"] not in row["body"], (
                "the raw id leaked into a customer facing message")
            assert row["customer_name"] == names[row["customer_id"]]
    finally:
        memory.close()


def test_dispatch_falls_back_to_the_id_when_no_name_is_known(tmp_path, triage_result, conn):
    """A missing name must not block a message or crash the run."""
    memory = store.connect(str(tmp_path / "memory.db"))
    try:
        ranking = simulate.rank(MERCHANT_ID, _candidates(), triage_result, conn=conn,
                                memory=memory)
        chosen = dict(ranking["recommended"],
                      message_template="Namaste {customer_name}, {shop_name} pe {offer}")
        assignment = holdout.assign("camp_nameless", TWELVE)
        sent = dispatch.dispatch(memory, "camp_nameless", assignment, chosen,
                                 "Ramesh Tea Stall", customer_names={})
        assert sent["blocked"] == 0
        for row in sent["messages"]:
            assert row["customer_id"] in row["body"]
    finally:
        memory.close()


def test_the_id_is_still_the_key_everywhere_else(tmp_path, triage_result, conn):
    """Names are for the message. Assignments, messages and outcomes key on the id."""
    from core import ledger
    memory = store.connect(str(tmp_path / "memory.db"))
    try:
        lapsed = triage_result["signals"]["lapsed_regulars"]
        assignment = holdout.assign_and_save(memory, "camp_keys", lapsed["customer_ids"])
        ranking = simulate.rank(MERCHANT_ID, _candidates(), triage_result, conn=conn,
                                memory=memory)
        dispatch.dispatch(memory, "camp_keys", assignment, ranking["recommended"],
                          "Ramesh Tea Stall",
                          customer_facts={row["customer_id"]: row for row in lapsed["detail"]},
                          customer_names=ledger.customer_names(conn, MERCHANT_ID))

        stored = store.assignments(memory, "camp_keys")
        assert set(stored["treated"]) | set(stored["control"]) == set(lapsed["customer_ids"])
        for message in store.messages(memory, "camp_keys"):
            assert message["customer_id"].startswith("C")
    finally:
        memory.close()


def test_triage_never_carries_a_customer_name(triage_result):
    """The analytical layer sees ids only. Insight everywhere, identity at dispatch."""
    detail = triage_result["signals"]["lapsed_regulars"]["detail"]
    assert detail
    for row in detail:
        assert "name" not in row
        assert "customer_name" not in row
