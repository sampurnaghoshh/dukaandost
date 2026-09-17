"""The number guard, item resolution and the offline replay path.

Every test here runs in replay mode with httpx blocked, so the suite never touches the
network and never spends a rupee of Sarvam credit.
"""

from __future__ import annotations

import json

import pytest

from core import generate, ledger, llm, run_night, simulate

MERCHANT_ID = "tea_stall_01"

DEVANAGARI_TWELVE = "१२"
RUPEE = "₹"


# ---------------------------------------------------------------- the number guard


def test_guard_catches_ascii_digits():
    assert generate.number_violations("aapke 12 grahak nahi aaye") == ["1", "2"]


def test_guard_catches_devanagari_digits():
    """The obvious way to sneak a number past a lazy check is to write it in another script."""
    assert generate.number_violations("aapke %s grahak" % DEVANAGARI_TWELVE) == list(
        DEVANAGARI_TWELVE)


def test_guard_catches_percent_and_rupee_signs():
    assert "%" in generate.number_violations("chai pe 10% chhoot")
    assert RUPEE in generate.number_violations("%s9400 ka nuksan" % RUPEE)


def test_guard_allows_placeholders_holding_the_numbers():
    clean = "Namaste {merchant_name}, {lapsed_count} grahak, {value_at_risk} ka nuksan"
    assert generate.number_violations(clean) == []


def test_guard_ignores_digits_inside_a_placeholder_name():
    """Only literal text is checked. A field name is not something the agent says out loud."""
    assert generate.number_violations("{value_at_risk} aur {top_3_items}") == []


def test_guard_rejects_malformed_braces():
    assert generate.number_violations("Namaste {merchant_name") == ["malformed placeholder braces"]


def test_guard_rejects_placeholders_code_cannot_fill():
    problems = generate.check_text("Namaste {sarpanch_name}", generate.CUSTOMER_PLACEHOLDERS)
    assert any("cannot fill" in item for item in problems)


def test_the_fallback_call_script_passes_its_own_guard():
    from core import fixtures
    assert generate.check_text(fixtures.FALLBACK_CALL_SCRIPT,
                               generate.CALL_SCRIPT_PLACEHOLDERS) == []
    _literals, fields = generate.split_placeholders(fixtures.FALLBACK_CALL_SCRIPT)
    assert generate.REQUIRED_CALL_SCRIPT_PLACEHOLDERS <= set(fields)


def test_a_candidate_carrying_a_number_is_rejected():
    dirty = generate.GeneratedCandidate(
        title="Winback", action_type="lapsed_winback", target_segment={"kind": "lapsed_regulars"},
        offer_level="LOW", rationale="Bring back the regulars.",
        message_template="Namaste {customer_name}, chai pe 10% chhoot!")
    problems = generate._candidate_problems(dirty)
    assert any("numerals or currency" in item for item in problems)


def test_a_clean_candidate_is_accepted():
    clean = generate.GeneratedCandidate(
        title="Winback", action_type="lapsed_winback", target_segment={"kind": "lapsed_regulars"},
        offer_level="LOW", rationale="Bring back the regulars.",
        message_template="Namaste {customer_name}, {shop_name} pe {offer}!")
    assert generate._candidate_problems(clean) == []


# ---------------------------------------------------------------- offer levels


def test_offer_level_maps_to_a_percentage_in_code_not_in_the_model():
    for level in simulate.OFFER_LEVELS:
        candidate = {"offer": {"type": "percent_discount", "offer_level": level}}
        assert simulate.discount_fraction(candidate) == (
            simulate.OFFER_LEVEL_DISCOUNT_PCT[level] / 100.0)


def test_a_raw_percentage_buckets_back_to_a_level():
    assert simulate.offer_level({"offer": {"value": 10}}) == "LOW"
    assert simulate.offer_level({"offer": {"value": 20}}) == "MEDIUM"
    assert simulate.offer_level({"offer": {"value": 50}}) == "HIGH"


# ---------------------------------------------------------------- item resolution


