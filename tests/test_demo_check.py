"""The demo day preflight. Offline, so it can be trusted the morning it matters.

The preflight itself talks to a running server. These tests drive its pieces against a
TestClient and a temporary memory, so nothing here needs uvicorn or the network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scripts import demo_check

MERCHANT_ID = "tea_stall_01"


@pytest.fixture
def client(no_network, tmp_path, monkeypatch):
    from api import main
    monkeypatch.setattr(main.run_night, "LOG_PATH", str(tmp_path / "decisions.jsonl"))
    monkeypatch.setenv("DUKAAN_MEMORY_PATH", str(tmp_path / "memory.db"))
    main._ANALYSIS_CACHE.clear()
    main.local_soundbox.PENDING.clear()
    return TestClient(main.app)


@pytest.fixture
def through(client, monkeypatch):
    """Points demo_check's httpx calls at the in process app instead of a real socket."""
    class Routed:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            return client.get(_path(url))

        def post(self, url, **kwargs):
            return client.post(_path(url), json=kwargs.get("json"))

    def _path(url):
        for host in ("http://localhost:8000", "http://testserver"):
            if url.startswith(host):
                return url[len(host):]
        return url[url.index("/", len("https://")):] if "://" in url else url

    monkeypatch.setattr(demo_check.httpx, "Client", Routed)
    return client


# ---------------------------------------------------------------- the report


def test_a_clean_report_passes():
    report = demo_check.Report()
    report.passed("all good")
    report.warned("a bit odd")
    assert report.failures == []
    assert "PASS" in report.render()


def test_one_failure_is_enough_to_fail():
    report = demo_check.Report()
    report.passed("fine")
    report.failed("broken", "because")
    assert len(report.failures) == 1
    assert report.failures[0]["detail"] == "because"


def test_a_warning_never_fails_the_run():
    """A already answered call is worth flagging, not worth blocking the demo over."""
    report = demo_check.Report()
    report.warned("odd but survivable")
    assert report.failures == []


# ---------------------------------------------------------------- the checks


def test_the_api_check_reports_the_modes(through):
    report = demo_check.Report()
    health = demo_check.check_api(report, "http://localhost:8000")
    assert health is not None
    assert report.failures == []
    assert "bulbul" in report.rows[0]["detail"]


def test_the_api_check_fails_and_says_how_to_start_it(monkeypatch):
    import httpx

    def refuse(*args, **kwargs):
        raise httpx.ConnectError("nothing listening")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    report = demo_check.Report()
    assert demo_check.check_api(report, "http://localhost:8000") is None
    assert report.failures
    assert "uvicorn" in report.failures[0]["detail"]


def test_the_ledger_check_finds_the_twelve(no_network):
    report = demo_check.Report()
    context = demo_check.check_ledger(report, MERCHANT_ID)
    assert context is not None
    assert report.failures == []
    assert any("12 ids match" in row["detail"] for row in report.rows)


def test_the_memory_check_fails_on_an_empty_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("DUKAAN_MEMORY_PATH", str(tmp_path / "empty.db"))
    report = demo_check.Report()
    demo_check.check_memory(report, MERCHANT_ID, expected_campaigns=6)
    assert report.failures
    assert "run_campaigns" in report.failures[0]["detail"]


def test_the_memory_check_passes_with_the_expected_campaigns(tmp_path, monkeypatch,
                                                             no_network):
    path = str(tmp_path / "six.db")
    monkeypatch.setenv("DUKAAN_MEMORY_PATH", path)
    from scripts import run_campaigns
    run_campaigns.run(MERCHANT_ID, 6, path, fresh=True, use_fixtures=True)

    report = demo_check.Report()
    demo_check.check_memory(report, MERCHANT_ID, expected_campaigns=6)
    assert report.failures == []
    assert any("6 measured" in row["detail"] for row in report.rows)


