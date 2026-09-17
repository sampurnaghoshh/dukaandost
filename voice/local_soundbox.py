"""Demo day fallback: the same script through Sarvam TTS in a browser tab, mic for the reply.

Implements dial(merchant_id, brief) -> Decision from voice/base.py. Same Brief in, same
Decision out as real telephony, so triage, the simulator, the holdout and measurement cannot
tell which one ran.

How it works:

  1. dial() renders the filled Hindi script to a WAV through Sarvam TTS and parks a pending
     call in the registry below.
  2. The FastAPI page at /soundbox plays that WAV, then records a few seconds of microphone
     audio and posts it back.
  3. Sarvam STT transcribes the reply, classify_reply() reads it as approved, declined or
     unclear. Unclear replays a short reprompt once, then gives up and returns undecided.
  4. Two large buttons, Haan and Nahi, produce the identical Decision without touching the
     microphone or the network.

Those buttons are not a convenience. On a stage, with venue wifi and a room full of noise,
they are the difference between a demo and an apology, and the Decision they return is
byte for byte what the spoken path returns.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime

from core import speech
from core.llm import LLMError
from voice.base import Brief, Decision

CHANNEL = "local_soundbox"

# --------------------------------------------------------------------------
# Reading the merchant's answer
#
# Matched on the word, not on a model. A shopkeeper saying haan in a noisy market is not a
# natural language understanding problem worth spending a round trip on, and a wrong read
# here spends his money.
# --------------------------------------------------------------------------

APPROVE_PHRASES = (
    "haan", "ha", "han", "haa", "hanji", "ji haan", "ji ha", "haan ji",
    "bhej do", "bhejo", "bhej", "bhej dijiye", "bhej de", "bhej dena",
    "theek hai", "thik hai", "theek", "thik", "sahi hai", "accha", "achha",
    "kar do", "kardo", "ok", "okay", "yes", "haan bhej do",
    "हां", "हाँ", "हा",
    "जी हां", "हां जी",
    "भेज दो", "भेजो",
    "ठीक है", "ठिक है",
    "अच्छा", "कर दो",
)

DECLINE_PHRASES = (
    "nahi", "nahin", "na", "naa", "nai", "mat bhejo", "mat bhej", "mat",
    "nahi chahiye", "nahin chahiye", "rehne do", "rahne do", "abhi nahi",
    "abhi nahin", "baad mein", "no", "nope", "cancel", "ruko", "ruk jao",
    "नहीं", "नही", "ना",
    "मत भेजो", "मत भेज",
    "नहीं चाहिए",
    "रहने दो", "अभी नहीं",
    "बाद में",
)

REPROMPT_HINDI = (
    "माफ़ कीजिए, समझा "
    "नहीं। भेजना है तो "
    "हां बोलिए, नहीं तो "
    "नहीं बोलिए।"
)

MAX_REPROMPTS = 1


# Flattened to spaces before matching. Everything else is kept, which matters more than it
# looks: a Devanagari vowel sign like the one in haan is a combining mark, not alphanumeric,
# so filtering on isalnum() silently tears Hindi words in half.
PUNCTUATION = set(".,!?;:\"'`()[]{}<>/\\|_=+*&^~@#$-"
                  # Devanagari danda and double danda, ellipsis, and the two long dashes.
                  # Built from code points so no literal long dash sits in the source, which
                  # is a house rule, while transcripts containing them still get cleaned.
                  + "".join(chr(code) for code in (0x0964, 0x0965, 0x2026, 0x2013, 0x2014)))


def _normalise(text: str) -> str:
    """Lower cased, punctuation flattened to spaces, so a full stop cannot defeat a match."""
    kept = [" " if (character in PUNCTUATION or character.isspace()) else character
            for character in (text or "").lower()]
    return " ".join("".join(kept).split())


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """Whole word match, so nahi inside a longer word does not count as a refusal."""
    words = haystack.split()
    target = phrase.split()
    if not target:
        return False
    for start in range(len(words) - len(target) + 1):
        if words[start:start + len(target)] == target:
            return True
    return False


def classify_reply(transcript: str) -> dict:
    """approved, declined or unclear, with the phrase that decided it.

    A reply containing both a yes and a no is unclear on purpose. "haan nahi nahi" is a
    person changing their mind, and the safe reading of an ambiguous answer about spending
    money is to ask again.
    """
    text = _normalise(transcript)
    if not text:
        return {"outcome": "unclear", "matched": None,
                "reason": "nothing was transcribed"}

    approvals = [phrase for phrase in APPROVE_PHRASES if _contains_phrase(text, phrase)]
    declines = [phrase for phrase in DECLINE_PHRASES if _contains_phrase(text, phrase)]

    if approvals and declines:
        longest_yes = max(approvals, key=len)
        longest_no = max(declines, key=len)
        # "mat bhejo" contains "bhejo". The longer phrase is the one the merchant said, and
        # the shorter one is a fragment of it, so the specific reading wins.
        if longest_no in longest_yes and longest_no != longest_yes:
            return {"outcome": "approved", "matched": longest_yes,
                    "reason": "a refusal word appeared only inside a longer approval phrase"}
        if longest_yes in longest_no and longest_yes != longest_no:
            return {"outcome": "declined", "matched": longest_no,
                    "reason": "an approval word appeared only inside a longer refusal phrase"}
        return {"outcome": "unclear", "matched": None,
                "reason": "heard both a yes and a no in the same reply"}
    if approvals:
        return {"outcome": "approved", "matched": max(approvals, key=len),
                "reason": "matched an approval phrase"}
    if declines:
        return {"outcome": "declined", "matched": max(declines, key=len),
                "reason": "matched a refusal phrase"}
    return {"outcome": "unclear", "matched": None,
            "reason": "no approval or refusal phrase in the reply"}


# --------------------------------------------------------------------------
# Pending calls
#
# dial() parks the call here and the browser page drives it to a Decision. Single merchant,
# single process, so a dict is the right amount of machinery.
# --------------------------------------------------------------------------

PENDING: dict = {}


def dial(merchant_id: str, brief: Brief) -> Decision:
    """Opens a soundbox call. Returns immediately with an undecided Decision.

    The browser page resolves it. A Decision comes back either from the microphone path or
    from the Haan and Nahi buttons, and the two are indistinguishable downstream.
    """
    call_id = "call_%s" % uuid.uuid4().hex[:10]
    audio = None
    error = None
    try:
        audio = speech.speak(brief.script, language_code=brief.language)
    except LLMError as exc:
        error = "%s: %s" % (type(exc).__name__, exc)

    PENDING[call_id] = {
        "call_id": call_id,
        "merchant_id": merchant_id,
        "brief": brief,
        "audio": audio,
        "audio_error": error,
        "reprompts": 0,
        "started_at": time.time(),
        "decision": None,
        "transcripts": [],
    }
    return Decision(
        approved=False,
        outcome="no_answer",
        transcript="",
        merchant_id=merchant_id,
        candidate_id=brief.candidate_id,
        call_id=call_id,
        channel=CHANNEL,
        modifications={"status": "awaiting_reply",
                       "audio_path": (audio or {}).get("audio_path", ""),
                       "audio_source": (audio or {}).get("source", "none"),
                       "audio_error": error or ""},
    )


def reprompt_audio(call_id: str) -> dict | None:
    """The short "say haan or nahi" clip, rendered the same way as the script."""
    call = PENDING.get(call_id)
    if call is None:
        return None
    try:
        return speech.speak(REPROMPT_HINDI, language_code=call["brief"].language)
    except LLMError:
        return None


def settle(call_id: str, outcome: str, transcript: str = "", via: str = "button",
           matched: str | None = None) -> Decision:
    """Turns an outcome into the Decision, and records it against the pending call."""
    call = PENDING.get(call_id)
    if call is None:
        raise KeyError("no pending call %s" % call_id)

    brief: Brief = call["brief"]
    call["transcripts"].append({"text": transcript, "via": via, "outcome": outcome,
                                "at": datetime.now().isoformat(timespec="seconds")})
    decision = Decision(
        approved=(outcome == "approved"),
        outcome=outcome if outcome in ("approved", "declined", "no_answer") else "no_answer",
        transcript=transcript,
        merchant_id=call["merchant_id"],
        candidate_id=brief.candidate_id,
        call_id=call_id,
        duration_seconds=round(time.time() - call["started_at"], 2),
        channel=CHANNEL,
        modifications={
            "via": via,
            "matched_phrase": matched or "",
            "reprompts": call["reprompts"],
            "audio_path": (call["audio"] or {}).get("audio_path", ""),
            "undecided": outcome == "undecided",
        },
    )
    call["decision"] = decision
    return decision


def handle_reply(call_id: str, audio_bytes: bytes | None = None, filename: str = "reply.webm",
                 transcript: str | None = None) -> dict:
    """One turn of the spoken path.

    Returns either a settled Decision, or an instruction to reprompt once. A second unclear
    reply ends the call as undecided rather than guessing what the merchant meant.
    """
    call = PENDING.get(call_id)
    if call is None:
        raise KeyError("no pending call %s" % call_id)

    heard = transcript
    stt_meta = {"source": "supplied"}
    if heard is None:
        if not audio_bytes:
            heard = ""
            stt_meta = {"source": "empty"}
        else:
            try:
                result = speech.transcribe(audio_bytes, filename=filename)
                heard = result["transcript"]
                stt_meta = {"source": result["source"], "model": result.get("model")}
            except LLMError as exc:
                heard = ""
                stt_meta = {"source": "error", "error": "%s: %s" % (type(exc).__name__, exc)}

    verdict = classify_reply(heard)
    if verdict["outcome"] in ("approved", "declined"):
        decision = settle(call_id, verdict["outcome"], transcript=heard, via="voice",
                          matched=verdict["matched"])
        return {"status": "settled", "decision": decision, "transcript": heard,
                "classification": verdict, "stt": stt_meta}

    if call["reprompts"] < MAX_REPROMPTS:
        call["reprompts"] += 1
        call["transcripts"].append({"text": heard, "via": "voice", "outcome": "unclear",
                                    "at": datetime.now().isoformat(timespec="seconds")})
        return {"status": "reprompt", "transcript": heard, "classification": verdict,
                "reprompt_text": REPROMPT_HINDI, "stt": stt_meta,
                "reprompts": call["reprompts"]}

    decision = settle(call_id, "undecided", transcript=heard, via="voice")
    return {"status": "settled", "decision": decision, "transcript": heard,
            "classification": verdict, "stt": stt_meta,
            "note": "two unclear replies, ending the call rather than guessing"}


def press_button(call_id: str, answer: str) -> Decision:
    """The Haan and Nahi buttons. No microphone, no network, identical Decision."""
    outcome = "approved" if str(answer).strip().lower() in ("haan", "yes", "approve",
                                                            "approved") else "declined"
    spoken = "Haan, bhej do." if outcome == "approved" else "Nahi, mat bhejo."
    return settle(call_id, outcome, transcript=spoken, via="button", matched=answer)
