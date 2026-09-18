"""FastAPI app, including the endpoint the voice agent calls mid conversation.

Every stage of the nightly loop is one endpoint, so on Friday the n8n workflow is a chain of
HTTP nodes rather than a cron wrapped around a Python script:

    GET  /health            is the service up, which mode is it in
    POST /triage            opportunity score and evidence
    POST /generate          LLM candidates, or the fixtures if nothing valid comes back
    POST /simulate          the level grid, the ranking and the refusals
    POST /call              renders the script, opens a soundbox call, returns the Decision
    POST /campaign/launch   the mid call endpoint the Sarvam agent tool hits in Step 5
    POST /measure           stubbed until Step 5, answers not_ready
    GET  /decisions         tails logs/decisions.jsonl for the judge dashboard

    GET  /soundbox          the browser page that plays the script and takes the answer
    POST /soundbox/reply    microphone audio in, Decision or reprompt out
    POST /soundbox/button   the Haan and Nahi buttons

Every endpoint validates with pydantic and appends one line to the decision log.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime

import httpx

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from core import (campaign as campaign_flow, dispatch, fixtures, generate, holdout, ledger,
                  llm, run_night, simulate, speech, triage)
from core.holdout import HOLDOUT_SHARE
from memory import store
from voice import local_soundbox
from voice.base import Brief

app = FastAPI(title="Dukaan Dost", version="0.4.0",
              description="An autonomous growth analyst for Paytm merchants.")

DEFAULT_MERCHANT = "tea_stall_01"
PAGE_PATH = os.path.join(ledger.REPO_ROOT, "dashboard", "soundbox.html")

# Campaigns approved on a call. Step 5 moves this into memory/store.py.
CAMPAIGNS: dict = {}


# --------------------------------------------------------------------------
# Request and response models
# --------------------------------------------------------------------------


class MerchantRequest(BaseModel):
    merchant_id: str = DEFAULT_MERCHANT
    today: str | None = None


class GenerateRequest(MerchantRequest):
    use_fixtures: bool = False


class SimulateRequest(MerchantRequest):
    candidates: list[dict] | None = None
    discount_spent_this_month: float = 0.0


class CallRequest(MerchantRequest):
    candidates: list[dict] | None = None
    # Where to POST the Decision once the merchant answers. The n8n Wait node generates this
    # and hands it over, so the workflow can pause on a real event instead of polling.
    callback_url: str | None = None


class LaunchRequest(BaseModel):
    merchant_id: str = DEFAULT_MERCHANT
    call_id: str = ""
    candidate_id: str = ""
    approved: bool = True
    transcript: str = ""
    source: str = "voice_agent"


class MeasureRequest(BaseModel):
    campaign_id: str
    merchant_id: str = DEFAULT_MERCHANT
    simulate_outcomes: bool = True


class ReplyRequest(BaseModel):
    call_id: str
    transcript: str | None = None


class ButtonRequest(BaseModel):
    call_id: str
    answer: str = Field(description="haan or nahi")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _log(stage: str, outcome: str, headline: str, **extra) -> None:
    """One decision line per request.

    The log path is read at call time, not bound as a default argument, so a test or a
    different deployment can point it somewhere else and actually be obeyed.
    """
    run_night.log_decision(
        dict(extra, stage=stage, decision=outcome, headline=headline, via="api"),
        run_night.LOG_PATH)


def _notify_callback(call_id: str, decision) -> None:
    """Tells whoever is waiting that the merchant answered. Best effort, never fatal.

    The n8n Wait node parks the whole nightly run on a resume URL. This is what wakes it.
    If nobody is waiting, or the post fails, the call still settled and the log still has it.
    """
    call = local_soundbox.PENDING.get(call_id) or {}
    url = call.get("callback_url")
    if not url:
        return
    payload = json.loads(decision.model_dump_json())
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(url, json=payload)
        _log("call_callback", "delivered", "woke the waiting workflow",
             merchant_id=decision.merchant_id, call_id=call_id)
    except Exception as exc:
        _log("call_callback", "failed", "could not reach the waiting workflow: %s"
             % type(exc).__name__, merchant_id=decision.merchant_id, call_id=call_id)


def _resolve_call_id(merchant_id: str, call_id: str | None) -> tuple:
    """Which call to launch from, and where that answer came from.

    An explicit call id always wins. Without one, the most recently opened call for this
    merchant is used, because the Sarvam agent tool fires mid conversation and does not
    reliably have the id to hand when it does. Only calls that actually have a chosen
    candidate count, since a call with nothing decided has nothing to dispatch.

    The source is returned alongside so the decision log records whether the campaign was
    launched against a call the caller named or one this function picked. A dispatch that
    guessed which call it belonged to should say so.
    """
    if call_id:
        return call_id, "explicit"

    open_calls = [call for call in local_soundbox.PENDING.values()
                  if call.get("merchant_id") == merchant_id and call.get("chosen")]
    if not open_calls:
        return None, "none_open"
    latest = max(open_calls, key=lambda call: call["started_at"])
    return latest["call_id"], "resolved_latest_open"


def _resolve_today(conn, merchant_id: str, today: str | None) -> date:
    return date.fromisoformat(today) if today else ledger.data_as_of(conn, merchant_id)


def _triage(merchant_id: str, today: str | None) -> tuple:
    conn = ledger.connect()
    try:
        when = _resolve_today(conn, merchant_id, today)
        return triage.score(merchant_id, when, conn=conn), when
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Up, and honest about which mode it is in. Never reports anything about the key
    beyond whether one exists."""
    payload = {
        "status": "ok",
        "service": "dukaan-dost",
        "version": app.version,
        "llm_mode": llm.mode(),
        "llm_model": llm.model_id(),
        "tts_model": speech.tts_model(),
        "stt_model": speech.stt_model(),
        "key_present": llm.key_is_present(),
        "holdout_share": HOLDOUT_SHARE,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    _log("health", "ok", "health check", merchant_id=None, **{
        "llm_mode": payload["llm_mode"]})
    return payload


@app.post("/triage")
def post_triage(request: MerchantRequest) -> dict:
    result, _when = _triage(request.merchant_id, request.today)
    _log("triage", result["decision"],
         "worth a call" if result["worth_a_call"] else "no call tonight",
         merchant_id=request.merchant_id, today=result["today"],
         opportunity_score=result["opportunity_score"], signals=result["signals"],
         evidence=result["evidence"])
    return result


@app.post("/generate")
def post_generate(request: GenerateRequest) -> dict:
    conn = ledger.connect()
    try:
        when = _resolve_today(conn, request.merchant_id, request.today)
        triage_result = triage.score(request.merchant_id, when, conn=conn)
        if request.use_fixtures:
            record = {"source": "fixture", "candidates": list(fixtures.PLACEHOLDER_CANDIDATES),
                      "dropped": [], "errors": [], "events": [],
                      "fallback_reason": "caller asked for the fixtures"}
        else:
            record = run_night.generate_candidates(conn, request.merchant_id, triage_result)
    finally:
        conn.close()

    _log("generate", "candidates_ready" if record["candidates"] else "nothing_generated",
         "%d candidates from the %s" % (len(record["candidates"]), record["source"]),
         merchant_id=request.merchant_id, today=triage_result["today"],
         source=record["source"], generated=record["candidates"], dropped=record["dropped"],
         errors=record["errors"], item_resolution=record.get("item_resolution"))
    return {
        "merchant_id": request.merchant_id,
        "today": triage_result["today"],
        "source": record["source"],
        "candidates": record["candidates"],
        "dropped": record["dropped"],
        "errors": record["errors"],
        "item_resolution": record.get("item_resolution"),
    }


@app.post("/simulate")
def post_simulate(request: SimulateRequest) -> dict:
    conn = ledger.connect()
    try:
        when = _resolve_today(conn, request.merchant_id, request.today)
        triage_result = triage.score(request.merchant_id, when, conn=conn)
        candidates = request.candidates
        if candidates is None:
            candidates = run_night.generate_candidates(
                conn, request.merchant_id, triage_result)["candidates"]
        memory = store.connect()
        try:
            ranking = simulate.rank(
                request.merchant_id, candidates, triage_result, today=when, conn=conn,
                memory=memory,
                discount_spent_this_month=request.discount_spent_this_month)
        finally:
            memory.close()
    finally:
        conn.close()

    recommended = ranking["recommended"]
    _log("simulate", "recommend" if recommended else "no_viable_action",
         recommended["title"] if recommended else "nothing clears the guardrails",
         merchant_id=request.merchant_id, today=ranking["today"],
         baseline=ranking["baseline"], guardrails=ranking["guardrails"],
         level_grid=ranking["level_grid"], recommended=recommended,
         ranked=ranking["ranked"], rejected=ranking["rejected"])
    return ranking


@app.post("/call")
def post_call(request: CallRequest) -> dict:
    """Builds the brief from the ranked winner and opens a soundbox call."""
    conn = ledger.connect()
    try:
        when = _resolve_today(conn, request.merchant_id, request.today)
        merchant_name = ledger.merchant(conn, request.merchant_id)["name"]
        triage_result = triage.score(request.merchant_id, when, conn=conn)
        if not triage_result["worth_a_call"]:
            _log("call", "no_call", "triage says this merchant is not worth a call tonight",
                 merchant_id=request.merchant_id, today=triage_result["today"])
            return {"status": "no_call", "reason": "triage threshold not cleared",
                    "triage": triage_result}

        candidates = request.candidates
        if candidates is None:
            candidates = run_night.generate_candidates(
                conn, request.merchant_id, triage_result)["candidates"]
        memory = store.connect()
        try:
            ranking = simulate.rank(request.merchant_id, candidates, triage_result,
                                    today=when, conn=conn, memory=memory)
        finally:
            memory.close()
    finally:
        conn.close()

    recommended = ranking["recommended"]
    if recommended is None:
        _log("call", "no_viable_action", "nothing clears the guardrails, staying silent",
             merchant_id=request.merchant_id, today=ranking["today"])
        return {"status": "no_viable_action", "simulation": ranking}

    script = generate.call_script()
    brief = run_night.build_brief(request.merchant_id, merchant_name, triage_result,
                                  recommended, script)
    decision = local_soundbox.dial(request.merchant_id, brief)
    # The winning candidate travels with the call, so the mid call launch dispatches exactly
    # what the merchant was read out, not a re-derivation of it.
    local_soundbox.PENDING[decision.call_id]["chosen"] = recommended
    local_soundbox.PENDING[decision.call_id]["callback_url"] = request.callback_url

    _log("call", decision.outcome, "soundbox call opened for %s" % recommended["title"],
         merchant_id=request.merchant_id, today=ranking["today"],
         call_id=decision.call_id, candidate_id=brief.candidate_id,
         brief=json.loads(brief.model_dump_json()),
         decision=json.loads(decision.model_dump_json()))
    return {
        "status": "awaiting_reply",
        "call_id": decision.call_id,
        "soundbox_url": "/soundbox?call_id=%s" % decision.call_id,
        "brief": json.loads(brief.model_dump_json()),
        "decision": json.loads(decision.model_dump_json()),
        "simulation": {"recommended": recommended, "level_grid": ranking["level_grid"],
                       "baseline": ranking["baseline"]},
    }


@app.post("/campaign/launch")
def post_campaign_launch(request: LaunchRequest) -> dict:
    """The mid call endpoint. The Sarvam agent's API tool hits this the moment the merchant
    says haan, which is what makes the campaign fire while he is still on the line.

    Assigns the holdout, records what the simulator predicted, and renders a Hindi message
    for every treated customer. Nothing is sent: WhatsApp Business API is the production
    path and needs business verification plus template approval.
    """
    call_id, call_id_source = _resolve_call_id(request.merchant_id, request.call_id)

    if not request.approved:
        _log("campaign_launch", "declined", "merchant declined, nothing dispatched",
             merchant_id=request.merchant_id, call_id=call_id or "",
             call_id_source=call_id_source, candidate_id=request.candidate_id,
             transcript=request.transcript)
        return {"status": "declined", "campaign_id": None, "call_id": call_id,
                "reason": "the merchant did not approve"}

    call = local_soundbox.PENDING.get(call_id) if call_id else None
    brief: Brief | None = call["brief"] if call else None
    chosen = (call or {}).get("chosen")
    if brief is None or chosen is None:
        detail = ("no open call %s to launch from" % call_id if call_id
                  else "no open call for %s to launch from" % request.merchant_id)
        _log("campaign_launch", "no_open_call", detail,
             merchant_id=request.merchant_id, call_id=call_id or "",
             call_id_source=call_id_source)
        raise HTTPException(status_code=404, detail=detail)

    conn = ledger.connect()
    memory = store.connect()
    try:
        shop_name = ledger.merchant(conn, request.merchant_id)["name"]
        triage_result = triage.score(request.merchant_id,
                                     ledger.data_as_of(conn, request.merchant_id), conn=conn)
        lapsed = triage_result["signals"]["lapsed_regulars"]
        customer_ids = list(lapsed["customer_ids"])
        facts = {row["customer_id"]: row for row in lapsed["detail"]}

        launched = campaign_flow.launch(memory, request.merchant_id, chosen, shop_name,
                                        customer_ids, customer_facts=facts,
                                        customer_names=ledger.customer_names(
                                            conn, request.merchant_id))
    finally:
        conn.close()
        memory.close()

    campaign_id = launched["campaign_id"]
    CAMPAIGNS[campaign_id] = {
        "campaign_id": campaign_id,
        "merchant_id": request.merchant_id,
        "call_id": call_id,
        "candidate_id": chosen.get("candidate_id"),
        "approved_at": datetime.now().isoformat(timespec="seconds"),
        "source": request.source,
        "transcript": request.transcript,
        "status": "dispatched_awaiting_measurement",
    }
    _log("campaign_launch", "approved",
         "campaign %s approved on the call, %d messaged and %d held back"
         % (campaign_id, launched["dispatch"]["rendered"],
            launched["assignment"]["holdout_count"]),
         merchant_id=request.merchant_id, call_id=call_id, call_id_source=call_id_source,
         campaign_id=campaign_id,
         candidate_id=chosen.get("candidate_id"), predicted=launched["predicted"],
         assignment={"treated": launched["assignment"]["treated"],
                     "control": launched["assignment"]["control"],
                     "seed": launched["assignment"]["seed"]},
         messages=launched["dispatch"]["messages"], transcript=request.transcript)
    return {
        "status": "approved",
        "campaign_id": campaign_id,
        "call_id": call_id,
        "call_id_source": call_id_source,
        "sequence": launched["sequence"],
        "treated_count": launched["assignment"]["treated_count"],
        "holdout_count": launched["assignment"]["holdout_count"],
        "holdout_share": HOLDOUT_SHARE,
        "assignment_method": launched["assignment"]["method"],
        "predicted": launched["predicted"],
        "dispatch": {key: value for key, value in launched["dispatch"].items()
                     if key != "messages"},
        "messages": launched["dispatch"]["messages"],
        "next": "call POST /measure with this campaign_id after the 72 hour wait",
    }


@app.post("/measure")
def post_measure(request: MeasureRequest) -> dict:
    """Real since Step 5. The n8n Wait node calls this 72 hours after dispatch.

    lift = treated conversion minus control conversion. Nothing here estimates anything.
    """
    memory = store.connect()
    try:
        record = store.campaign(memory, request.campaign_id)
        if record is None:
            _log("measure", "unknown_campaign", "no such campaign",
                 merchant_id=request.merchant_id, campaign_id=request.campaign_id)
            raise HTTPException(status_code=404,
                                detail="no campaign %s in memory" % request.campaign_id)

        if request.simulate_outcomes and not store.outcomes(memory, request.campaign_id):
            # Seventy two hours have not really passed. The simulated world stands in.
            assignment = store.assignments(memory, request.campaign_id)
            conn = ledger.connect()
            try:
                triage_result = triage.score(request.merchant_id,
                                             ledger.data_as_of(conn, request.merchant_id),
                                             conn=conn)
            finally:
                conn.close()
            lapsed = triage_result["signals"]["lapsed_regulars"]
            value_each = lapsed["value_at_risk_monthly"] / max(1, lapsed["count"])
            campaign_flow.observe(memory, request.campaign_id, assignment,
                                  record["offer_level"], value_each)

        try:
            measurement = campaign_flow.close(memory, request.campaign_id)
        except LookupError as exc:
            _log("measure", "not_ready", str(exc), merchant_id=request.merchant_id,
                 campaign_id=request.campaign_id)
            return {"status": "not_ready", "campaign_id": request.campaign_id,
                    "reason": str(exc)}

        learned = store.learned_response_scale(
            memory, request.merchant_id, simulate.PRIOR_OFFER_UPLIFT,
            baseline_rate=store.last_baseline_rate(memory, request.merchant_id))
        _log("measure", "measured",
             "lift %.3f, %d of %d treated returned against %d of %d held back"
             % (measurement["lift"], measurement["treated_returns"], measurement["treated_n"],
                measurement["control_returns"], measurement["control_n"]),
             merchant_id=request.merchant_id, campaign_id=request.campaign_id,
             measurement=measurement, learned=learned)
        return {"status": "measured", "measurement": measurement, "learned": learned,
                "measured_at_hours": 72}
    finally:
        memory.close()


@app.get("/learning")
def get_learning(merchant_id: str = DEFAULT_MERCHANT) -> dict:
    """Predicted against actual, campaign by campaign. The chart the judges see."""
    memory = store.connect()
    try:
        series = store.learning_series(memory, merchant_id)
        learned = store.learned_response_scale(
            memory, merchant_id, simulate.PRIOR_OFFER_UPLIFT,
            baseline_rate=store.last_baseline_rate(memory, merchant_id))
        first = series[0] if series else None
        last = series[-1] if series else None
        return {
            "merchant_id": merchant_id,
            "campaigns": len(series),
            "series": series,
            "learned_response_scale": learned,
            "priors": simulate.PRIOR_OFFER_UPLIFT,
            "headline": (
                "campaign %d believed %.3f, campaign %d believes %.3f"
                % (first["sequence"], first["predicted_uplift"],
                   last["sequence"], last["predicted_uplift"]) if series else
                "no campaigns measured yet"),
        }
    finally:
        memory.close()


@app.get("/grid")
def get_grid(merchant_id: str = DEFAULT_MERCHANT, today: str | None = None) -> dict:
    """Candidate by offer level, with what the model proposed and what the simulator picked."""
    conn = ledger.connect()
    memory = store.connect()
    try:
        when = _resolve_today(conn, merchant_id, today)
        triage_result = triage.score(merchant_id, when, conn=conn)
        record = run_night.generate_candidates(conn, merchant_id, triage_result)
        ranking = simulate.rank(merchant_id, record["candidates"], triage_result,
                                today=when, conn=conn, memory=memory)
    finally:
        conn.close()
        memory.close()

    proposed = {item.get("candidate_id"): item.get("action_type")
                for item in record["candidates"]}
    grid = []
    for row in ranking["level_grid"]:
        grid.append(dict(row,
                         proposed_by=("llm" if record["source"] == "llm" else "fixture"),
                         model_proposed_action=proposed.get(row["candidate_id"]),
                         simulator_picked_level=row["chosen_level"]))

    _log("grid", "ok", "%d candidates by %d levels" % (len(grid), len(simulate.OFFER_LEVELS)),
         merchant_id=merchant_id, today=ranking["today"])
    return {
        "merchant_id": merchant_id,
        "today": ranking["today"],
        "offer_levels": ranking["offer_levels"],
        "uplift_in_use": ranking["uplift_in_use"],
        "candidate_source": record["source"],
        "note": ("the model proposes the action and the segment, the simulator prices every "
                 "level and picks the one that survives and earns most"),
        "grid": grid,
        "recommended": ranking["recommended"],
    }


@app.get("/decisions")
def get_decisions(limit: int = 50, stage: str | None = None) -> dict:
    """Tails the decision log for the dashboard. Newest last, as it was written."""
    path = run_night.LOG_PATH
    if not os.path.exists(path):
        return {"count": 0, "decisions": []}
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if stage and row.get("stage") != stage:
                continue
            rows.append(row)
    return {"count": len(rows), "returned": len(rows[-limit:]), "decisions": rows[-limit:]}


# --------------------------------------------------------------------------
# The soundbox page
# --------------------------------------------------------------------------


@app.get("/soundbox", response_class=HTMLResponse)
def get_soundbox(call_id: str = "") -> HTMLResponse:
    with open(PAGE_PATH, encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


@app.get("/soundbox/audio")
def get_soundbox_audio(call_id: str, kind: str = "script") -> FileResponse:
    call = local_soundbox.PENDING.get(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="no pending call %s" % call_id)
    audio = call["audio"] if kind == "script" else local_soundbox.reprompt_audio(call_id)
    if not audio or not os.path.exists(audio.get("audio_path", "")):
        raise HTTPException(status_code=503,
                            detail="no audio rendered for this call, use the buttons")
    return FileResponse(audio["audio_path"], media_type="audio/wav")


@app.get("/soundbox/state")
def get_soundbox_state(call_id: str) -> dict:
    call = local_soundbox.PENDING.get(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="no pending call %s" % call_id)
    brief: Brief = call["brief"]
    decision = call["decision"]
    return {
        "call_id": call_id,
        "merchant_id": call["merchant_id"],
        "merchant_name": brief.merchant_name,
        "script": brief.script,
        "title": brief.title,
        "rationale": brief.rationale,
        "numbers": json.loads(brief.numbers.model_dump_json()),
        "has_audio": bool(call["audio"]),
        "audio_error": call["audio_error"] or "",
        "reprompts": call["reprompts"],
        "settled": decision is not None,
        "decision": json.loads(decision.model_dump_json()) if decision else None,
    }


@app.post("/soundbox/reply")
async def post_soundbox_reply(call_id: str = Form(...),
                              audio: UploadFile | None = File(None),
                              transcript: str | None = Form(None)) -> dict:
    """Microphone audio in. Either a settled Decision or one reprompt."""
    try:
        payload = local_soundbox.handle_reply(
            call_id,
            audio_bytes=(await audio.read()) if audio is not None else None,
            filename=(audio.filename if audio is not None else "reply.webm"),
            transcript=transcript)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    decision = payload.get("decision")
    if decision is not None:
        _notify_callback(call_id, decision)
    _log("voice_reply", payload["status"],
         "heard %r" % (payload.get("transcript") or ""),
         merchant_id=local_soundbox.PENDING[call_id]["merchant_id"], call_id=call_id,
         classification=payload["classification"], stt=payload.get("stt"),
         decision=json.loads(decision.model_dump_json()) if decision else None)
    return _reply_payload(payload)


@app.post("/soundbox/button")
def post_soundbox_button(request: ButtonRequest) -> dict:
    """The demo insurance path. No microphone, no network, identical Decision."""
    try:
        decision = local_soundbox.press_button(request.call_id, request.answer)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _notify_callback(request.call_id, decision)

    _log("voice_reply", decision.outcome, "button pressed: %s" % request.answer,
         merchant_id=decision.merchant_id, call_id=request.call_id,
         decision=json.loads(decision.model_dump_json()))
    return {"status": "settled", "decision": json.loads(decision.model_dump_json())}


def _reply_payload(payload: dict) -> dict:
    out = {"status": payload["status"], "transcript": payload.get("transcript", ""),
           "classification": payload["classification"]}
    if payload["status"] == "reprompt":
        out["reprompt_text"] = payload["reprompt_text"]
        out["reprompts"] = payload["reprompts"]
    else:
        out["decision"] = json.loads(payload["decision"].model_dump_json())
        if payload.get("note"):
            out["note"] = payload["note"]
    return out


# --------------------------------------------------------------------------
# The judge dashboard
#
# One page, one fetch, polled every two seconds. Everything it needs comes from
# /dashboard/state so the page stays a renderer and nothing more.
#
# Triage over eighteen months of ledger takes well over a second, and generation reads the
# LLM cache, so recomputing both on every poll would make the page crawl. They are cached
# here and invalidated when the number of measured campaigns changes, which is exactly when
# the learned uplift moves and the grid genuinely needs redrawing.
# --------------------------------------------------------------------------

DASHBOARD_PATH = os.path.join(ledger.REPO_ROOT, "dashboard", "index.html")
LEARNING_EXPORT_PATH = os.path.join(ledger.REPO_ROOT, "dashboard", "learning.json")
_ANALYSIS_CACHE: dict = {}


def _segment_label(segment: dict | None) -> str:
    """A target segment as a judge would say it, not as JSON."""
    segment = segment or {}
    kind = segment.get("kind")
    if kind == "lapsed_regulars":
        return "regulars who stopped coming"
    if kind == "offpeak_slot":
        hours = segment.get("hours") or []
        if hours:
            return ("%s %02d:00 to %02d:00"
                    % (segment.get("weekday_name", "one weekday"), hours[0], hours[-1] + 1))
        return segment.get("weekday_name", "a quiet weekday slot")
    if kind == "anchor_buyers":
        return "%s buyers" % segment.get("anchor_item", "anchor")
    return kind or "unknown segment"


def _analysis(merchant_id: str, version: int) -> dict:
    """Triage, generation and the priced grid. Cached against the measured campaign count."""
    key = (merchant_id, version)
    if key in _ANALYSIS_CACHE:
        return _ANALYSIS_CACHE[key]

    conn = ledger.connect()
    memory = store.connect()
    try:
        when = ledger.data_as_of(conn, merchant_id)
        triage_result = triage.score(merchant_id, when, conn=conn)
        generated = None
        ranking = None
        if triage_result["worth_a_call"]:
            generated = run_night.generate_candidates(conn, merchant_id, triage_result)
            ranking = simulate.rank(merchant_id, generated["candidates"], triage_result,
                                    today=when, conn=conn, memory=memory)
    finally:
        conn.close()
        memory.close()

    _ANALYSIS_CACHE.clear()
    _ANALYSIS_CACHE[key] = {"triage": triage_result, "generated": generated,
                            "ranking": ranking}
    return _ANALYSIS_CACHE[key]


def _tonight(triage_result: dict) -> dict:
    signals = triage_result["signals"]
    lapsed = signals["lapsed_regulars"]
    rows = []
    if lapsed["count"]:
        gaps = [row["days_since_last_visit"] for row in lapsed["detail"]]
        rows.append({
            "kind": "lapsed regulars",
            "sentence": ("%d regulars have stopped coming, silent between %d and %d days"
                         % (lapsed["count"], min(gaps), max(gaps))),
            "monthly_value": lapsed["value_at_risk_monthly"],
            "counts_in_full": True,
        })
    for gap in signals["offpeak_gaps"]["detail"]:
        rows.append({
            "kind": "off peak gap",
            "sentence": ("%s %02d:00 to %02d:00 runs %.0f percent below the same hours on "
                         "other days" % (gap["weekday_name"], gap["hours"][0],
                                         gap["hours"][-1] + 1, gap["shortfall_pct"])),
            "monthly_value": gap["monthly_upside"],
            "counts_in_full": False,
        })
    for gap in signals["affinity_gaps"]["detail"]:
        rows.append({
            "kind": "basket affinity gap",
            "sentence": ("%s travels alone, attaching a snack %.0f times in a hundred "
                         "against %.0f for %s"
                         % (gap["anchor_item"], 100 * gap["attach_rate"],
                            100 * gap["benchmark_attach_rate"], gap["benchmark_item"])),
            "monthly_value": gap["monthly_upside"],
            "counts_in_full": False,
        })
    return {
        "decision": triage_result["decision"],
        "worth_a_call": triage_result["worth_a_call"],
        "opportunity_score": triage_result["opportunity_score"],
        "revenue_share": triage_result["revenue_share"],
        "thresholds": triage_result["thresholds"],
        "signals": rows,
        "evidence": triage_result["evidence"],
        "silent_reason": (None if triage_result["worth_a_call"] else
                          "Nothing at this shop is far enough from its own norm tonight. "
                          "The agent stays silent, which is what it does most nights."),
    }


def _latest_call() -> dict | None:
    """The most recently opened soundbox call, settled or not."""
    if not local_soundbox.PENDING:
        return None
    call = sorted(local_soundbox.PENDING.values(), key=lambda row: row["started_at"])[-1]
    brief: Brief = call["brief"]
    decision = call["decision"]
    heard = ""
    for turn in call["transcripts"]:
        if turn.get("text"):
            heard = turn["text"]
    return {
        "call_id": call["call_id"],
        "merchant_name": brief.merchant_name,
        "title": brief.title,
        "script": brief.script,
        "script_source": brief.script_source,
        "transcript": (decision.transcript if decision else heard),
        "outcome": (decision.outcome if decision else "awaiting_reply"),
        "approved": (decision.approved if decision else None),
        "via": (decision.modifications.get("via") if decision else None),
        "settled": decision is not None,
        "has_audio": bool(call["audio"]),
        "audio_url": ("/soundbox/audio?call_id=%s" % call["call_id"]
                      if call["audio"] else None),
        "numbers": json.loads(brief.numbers.model_dump_json()),
    }


def _latest_campaign(memory, merchant_id: str) -> dict | None:
    row = memory.execute(
        "SELECT * FROM campaigns WHERE merchant_id = ? ORDER BY sequence DESC LIMIT 1",
        (merchant_id,)).fetchone()
    if row is None:
        return None

    campaign_id = row["campaign_id"]
    assignment = store.assignments(memory, campaign_id)
    bodies = {message["customer_id"]: message
              for message in store.messages(memory, campaign_id)}

    conn = ledger.connect()
    try:
        names = ledger.customer_names(conn, merchant_id)
    finally:
        conn.close()

    treated = [{"customer_id": customer_id,
                "customer_name": names.get(customer_id, customer_id),
                "body": bodies.get(customer_id, {}).get("body", ""),
                "status": bodies.get(customer_id, {}).get("status", "not rendered")}
               for customer_id in assignment["treated"]]
    control = [{"customer_id": customer_id,
                "customer_name": names.get(customer_id, customer_id),
                "body": None,
                "status": "held back, never messaged"}
               for customer_id in assignment["control"]]

    measurement = memory.execute("SELECT * FROM measurements WHERE campaign_id = ?",
                                 (campaign_id,)).fetchone()
    return {
        "campaign_id": campaign_id,
        "sequence": row["sequence"],
        "offer_level": row["offer_level"],
        "discount_pct": row["discount_pct"],
        "offer_applies_to": row["offer_applies_to"],
        "status": row["status"],
        "treated": treated,
        "control": control,
        "channel": "dashboard_stub",
        "production_channel": "whatsapp_business_api",
        "dispatch_note": ("rendered and logged, not sent. WhatsApp Business API is the "
                          "production path and needs business verification plus template "
                          "approval."),
        "measurement": dict(measurement) if measurement else None,
        "horizon_days": simulate.VALUE_HORIZON_DAYS,
    }


def _learning(memory, merchant_id: str) -> dict:
    series = store.learning_series(memory, merchant_id)
    scale = store.learned_response_scale(
        memory, merchant_id, simulate.PRIOR_OFFER_UPLIFT,
        baseline_rate=store.last_baseline_rate(memory, merchant_id))

    # The hidden truth reaches the chart only through the evaluation export written by
    # scripts/run_campaigns.py. No module the agent uses imports the simulated world, and
    # this one reads a file rather than that module, so the separation holds here too.
    overlay = {}
    if os.path.exists(LEARNING_EXPORT_PATH):
        try:
            with open(LEARNING_EXPORT_PATH, encoding="utf-8") as handle:
                exported = json.load(handle)
            overlay = {row["sequence"]: row for row in exported.get("rows", [])}
        except (ValueError, KeyError, TypeError):
            overlay = {}

    truths = [row.get("hidden_truth") for row in overlay.values()
              if row.get("hidden_truth") is not None]
    for row in series:
        extra = overlay.get(row["sequence"], {})
        row["hidden_truth"] = extra.get("hidden_truth")
        row["belief_error_vs_truth"] = extra.get("belief_error_vs_truth")

    headline = None
    if series:
        headline = (("this shop measures %.2f times as responsive as the priors assumed"
                     % scale["scale"]) if scale["source"] == "MEASURED"
                    else "no campaign measured yet, the priors stand unchanged")
    return {
        "campaigns": len(series),
        "series": series,
        "scale": scale,
        "hidden_truth": (sum(truths) / len(truths)) if truths else None,
        "priors": simulate.PRIOR_OFFER_UPLIFT,
        "headline": headline,
    }


@app.get("/dashboard/state")
def get_dashboard_state(merchant_id: str = DEFAULT_MERCHANT) -> dict:
    """Everything the judge page renders, in one call. Read only, safe to poll."""
    memory = store.connect()
    try:
        version = memory.execute("SELECT COUNT(*) FROM measurements WHERE merchant_id = ?",
                                 (merchant_id,)).fetchone()[0]
        analysis = _analysis(merchant_id, version)
        campaign_state = _latest_campaign(memory, merchant_id)
        learning = _learning(memory, merchant_id)
    finally:
        memory.close()

    triage_result = analysis["triage"]
    ranking = analysis["ranking"]
    generated = analysis["generated"]

    reasoning = {
        "candidate_source": (generated or {}).get("source"),
        "offer_levels": simulate.OFFER_LEVEL_DISCOUNT_PCT,
        "note": "the model proposes who and what, code prices how deep",
        "dropped": (generated or {}).get("dropped", []),
        "grid": [],
        "recommended_id": None,
    }
    if ranking:
        reasoning["recommended_id"] = (ranking["recommended"] or {}).get("candidate_id")
        for row in ranking["level_grid"]:
            reasoning["grid"].append({
                "candidate_id": row["candidate_id"],
                "title": row["title"],
                "action_type": row["action_type"],
                "segment": _segment_label(row.get("target_segment")),
                "rationale": row.get("rationale"),
                "source": row["source"],
                "model_suggested_level": row.get("model_suggested_level"),
                "chosen_level": row["chosen_level"],
                "accepted": row["accepted"],
                "levels": row["levels"],
                "rejections": row.get("rejections", []),
            })

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "llm_mode": llm.mode(),
        "merchant": {
            "merchant_id": merchant_id,
            "name": triage_result["merchant_name"],
            "today": triage_result["today"],
            "baseline": triage_result["merchant_baseline"],
        },
        "tonight": _tonight(triage_result),
        "reasoning": reasoning,
        "call": _latest_call(),
        "campaign": campaign_state,
        "learning": learning,
    }


@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def get_dashboard() -> HTMLResponse:
    with open(DASHBOARD_PATH, encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


# --------------------------------------------------------------------------
# Endpoints the n8n workflows need
#
# Each of these exists because a node on the canvas calls it. They are deliberately thin:
# the workflow owns the sequencing, this service owns the arithmetic.
# --------------------------------------------------------------------------


class LogDecisionRequest(BaseModel):
    stage: str
    decision: str
    headline: str = ""
    merchant_id: str = DEFAULT_MERCHANT
    detail: dict = Field(default_factory=dict)
    source: str = "n8n"


class DispatchRequest(BaseModel):
    merchant_id: str = DEFAULT_MERCHANT
    call_id: str
    treated: list[str]
    control: list[str]
    seed: int = holdout.DEFAULT_SEED


class LearningUpdateRequest(BaseModel):
    merchant_id: str = DEFAULT_MERCHANT
    campaign_id: str


class CallOutcomeRequest(BaseModel):
    call_id: str
    outcome: str = Field(description="approved, declined or no_answer")
    transcript: str = ""
    merchant_id: str = DEFAULT_MERCHANT
    modifications: dict = Field(default_factory=dict)
    source: str = "sarvam_webhook"


@app.post("/decisions")
def post_decision(request: LogDecisionRequest) -> dict:
    """Writes one line into the decision log from the workflow.

    The silent nights, the declines and the no answers all arrive here. A night the agent
    decided to do nothing is a decision, and the dashboard reads it the same as any other.
    """
    _log(request.stage, request.decision, request.headline,
         merchant_id=request.merchant_id, detail=request.detail, source=request.source)
    return {"status": "logged", "stage": request.stage, "decision": request.decision}


@app.post("/campaign/dispatch")
def post_campaign_dispatch(request: DispatchRequest) -> dict:
    """Dispatches a split that the n8n Code node computed.

    The workflow owns the assignment so the split is visible on the canvas rather than
    hidden in a service. This endpoint still checks it: the arms must be disjoint, must
    cover the segment, and must be the right sizes, or nothing is sent. It also reports
    whether the workflow's split matches what core/holdout.py would have produced, so a
    drift between the two is visible rather than silent.
    """
    call = local_soundbox.PENDING.get(request.call_id)
    chosen = (call or {}).get("chosen")
    brief: Brief | None = call["brief"] if call else None
    if brief is None or chosen is None:
        raise HTTPException(status_code=404,
                            detail="no open call %s to dispatch from" % request.call_id)

    assignment = {
        "campaign_id": request.call_id,
        "seed": request.seed,
        "segment_size": len(set(request.treated) | set(request.control)),
        "treated": sorted(request.treated),
        "control": sorted(request.control),
        "treated_count": len(request.treated),
        "holdout_count": len(request.control),
        "holdout_share": HOLDOUT_SHARE,
        "method": "computed in the n8n Code node, verified here",
    }
    problems = holdout.validate(assignment)
    if problems:
        _log("campaign_dispatch", "refused", "; ".join(problems),
             merchant_id=request.merchant_id, call_id=request.call_id)
        raise HTTPException(status_code=400,
                            detail="refusing to dispatch a broken split: %s"
                                   % "; ".join(problems))

    canonical = holdout.assign(request.call_id,
                               list(request.treated) + list(request.control),
                               seed=request.seed)
    matches = (canonical["treated"] == assignment["treated"]
               and canonical["control"] == assignment["control"])

    conn = ledger.connect()
    memory = store.connect()
    try:
        shop_name = ledger.merchant(conn, request.merchant_id)["name"]
        triage_result = triage.score(request.merchant_id,
                                     ledger.data_as_of(conn, request.merchant_id), conn=conn)
        facts = {row["customer_id"]: row
                 for row in triage_result["signals"]["lapsed_regulars"]["detail"]}

        campaign_id = campaign_flow.new_campaign_id()
        store.save_assignments(memory, campaign_id, assignment)
        record = store.record_campaign(memory, campaign_id, request.merchant_id, chosen)
        sent = dispatch.dispatch(memory, campaign_id, assignment, chosen, shop_name, facts,
                                 customer_names=ledger.customer_names(conn,
                                                                      request.merchant_id))
    finally:
        conn.close()
        memory.close()

    CAMPAIGNS[campaign_id] = {
        "campaign_id": campaign_id,
        "merchant_id": request.merchant_id,
        "call_id": request.call_id,
        "candidate_id": chosen.get("candidate_id"),
        "status": "dispatched_awaiting_measurement",
    }
    _log("campaign_dispatch", "dispatched",
         "%d messaged and %d held back for campaign %s"
         % (sent["rendered"], len(assignment["control"]), campaign_id),
         merchant_id=request.merchant_id, call_id=request.call_id, campaign_id=campaign_id,
         matches_core_holdout=matches, messages=sent["messages"])
    return {
        "status": "dispatched",
        "campaign_id": campaign_id,
        "sequence": record["sequence"],
        "treated_count": assignment["treated_count"],
        "holdout_count": assignment["holdout_count"],
        "matches_core_holdout": matches,
        "rendered": sent["rendered"],
        "blocked": sent["blocked"],
        "production_channel": sent["production_channel"],
        "messages": sent["messages"],
    }


@app.post("/learning/update")
def post_learning_update(request: LearningUpdateRequest) -> dict:
    """Writes the lesson back: what was predicted, what was measured, what is now believed.

    /measure files the measurement. This is the step that reads it back and reports the
    refreshed belief, so the workflow has a node that visibly closes the loop and the
    dashboard gets a log line saying what the shop just taught the agent.
    """
    memory = store.connect()
    try:
        record = store.campaign(memory, request.campaign_id)
        row = memory.execute("SELECT * FROM measurements WHERE campaign_id = ?",
                             (request.campaign_id,)).fetchone()
        if record is None or row is None:
            _log("learning_update", "not_ready", "nothing measured for that campaign yet",
                 merchant_id=request.merchant_id, campaign_id=request.campaign_id)
            return {"status": "not_ready", "campaign_id": request.campaign_id,
                    "reason": "measure the campaign before writing the lesson back"}

        measurement = dict(row)
        scale = store.learned_response_scale(
            memory, request.merchant_id, simulate.PRIOR_OFFER_UPLIFT,
            baseline_rate=store.last_baseline_rate(memory, request.merchant_id))
        believed_now = {
            level: simulate.PRIOR_OFFER_UPLIFT[level] * scale["scale"]
            for level in simulate.OFFER_LEVELS
        }
    finally:
        memory.close()

    _log("learning_update", "recorded",
         "predicted %.3f, measured %.3f, error %.3f"
         % (measurement["predicted_uplift"] or 0.0, measurement["lift"],
            measurement["uplift_error"] or 0.0),
         merchant_id=request.merchant_id, campaign_id=request.campaign_id,
         predicted_uplift=measurement["predicted_uplift"],
         actual_uplift=measurement["lift"],
         uplift_error=measurement["uplift_error"],
         profit_error=measurement["profit_error"],
         learned_scale=scale)
    return {
        "status": "recorded",
        "campaign_id": request.campaign_id,
        "predicted_uplift": measurement["predicted_uplift"],
        "actual_uplift": measurement["lift"],
        "uplift_error": measurement["uplift_error"],
        "predicted_profit": measurement["predicted_profit"],
        "actual_profit": measurement["actual_profit"],
        "profit_error": measurement["profit_error"],
        "learned_response_scale": scale,
        "believed_uplift_now": believed_now,
    }


@app.post("/call/outcome")
def post_call_outcome(request: CallOutcomeRequest) -> dict:
    """Records how a call ended. This is where Sarvam's webhook callback lands.

    The soundbox settles its own calls through /soundbox/button and /soundbox/reply. Real
    telephony has no browser, so the outcome arrives here instead and produces the same
    Decision, which is the point of having one voice interface with two backends.
    """
    outcome = str(request.outcome).strip().lower()
    if outcome not in ("approved", "declined", "no_answer"):
        raise HTTPException(status_code=400,
                            detail="outcome must be approved, declined or no_answer")
    try:
        decision = local_soundbox.settle(request.call_id, outcome,
                                         transcript=request.transcript, via=request.source)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _notify_callback(request.call_id, decision)
    _log("call_outcome", outcome, "call %s ended as %s" % (request.call_id, outcome),
         merchant_id=request.merchant_id, call_id=request.call_id,
         transcript=request.transcript, modifications=request.modifications,
         decision=json.loads(decision.model_dump_json()))
    return {
        "status": "recorded",
        "call_id": request.call_id,
        "outcome": outcome,
        "approved": decision.approved,
        "decision": json.loads(decision.model_dump_json()),
    }
