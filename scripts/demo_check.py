"""Preflight for demo day. One command that says whether the demo will work.

    python -m scripts.demo_check
    python -m scripts.demo_check --tunnel https://something.trycloudflare.com

Without a tunnel it checks the local side: the API is up, the ledger and the twelve lapsed
regulars are intact, campaign memory is in the state the demo expects, the text to speech
cache is warm enough for replay to run with the network unplugged, and a call is open and
waiting for the merchant to answer.

With a tunnel it also checks the outside world can reach the two things that have to be
reachable: the dashboard, and the campaign launch endpoint the Sarvam agent tool hits
mid call. The launch probe deliberately sends approved false, so it proves routing without
dispatching anything to anybody.

Exits non zero if anything fails, so it can be trusted at nine in the morning.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from core import ledger

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_MERCHANT = "tea_stall_01"
EXPECTED_LAPSED = 12
EXPECTED_VALUE_AT_RISK = 9400.0
VALUE_TOLERANCE = 0.15
DEFAULT_EXPECTED_CAMPAIGNS = 6
TIMEOUT = 20.0
TUNNEL_TIMEOUT = 45.0

TICK = "PASS"
CROSS = "FAIL"
WARN = "WARN"


class Report:
    """Collects one line per check. Warnings are printed but do not fail the run."""

    def __init__(self) -> None:
        self.rows: list = []

    def add(self, status: str, label: str, detail: str = "") -> None:
        self.rows.append({"status": status, "label": label, "detail": detail})

    def passed(self, label: str, detail: str = "") -> None:
        self.add(TICK, label, detail)

    def failed(self, label: str, detail: str = "") -> None:
        self.add(CROSS, label, detail)

    def warned(self, label: str, detail: str = "") -> None:
        self.add(WARN, label, detail)

    @property
    def failures(self) -> list:
        return [row for row in self.rows if row["status"] == CROSS]

    def render(self) -> str:
        lines = []
        for row in self.rows:
            lines.append("  %-4s  %-46s %s" % (row["status"], row["label"], row["detail"]))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Local checks
# --------------------------------------------------------------------------


def check_api(report: Report, base_url: str) -> dict | None:
    """Is the service up, and which mode is it in."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get("%s/health" % base_url)
    except httpx.HTTPError as exc:
        report.failed("API answers on %s" % base_url,
                      "%s. Start it: uvicorn api.main:app --port 8000"
                      % type(exc).__name__)
        return None

    if response.status_code != 200:
        report.failed("API answers on %s" % base_url, "HTTP %d" % response.status_code)
        return None

    health = response.json()
    report.passed("API answers on %s" % base_url,
                  "%s, llm %s, tts %s, stt %s"
                  % (health["status"], health["llm_mode"], health["tts_model"],
                     health["stt_model"]))
    if not health.get("key_present"):
        report.warned("Sarvam key present",
                      "no key in the environment, live calls will fail, replay still works")
    else:
        report.passed("Sarvam key present", "loaded, never printed")
    return health


def check_ledger(report: Report, merchant_id: str) -> dict | None:
    """The ledger is there and triage still finds exactly the twelve the demo talks about."""
    from core import triage

    truth_path = os.path.join(ledger.REPO_ROOT, "data", "ground_truth.json")
    if not os.path.exists(ledger.DEFAULT_DB_PATH):
        report.failed("Ledger present", "no data/dukaan.db, run python -m data.generate")
        return None
    if not os.path.exists(truth_path):
        report.failed("Ground truth present", "no data/ground_truth.json")
        return None

    with open(truth_path, encoding="utf-8") as handle:
        truth = json.load(handle)

    conn = ledger.connect()
    try:
        today = ledger.data_as_of(conn, merchant_id)
        result = triage.score(merchant_id, today, conn=conn)
        shop_name = ledger.merchant(conn, merchant_id)["name"]
    finally:
        conn.close()

    lapsed = result["signals"]["lapsed_regulars"]
    found = sorted(lapsed["customer_ids"])
    expected = sorted(truth["lapsed_customer_ids"])

    if found != expected:
        report.failed("Triage finds the same twelve customers",
                      "found %d, expected %d, regenerate with python -m data.generate"
                      % (len(found), len(expected)))
    else:
        report.passed("Triage finds the same twelve customers",
                      "%d ids match data/ground_truth.json" % len(found))

    value = lapsed["value_at_risk_monthly"]
    drift = abs(value - EXPECTED_VALUE_AT_RISK) / EXPECTED_VALUE_AT_RISK
    if lapsed["count"] != EXPECTED_LAPSED or drift > VALUE_TOLERANCE:
        report.failed("Value at risk near the number you say on stage",
                      "%d customers worth %.0f" % (lapsed["count"], value))
    else:
        report.passed("Value at risk near the number you say on stage",
                      "%d customers worth %.0f, the pitch says about %.0f"
                      % (lapsed["count"], value, EXPECTED_VALUE_AT_RISK))

    if not result["worth_a_call"]:
        report.failed("Triage says tonight is worth a call",
                      "it wants to stay silent, the demo has nothing to show")
    else:
        report.passed("Triage says tonight is worth a call",
                      "opportunity %.0f a month" % result["opportunity_score"])

    return {"triage": result, "today": today, "shop_name": shop_name}