def test_the_memory_check_warns_rather_than_fails_on_a_short_run(tmp_path, monkeypatch,
                                                                 no_network):
    path = str(tmp_path / "two.db")
    monkeypatch.setenv("DUKAAN_MEMORY_PATH", path)
    from scripts import run_campaigns
    run_campaigns.run(MERCHANT_ID, 2, path, fresh=True, use_fixtures=True)

    report = demo_check.Report()
    demo_check.check_memory(report, MERCHANT_ID, expected_campaigns=6)
    assert report.failures == [], "a short curve should not block the demo"
    assert any(row["status"] == demo_check.WARN for row in report.rows)


def test_the_open_call_check_fails_when_no_call_is_open(through):
    report = demo_check.Report()
    demo_check.check_open_call(report, "http://localhost:8000")
    assert report.failures
    assert "/call" in report.failures[0]["detail"]


def test_the_open_call_check_passes_once_a_call_is_open(through):
    through.post("/call", json={"merchant_id": MERCHANT_ID})
    report = demo_check.Report()
    demo_check.check_open_call(report, "http://localhost:8000")
    assert report.failures == []
    assert any("waiting for the merchant" in row["detail"] for row in report.rows)


def test_an_already_answered_call_warns_rather_than_fails(through):
    call = through.post("/call", json={"merchant_id": MERCHANT_ID}).json()
    through.post("/soundbox/button", json={"call_id": call["call_id"], "answer": "haan"})

    report = demo_check.Report()
    demo_check.check_open_call(report, "http://localhost:8000")
    assert report.failures == []
    assert any(row["status"] == demo_check.WARN for row in report.rows)
    assert any("open a fresh one" in row["detail"] for row in report.rows)


# ---------------------------------------------------------------- the tunnel


def test_the_tunnel_check_probes_without_dispatching_anything(through, tmp_path):
    """approved false on purpose: it proves the route without messaging a single customer."""
    from memory import store

    through.post("/call", json={"merchant_id": MERCHANT_ID})
    report = demo_check.Report()
    demo_check.check_tunnel(report, "http://localhost:8000", MERCHANT_ID)

    assert report.failures == []
    assert any("nothing dispatched" in row["detail"] for row in report.rows)

    memory = store.connect()
    try:
        campaigns = memory.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    finally:
        memory.close()
    assert campaigns == 0, "the preflight launched a real campaign"


def test_the_tunnel_check_fails_when_the_tunnel_is_down(monkeypatch):
    import httpx

    def refuse(*args, **kwargs):
        raise httpx.ConnectError("tunnel is down")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    report = demo_check.Report()
    demo_check.check_tunnel(report, "https://nothing.trycloudflare.com", MERCHANT_ID)
    assert report.failures
    assert "trycloudflare" in report.failures[0]["detail"]


# ---------------------------------------------------------------- the exit code


def test_main_exits_non_zero_when_something_fails(monkeypatch, capsys):
    def broken(*args, **kwargs):
        report = demo_check.Report()
        report.failed("something", "went wrong")
        return report

    monkeypatch.setattr(demo_check, "run", broken)
    assert demo_check.main([]) == 1
    assert "CHECK(S) FAILED" in capsys.readouterr().out


def test_main_exits_zero_when_everything_passes(monkeypatch, capsys):
    def clean(*args, **kwargs):
        report = demo_check.Report()
        report.passed("all good")
        return report

    monkeypatch.setattr(demo_check, "run", clean)
    assert demo_check.main([]) == 0
    assert "ready" in capsys.readouterr().out.lower()


def test_the_tunnel_argument_is_optional(monkeypatch):
    seen = {}

    def capture(base_url, merchant_id, tunnel, expected):
        seen["tunnel"] = tunnel
        return demo_check.Report()

    monkeypatch.setattr(demo_check, "run", capture)
    demo_check.main([])
    assert seen["tunnel"] is None
    demo_check.main(["--tunnel", "https://x.trycloudflare.com"])
    assert seen["tunnel"] == "https://x.trycloudflare.com"
