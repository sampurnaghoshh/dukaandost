"""The memory interface and its two backends.

The property that matters for demo day: sqlite is the default, and nothing the loop produces
changes when the backend does. A memory layer that could move a number would be a liability.
"""

from __future__ import annotations

import pytest

from memory import graph, store

MERCHANT_ID = "tea_stall_01"


@pytest.fixture(scope="module")
def six_campaign_memory(tmp_path_factory):
    """A memory with six measured campaigns, built offline."""
    import httpx

    path = str(tmp_path_factory.mktemp("graph") / "memory.db")
    saved = (httpx.HTTPTransport.handle_request,
             httpx.AsyncHTTPTransport.handle_async_request)

    def blocked(*args, **kwargs):
        raise AssertionError("the graph tests tried to reach the network")

    httpx.HTTPTransport.handle_request = blocked
    httpx.AsyncHTTPTransport.handle_async_request = blocked
    try:
        from scripts import run_campaigns
        run_campaigns.run(MERCHANT_ID, 6, path, fresh=True, use_fixtures=True)
    finally:
        (httpx.HTTPTransport.handle_request,
         httpx.AsyncHTTPTransport.handle_async_request) = saved
    return path


# ---------------------------------------------------------------- the default


def test_sqlite_is_the_default_backend(monkeypatch):
    """The demo path must never depend on cognee being installed."""
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    assert graph.backend_name() == graph.SQLITE
    assert graph.DEFAULT_BACKEND == graph.SQLITE


def test_asking_for_cognee_falls_back_rather_than_failing(monkeypatch, six_campaign_memory):
    """If cognee cannot start, the caller still gets a working memory and is told why."""
    monkeypatch.setenv("MEMORY_BACKEND", graph.COGNEE)
    memory, note = graph.open_memory(path=six_campaign_memory)
    try:
        assert memory.name in (graph.SQLITE, graph.COGNEE)
        if memory.name == graph.SQLITE:
            assert "cognee could not start" in note
        assert len(memory.merchant_history(MERCHANT_ID)) == 6
    finally:
        memory.close()


def test_an_unknown_backend_name_gives_sqlite(monkeypatch, six_campaign_memory):
    monkeypatch.setenv("MEMORY_BACKEND", "neo4j_someday")
    memory, note = graph.open_memory(path=six_campaign_memory)
    try:
        assert memory.name == graph.SQLITE
    finally:
        memory.close()


# ---------------------------------------------------------------- the interface


def test_history_carries_proposed_predicted_and_delivered(six_campaign_memory):
    memory = graph.SqliteGraph(path=six_campaign_memory)
    try:
        history = memory.merchant_history(MERCHANT_ID)
        assert len(history) == 6
        assert [row["sequence"] for row in history] == [1, 2, 3, 4, 5, 6]

        for row in history:
            assert row["proposed"]["action_type"]
            assert row["proposed"]["segment_kind"]
            assert row["proposed"]["offer_level"]
            assert row["predicted"]["uplift"] is not None
            assert row["delivered"]["lift"] is not None
            assert row["approved"] is True
    finally:
        memory.close()


def test_campaigns_like_filters_on_action_type_and_segment(six_campaign_memory):
    memory = graph.SqliteGraph(path=six_campaign_memory)
    try:
        matching = memory.campaigns_like(MERCHANT_ID, action_type="lapsed_winback",
                                         segment_kind="lapsed_regulars")
        assert len(matching) == 6

        assert memory.campaigns_like(MERCHANT_ID, action_type="offpeak_fill") == []
        assert memory.campaigns_like(MERCHANT_ID, segment_kind="anchor_buyers") == []
        assert memory.campaigns_like(MERCHANT_ID, offer_level="HIGH") == []
    finally:
        memory.close()


def test_write_campaign_outcome_returns_the_finished_record(six_campaign_memory):
    memory = graph.SqliteGraph(path=six_campaign_memory)
    try:
        history = memory.merchant_history(MERCHANT_ID)
        record = memory.write_campaign_outcome(MERCHANT_ID, history[0]["campaign_id"])
        assert record["campaign_id"] == history[0]["campaign_id"]
        assert record["delivered"]["lift"] is not None
    finally:
        memory.close()


def test_an_unknown_campaign_raises_rather_than_returning_nothing(six_campaign_memory):
    memory = graph.SqliteGraph(path=six_campaign_memory)
    try:
        with pytest.raises(LookupError):
            memory.write_campaign_outcome(MERCHANT_ID, "camp_never_happened")
    finally:
        memory.close()


# ---------------------------------------------------------------- both backends agree


def test_both_backends_return_the_same_history(six_campaign_memory):
    """The headline property. Swapping the backend must not move a single field."""
    readiness = graph.cognee_readiness()
    sqlite_memory = graph.SqliteGraph(path=six_campaign_memory)
    try:
        expected = sqlite_memory.merchant_history(MERCHANT_ID)

        if not readiness["ready"]:
            # cognee cannot start here, so the comparison is against what open_memory
            # actually hands back when asked for it, which is the fallback.
            fallback, _note = graph.open_memory(path=six_campaign_memory,
                                                backend=graph.COGNEE)
            try:
                assert fallback.merchant_history(MERCHANT_ID) == expected
            finally:
                fallback.close()
            pytest.skip("cognee not ready here: %s" % "; ".join(readiness["blockers"]))

        cognee_memory = graph.CogneeGraph(path=six_campaign_memory)
        try:
            assert cognee_memory.merchant_history(MERCHANT_ID) == expected
            assert (cognee_memory.campaigns_like(MERCHANT_ID, action_type="lapsed_winback")
                    == sqlite_memory.campaigns_like(MERCHANT_ID,
                                                    action_type="lapsed_winback"))
        finally:
            cognee_memory.close()
    finally:
        sqlite_memory.close()


# ---------------------------------------------------------------- isolation


def test_the_loop_does_not_read_the_graph_module():
    """Triage, generation, simulation and learning must be unaware the backend exists.

    If any of them imported this, the backend could change a number, and the arithmetic
    would stop being reproducible.
    """
    import inspect

    from core import generate, holdout, measure, simulate, triage
    for module in (triage, generate, simulate, measure, holdout):
        source = inspect.getsource(module)
        assert "memory.graph" not in source, "%s reads the graph" % module.__name__
        assert "MEMORY_BACKEND" not in source, (
            "%s branches on the backend" % module.__name__)


def test_the_learned_uplift_is_identical_whichever_backend_is_set(monkeypatch,
                                                                  six_campaign_memory):
    """The number that drives the next campaign cannot depend on the storage choice."""
    from core import simulate

    memory = store.connect(six_campaign_memory)
    try:
        monkeypatch.setenv("MEMORY_BACKEND", graph.SQLITE)
        with_sqlite = store.learned_response_scale(memory, MERCHANT_ID,
                                                   simulate.PRIOR_OFFER_UPLIFT)
        monkeypatch.setenv("MEMORY_BACKEND", graph.COGNEE)
        with_cognee = store.learned_response_scale(memory, MERCHANT_ID,
                                                   simulate.PRIOR_OFFER_UPLIFT)
        assert with_sqlite == with_cognee
    finally:
        memory.close()


# ---------------------------------------------------------------- the readiness report


def test_readiness_names_what_is_missing_rather_than_just_failing():
    readiness = graph.cognee_readiness()
    assert set(readiness) >= {"installed", "llm", "embeddings", "ready", "blockers"}
    if not readiness["ready"]:
        assert readiness["blockers"], "a not ready report must say why"