def check_memory(report: Report, merchant_id: str, expected_campaigns: int) -> None:
    """Campaign memory in the state the demo expects, with the count printed either way."""
    from memory import store

    memory = store.connect()
    try:
        campaigns = memory.execute("SELECT COUNT(*) FROM campaigns WHERE merchant_id = ?",
                                   (merchant_id,)).fetchone()[0]
        measured = memory.execute("SELECT COUNT(*) FROM measurements WHERE merchant_id = ?",
                                  (merchant_id,)).fetchone()[0]
        series = store.learning_series(memory, merchant_id)
    finally:
        memory.close()

    detail = "%d campaigns, %d measured" % (campaigns, measured)
    if measured == expected_campaigns:
        report.passed("Campaign memory in the expected state",
                      "%s, the learning curve has %d points" % (detail, len(series)))
    elif measured == 0:
        report.failed("Campaign memory in the expected state",
                      "%s. Panel six will be empty. Run: python -m scripts.run_campaigns"
                      % detail)
    else:
        report.warned("Campaign memory in the expected state",
                      "%s, expected %d. The curve will be short but the demo runs."
                      % (detail, expected_campaigns))

    if series:
        first, last = series[0], series[-1]
        report.passed("Learning curve climbs",
                      "campaign %d believed %.3f, campaign %d believes %.3f"
                      % (first["sequence"], first["predicted_uplift"],
                         last["sequence"], last["predicted_uplift"]))


