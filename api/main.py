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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from core import (campaign as campaign_flow, fixtures, generate, holdout, ledger, llm,
                  run_night, simulate, speech, triage)
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
    if not request.approved:
        _log("campaign_launch", "declined", "merchant declined, nothing dispatched",
             merchant_id=request.merchant_id, call_id=request.call_id,
             candidate_id=request.candidate_id, transcript=request.transcript)
        return {"status": "declined", "campaign_id": None,
                "reason": "the merchant did not approve"}

    call = local_soundbox.PENDING.get(request.call_id)
    brief: Brief | None = call["brief"] if call else None
    chosen = (call or {}).get("chosen")
    if brief is None or chosen is None:
        raise HTTPException(status_code=404,
                            detail="no open call %s to launch from" % request.call_id)

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
                                        customer_ids, customer_facts=facts)
    finally:
        conn.close()
        memory.close()

    campaign_id = launched["campaign_id"]
    CAMPAIGNS[campaign_id] = {
        "campaign_id": campaign_id,
        "merchant_id": request.merchant_id,
        "call_id": request.call_id,
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
         merchant_id=request.merchant_id, call_id=request.call_id, campaign_id=campaign_id,
         candidate_id=chosen.get("candidate_id"), predicted=launched["predicted"],
         assignment={"treated": launched["assignment"]["treated"],
                     "control": launched["assignment"]["control"],
                     "seed": launched["assignment"]["seed"]},
         messages=launched["dispatch"]["messages"], transcript=request.transcript)
    return {
        "status": "approved",
        "campaign_id": campaign_id,
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
