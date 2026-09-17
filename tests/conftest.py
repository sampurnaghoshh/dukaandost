"""Shared pytest fixtures. The ledger is read once and reused across the suite."""

from __future__ import annotations

import json
import os

import pytest

from core import ledger, triage

GROUND_TRUTH_PATH = os.path.join(ledger.REPO_ROOT, "data", "ground_truth.json")
MERCHANT_ID = "tea_stall_01"


@pytest.fixture(scope="session")
def conn():
    if not os.path.exists(ledger.DEFAULT_DB_PATH):
        pytest.skip("ledger missing, run python -m data.generate")
    connection = ledger.connect()
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def ground_truth():
    """Evaluation only. Read by the test suite and by scripts/check_data.py, nothing else."""
    with open(GROUND_TRUTH_PATH, encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="session")
def triage_result(conn):
    return triage.score(MERCHANT_ID, conn=conn)


@pytest.fixture
def no_network(monkeypatch):
    """Hard blocks HTTP so a test cannot quietly reach Sarvam.

    The suite must be runnable on a plane, on venue wifi, and without spending credit.
    Anything needing the model reads the recorded cache in data/llm_cache.
    """
    import httpx

    def blocked(*args, **kwargs):
        raise AssertionError("this test tried to reach the network")

    monkeypatch.setattr(httpx.Client, "post", blocked)
    monkeypatch.setattr(httpx, "post", blocked, raising=False)
    monkeypatch.setenv("LLM_MODE", "replay")
    return blocked
