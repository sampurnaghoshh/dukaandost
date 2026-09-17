"""Sarvam text to speech and speech to text, with the same record and replay discipline as llm.py.

Confirmed against docs.sarvam.ai on 17 Sep 2026:

  TTS  POST https://api.sarvam.ai/text-to-speech
       JSON in, JSON out. Response is {"audios": ["<base64 wav>", ...]}, join then decode.
       model bulbul:v3 (legacy bulbul:v2), 30 plus speakers, shubh is the default.
       2500 characters per request. Sample rates 8000, 16000, 22050, 24000, and
       32000, 44100, 48000 on bulbul:v3 REST only. Default 24000. Pace 0.5x to 2.0x.

  STT  POST https://api.sarvam.ai/speech-to-text
       multipart/form-data with file, model and mode. Response is {"transcript": ...}.
       model saaras:v3 (default, recommended) or saaras:v4 (latest).
       modes transcribe, translate, verbatim, translit, codemix.
       30 seconds per REST request, longer needs the batch API.
       WAV, MP3, AAC, AIFF, OGG, OPUS, FLAC, MP4, AMR, WMA and WebM, auto detected.

That last line is why the browser soundbox works at all: MediaRecorder hands us WebM and
Sarvam takes it without a conversion step.

Audio is cached by a hash of the request, so replay mode plays the demo with the network
unplugged. The API key is never logged, cached or included in an error.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

import httpx
from dotenv import load_dotenv

from core.ledger import REPO_ROOT
from core.llm import LLMAuthError, LLMError, LLMUnavailable, mode

load_dotenv()

TTS_ENDPOINT = "https://api.sarvam.ai/text-to-speech"
STT_ENDPOINT = "https://api.sarvam.ai/speech-to-text"
AUTH_HEADER = "api-subscription-key"

DEFAULT_TTS_MODEL = "bulbul:v3"
DEFAULT_TTS_SPEAKER = "shubh"
DEFAULT_STT_MODEL = "saaras:v3"
DEFAULT_STT_MODE = "transcribe"
DEFAULT_LANGUAGE = "hi-IN"

TTS_MAX_CHARS = 2500
TTS_SAMPLE_RATE = 24000          # bulbul:v3 default, 8000 is telephony grade
STT_MAX_SECONDS = 30
TIMEOUT_SECONDS = 90.0
RETRIES = 1

AUDIO_DIR = os.path.join(REPO_ROOT, "data", "audio_cache")


class SpeechError(LLMError):
    """Speech specific failure. Shares the exception family so callers catch one thing."""


def tts_model() -> str:
    return (os.getenv("SARVAM_TTS_MODEL") or DEFAULT_TTS_MODEL).strip()


def tts_speaker() -> str:
    return (os.getenv("SARVAM_TTS_SPEAKER") or DEFAULT_TTS_SPEAKER).strip()


def stt_model() -> str:
    return (os.getenv("SARVAM_STT_MODEL") or DEFAULT_STT_MODEL).strip()


def _api_key() -> str:
    key = os.getenv("SARVAM_API_KEY")
    if not key or not key.strip():
        raise LLMAuthError("SARVAM_API_KEY is not set in the environment")
    return key.strip()


def _digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# Text to speech
# --------------------------------------------------------------------------


def speak(text: str, language_code: str = DEFAULT_LANGUAGE,
          speaker: str | None = None, sample_rate: int = TTS_SAMPLE_RATE,
          pace: float = 1.0) -> dict:
    """Renders text to a WAV file on disk and returns its path plus how it got there.

    Cached on a hash of the request, so the same script never costs credit twice and the
    demo runs offline once the cache is warm.
    """
    if not text or not text.strip():
        raise SpeechError("nothing to speak")
    if len(text) > TTS_MAX_CHARS:
        raise SpeechError("script is %d characters, Sarvam accepts %d per request"
                          % (len(text), TTS_MAX_CHARS))

    request = {
        "text": text,
        "target_language_code": language_code,
        "model": tts_model(),
        "speaker": speaker or tts_speaker(),
        "speech_sample_rate": sample_rate,
        "pace": pace,
    }
    key = _digest(request)
    path = os.path.join(AUDIO_DIR, "tts_%s.wav" % key)
    current = mode()

    if os.path.exists(path) and current in ("record", "replay"):
        return {"audio_path": path, "cache_key": key, "source": "cache", "mode": current,
                "model": request["model"], "speaker": request["speaker"]}

    if current == "replay":
        raise LLMUnavailable(
            "replay mode and no cached audio for this script (key %s). Run once in record "
            "mode first." % key)

    audio = _post_tts(request)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(audio)
    return {"audio_path": path, "cache_key": key, "source": "sarvam", "mode": current,
            "model": request["model"], "speaker": request["speaker"]}


def _post_tts(request: dict) -> bytes:
    headers = {AUTH_HEADER: _api_key(), "Content-Type": "application/json"}
    last: Exception | None = None
    for _attempt in range(RETRIES + 1):
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.post(TTS_ENDPOINT, headers=headers, json=request)
        except httpx.HTTPError as exc:
            last = LLMUnavailable("Sarvam text to speech unreachable: %s" % type(exc).__name__)
            continue
        if response.status_code == 403:
            raise LLMAuthError("Sarvam refused text to speech with 403.")
        if response.status_code == 429 or response.status_code >= 500:
            last = LLMUnavailable("Sarvam text to speech returned %d" % response.status_code)
            continue
        if response.status_code >= 400:
            raise SpeechError("Sarvam text to speech returned %d" % response.status_code)
        try:
            chunks = response.json()["audios"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SpeechError("unexpected text to speech envelope: %s"
                              % type(exc).__name__) from exc
        if not chunks:
            raise SpeechError("Sarvam text to speech returned no audio")
        return base64.b64decode("".join(chunks))
    raise last if last else LLMUnavailable("Sarvam text to speech failed")


# --------------------------------------------------------------------------
# Speech to text
# --------------------------------------------------------------------------


def transcribe(audio: bytes, filename: str = "reply.webm",
               stt_mode: str = DEFAULT_STT_MODE) -> dict:
    """Transcribes a short reply. Cached on the audio itself so replays cost nothing."""
    if not audio:
        raise SpeechError("no audio to transcribe")

    key = hashlib.sha256(audio).hexdigest()[:32]
    request = {"model": stt_model(), "mode": stt_mode, "audio_sha": key}
    cache_file = os.path.join(AUDIO_DIR, "stt_%s.json" % _digest(request))
    current = mode()

    if os.path.exists(cache_file) and current in ("record", "replay"):
        with open(cache_file, encoding="utf-8") as handle:
            cached = json.load(handle)
        return {"transcript": cached["transcript"], "source": "cache", "mode": current,
                "model": request["model"]}

    if current == "replay":
        raise LLMUnavailable(
            "replay mode and no cached transcript for this audio. Use the Haan or Nahi "
            "button, which needs neither the microphone nor the network.")

    transcript = _post_stt(audio, filename, stt_mode)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as handle:
        json.dump({"transcript": transcript, "model": request["model"], "mode": stt_mode},
                  handle, ensure_ascii=False, indent=2)
    return {"transcript": transcript, "source": "sarvam", "mode": current,
            "model": request["model"]}


def _post_stt(audio: bytes, filename: str, stt_mode: str) -> str:
    headers = {AUTH_HEADER: _api_key()}
    files = {"file": (filename, audio, "application/octet-stream")}
    data = {"model": stt_model(), "mode": stt_mode}
    last: Exception | None = None
    for _attempt in range(RETRIES + 1):
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.post(STT_ENDPOINT, headers=headers, files=files, data=data)
        except httpx.HTTPError as exc:
            last = LLMUnavailable("Sarvam speech to text unreachable: %s" % type(exc).__name__)
            continue
        if response.status_code == 403:
            raise LLMAuthError("Sarvam refused speech to text with 403.")
        if response.status_code == 429 or response.status_code >= 500:
            last = LLMUnavailable("Sarvam speech to text returned %d" % response.status_code)
            continue
        if response.status_code >= 400:
            raise SpeechError("Sarvam speech to text returned %d" % response.status_code)
        try:
            return response.json()["transcript"] or ""
        except (ValueError, KeyError, TypeError) as exc:
            raise SpeechError("unexpected speech to text envelope: %s"
                              % type(exc).__name__) from exc
    raise last if last else LLMUnavailable("Sarvam speech to text failed")