def test_item_resolution_clusters_the_messy_names(conn, no_network):
    """Scores the mapping against true_item. EVALUATION ONLY, the agent never sees it."""
    raw_names = ledger.raw_item_names(conn, MERCHANT_ID)
    try:
        mapping = generate.resolve_items(raw_names)
    except llm.LLMUnavailable as exc:
        pytest.skip("nothing cached for item resolution: %s" % exc)

    truth = {row["raw_item_name"]: row["true_item"] for row in conn.execute(
        "SELECT DISTINCT raw_item_name, true_item FROM transactions WHERE merchant_id = ?",
        (MERCHANT_ID,))}
    score = generate.score_mapping(mapping, truth)

    print("\nItem resolution: %d of %d raw names clustered correctly, accuracy %.1f percent"
          % (score["correct"], score["raw_names"], 100 * score["accuracy"]))
    print("  %d true products, %d canonical keys produced"
          % (score["true_items"], score["canonical_keys"]))
    for true_item, key in sorted(score["majority_key_per_true_item"].items()):
        print("  %-16s -> %s" % (true_item, key))
    if score["collisions"]:
        print("  collisions: %s" % json.dumps(score["collisions"], ensure_ascii=False))

    assert len(mapping) == len(set(raw_names)), "every raw name must be mapped"
    assert score["accuracy"] >= 0.90


# ---------------------------------------------------------------- offline replay


def test_run_night_works_in_replay_with_no_network(tmp_path, no_network, monkeypatch):
    """Demo day insurance: the whole nightly loop with the network unplugged."""
    monkeypatch.setenv("LLM_MODE", "replay")
    log_path = tmp_path / "decisions.jsonl"

    exit_code = run_night.main(["--merchant", MERCHANT_ID, "--llm-mode", "replay",
                                "--log", str(log_path), "--json"])
    assert exit_code == 0

    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    stages = [line["stage"] for line in lines]
    assert stages == ["triage", "generate", "simulate"]
    assert lines[1]["llm_mode"] == "replay"


def test_the_brief_carries_a_filled_script_and_the_simulator_numbers(tmp_path, no_network,
                                                                     monkeypatch):
    monkeypatch.setenv("LLM_MODE", "replay")
    log_path = tmp_path / "decisions.jsonl"
    run_night.main(["--merchant", MERCHANT_ID, "--llm-mode", "replay",
                    "--log", str(log_path), "--json"])
    simulate_line = [json.loads(line) for line in
                     log_path.read_text(encoding="utf-8").splitlines()][-1]
    brief = simulate_line["brief"]
    assert brief is not None

    # The template the model wrote carries no numbers at all.
    assert generate.number_violations(brief["script_template"]) == []
    # The filled script does, because code put them there.
    assert generate.number_violations(brief["script"]) != []

    numbers = brief["numbers"]
    assert numbers["lapsed_count"] == 12
    assert numbers["treated_count"] + numbers["holdout_count"] == numbers["segment_size"]
    assert numbers["holdout_share"] == pytest.approx(0.15)
    assert str(numbers["lapsed_count"]) in brief["script"]


def test_replay_refuses_to_invent_an_answer_it_has_not_cached(no_network, monkeypatch):
    monkeypatch.setenv("LLM_MODE", "replay")

    class Anything(llm.BaseModel):
        ok: bool

    with pytest.raises(llm.LLMUnavailable):
        llm.complete([{"role": "user", "content": "a prompt nobody has ever recorded"}],
                     Anything, label="never_seen")


# ---------------------------------------------------------------- safe parsing


def test_code_fences_are_stripped_without_a_regex():
    assert llm.strip_code_fences('```json\n{"ok": true}\n```') == '{"ok": true}'
    assert llm.strip_code_fences('{"ok": true}') == '{"ok": true}'
    assert llm.parse_json('```\n{"a": 1}\n```') == {"a": 1}


def test_unparseable_output_raises_rather_than_being_scraped():
    with pytest.raises(llm.LLMInvalidOutput):
        llm.parse_json("I think the answer is probably about 12 customers")