def check_audio_cache(report: Report, merchant_id: str, context: dict | None) -> None:
    """Replay has to be able to speak the script with the network unplugged.

    This walks the whole offline path on purpose: generation from the LLM cache, pricing,
    the brief, then the text to speech cache. If every one of those resolves from disk, the
    venue wifi cannot take the demo down.
    """
    from core import generate, run_night, simulate, speech
    from core.llm import LLMError
    from memory import store

    if context is None:
        report.failed("Replay can speak the script offline", "skipped, the ledger failed")
        return

    previous = os.environ.get("LLM_MODE")
    os.environ["LLM_MODE"] = "replay"
    conn = ledger.connect()
    memory = store.connect()
    try:
        generated = run_night.generate_candidates(conn, merchant_id, context["triage"])
        ranking = simulate.rank(merchant_id, generated["candidates"], context["triage"],
                                today=context["today"], conn=conn, memory=memory)
        chosen = ranking["recommended"]
        if chosen is None:
            report.failed("Replay can speak the script offline",
                          "nothing clears the guardrails, so there is no script")
            return

        script = generate.call_script()
        brief = run_night.build_brief(merchant_id, context["shop_name"], context["triage"],
                                      chosen, script)
        audio = speech.speak(brief.script, language_code=brief.language)
    except LLMError as exc:
        report.failed("Replay can speak the script offline",
                      "%s. Warm it: set LLM_MODE=record and run python -m core.run_night"
                      % type(exc).__name__)
        return
    except Exception as exc:
        report.failed("Replay can speak the script offline",
                      "%s: %s" % (type(exc).__name__, exc))
        return
    finally:
        conn.close()
        memory.close()
        if previous is None:
            os.environ.pop("LLM_MODE", None)
        else:
            os.environ["LLM_MODE"] = previous

    size = os.path.getsize(audio["audio_path"]) if os.path.exists(audio["audio_path"]) else 0
    if audio["source"] != "cache" or size == 0:
        report.failed("Replay can speak the script offline",
                      "audio came from %s, not the cache" % audio["source"])
    else:
        report.passed("Replay can speak the script offline",
                      "%d KB of wav from cache, %s at %s"
                      % (size // 1024, audio["model"], chosen["estimates"]["offer_level"]))


def check_open_call(report: Report, base_url: str) -> None:
    """A call has to be open and unanswered, or there is nothing to say haan to."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get("%s/dashboard/state" % base_url)
    except httpx.HTTPError as exc:
        report.failed("A call is open and waiting", type(exc).__name__)
        return

    if response.status_code != 200:
        report.failed("A call is open and waiting", "HTTP %d" % response.status_code)
        return

    call = response.json().get("call")
    if call is None:
        report.failed("A call is open and waiting",
                      "none open. Open one: Invoke-RestMethod -Method Post "
                      "-Uri %s/call -ContentType 'application/json' -Body '{}'" % base_url)
        return

    if call["settled"]:
        report.warned("A call is open and waiting",
                      "%s is already answered as %s, open a fresh one before you present"
                      % (call["call_id"], call["outcome"]))
    else:
        report.passed("A call is open and waiting",
                      "%s, waiting for the merchant" % call["call_id"])

    if not call.get("has_audio"):
        report.failed("The open call has audio to play",
                      call.get("audio_error") or "no audio rendered, the buttons still work")
    else:
        report.passed("The open call has audio to play",
                      "soundbox at %s/soundbox?call_id=%s" % (base_url, call["call_id"]))


# --------------------------------------------------------------------------
# Tunnel checks
# --------------------------------------------------------------------------


def check_tunnel(report: Report, tunnel: str, merchant_id: str) -> None:
    """Can the outside world reach the two things that have to be reachable."""
    tunnel = tunnel.rstrip("/")

    try:
        with httpx.Client(timeout=TUNNEL_TIMEOUT, follow_redirects=True) as client:
            health = client.get("%s/health" % tunnel)
            dashboard = client.get("%s/dashboard" % tunnel)
            launch = client.post(
                "%s/campaign/launch" % tunnel,
                json={"merchant_id": merchant_id, "approved": False,
                      "source": "demo_check_preflight"})
    except httpx.HTTPError as exc:
        report.failed("Tunnel reaches the API", "%s at %s" % (type(exc).__name__, tunnel))
        return

    if health.status_code == 200:
        report.passed("Tunnel reaches the API", "%s answers health" % tunnel)
    else:
        report.failed("Tunnel reaches the API", "health returned %d" % health.status_code)

    if dashboard.status_code == 200 and "Dukaan Dost" in dashboard.text:
        report.passed("Tunnel serves the judge dashboard", "%s" % tunnel)
    else:
        report.failed("Tunnel serves the judge dashboard",
                      "HTTP %d" % dashboard.status_code)

    # approved false on purpose: proves the route the Sarvam tool hits without sending
    # anything to anybody.
    if launch.status_code == 200 and launch.json().get("status") == "declined":
        report.passed("Tunnel reaches the launch endpoint",
                      "%s/campaign/launch answered, nothing dispatched" % tunnel)
    else:
        report.failed("Tunnel reaches the launch endpoint",
                      "HTTP %d, %s" % (launch.status_code, launch.text[:80]))


# --------------------------------------------------------------------------


def run(base_url: str = DEFAULT_BASE_URL, merchant_id: str = DEFAULT_MERCHANT,
        tunnel: str | None = None,
        expected_campaigns: int = DEFAULT_EXPECTED_CAMPAIGNS) -> Report:
    report = Report()
    check_api(report, base_url)
    context = check_ledger(report, merchant_id)
    check_memory(report, merchant_id, expected_campaigns)
    check_audio_cache(report, merchant_id, context)
    check_open_call(report, base_url)
    if tunnel:
        check_tunnel(report, tunnel, merchant_id)
    return report


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Demo day preflight.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--merchant", default=DEFAULT_MERCHANT)
    parser.add_argument("--tunnel", default=None,
                        help="cloudflared URL, checks the outside world can reach the demo")
    parser.add_argument("--expect-campaigns", type=int, default=DEFAULT_EXPECTED_CAMPAIGNS)
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    report = run(args.base_url, args.merchant, args.tunnel, args.expect_campaigns)

    print("")
    print("Dukaan Dost preflight")
    print("=" * 78)
    print(report.render())
    print("")
    if report.failures:
        print("  %d CHECK(S) FAILED. Fix these before you present:" % len(report.failures))
        for row in report.failures:
            print("    %s: %s" % (row["label"], row["detail"]))
        return 1
    if not args.tunnel:
        print("  Local side ready. Run again with --tunnel to check the outside world.")
    else:
        print("  Ready. Local side good and the tunnel reaches the demo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
