"""Thin Sarvam chat completions client with a timeout, one retry, and record or replay.

Confirmed against docs.sarvam.ai on 17 Sep 2026:

    endpoint    POST https://api.sarvam.ai/v1/chat/completions
    auth        api-subscription-key header (Authorization Bearer also accepted)
    model       sarvam-105b, the flagship. Sarvam-M is deprecated and removed.
    json        no documented response_format or json_schema parameter, so JSON is asked
                for in the prompt and parsed defensively here
    params      messages, temperature, top_p, max_tokens, seed, stop, reasoning_effort,
                wiki_grounding, presence_penalty, frequency_penalty

Three modes, set by LLM_MODE:

    live     always call the API, never touch the cache
    record   use a cached response if there is one, otherwise call and save it (default)
    replay   cached responses only, no network access whatsoever

Replay is the demo day insurance policy. Once the cache is warm the whole nightly loop runs
with the network unplugged, which matters on venue wifi.

The API key is read from the environment and never printed, logged, returned in an error,
or written to the cache.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from core.ledger import REPO_ROOT

load_dotenv()

ENDPOINT = "https://api.sarvam.ai/v1/chat/completions"
AUTH_HEADER = "api-subscription-key"
DEFAULT_MODEL = "sarvam-105b"
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 3000
DEFAULT_SEED = 20260917

# sarvam-105b thinks by default at medium effort, and the thinking is billed and counted
# against max_tokens. Left on, it spends the whole budget on reasoning_content and returns
# a null content with finish_reason "length". None disables reasoning completely, which is
# what we want: these prompts ask for a fixed JSON shape, not for deliberation.
DEFAULT_REASONING_EFFORT = None
TIMEOUT_SECONDS = 60.0
RETRIES = 1

CACHE_DIR = os.path.join(REPO_ROOT, "data", "llm_cache")


class LLMError(Exception):
    """Anything that stopped us getting a valid structured answer."""


class LLMAuthError(LLMError):
    """No key, or the key was refused. Sarvam answers 403 for this, not 401."""


class LLMUnavailable(LLMError):
    """No cached answer in replay mode, or the network gave up."""


class LLMInvalidOutput(LLMError):
    """The model answered, but not in the shape we asked for."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def mode() -> str:
    value = (os.getenv("LLM_MODE") or "record").strip().lower()
    if value not in ("live", "record", "replay"):
        raise LLMError("LLM_MODE must be live, record or replay, got %r" % value)
    return value


def model_id() -> str:
    return (os.getenv("SARVAM_LLM_MODEL") or DEFAULT_MODEL).strip()


def _api_key() -> str:
    key = os.getenv("SARVAM_API_KEY")
    if not key or not key.strip():
        raise LLMAuthError("SARVAM_API_KEY is not set in the environment")
    return key.strip()


def key_is_present() -> bool:
    """Whether a key exists. Never reveals it, not even its length."""
    return bool((os.getenv("SARVAM_API_KEY") or "").strip())


# --------------------------------------------------------------------------
# Safe parsing. No regex anywhere: fences are sliced, values come from json.loads.
# --------------------------------------------------------------------------


def strip_code_fences(text: str) -> str:
    """Pulls the body out of a fenced block if the model wrapped its JSON in one."""
    body = (text or "").strip()
    start = body.find("```")
    if start == -1:
        return body
    after_open = body.find("\n", start)
    if after_open == -1:
        return body[start + 3:].strip()
    end = body.find("```", after_open)
    if end == -1:
        return body[after_open + 1:].strip()
    return body[after_open + 1:end].strip()


def parse_json(text: str) -> Any:
    """json.loads on the stripped body. Never a regex, never a substring hunt for values."""
    body = strip_code_fences(text)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMInvalidOutput("model did not return parseable JSON: %s" % exc) from exc


def validate(payload: Any, schema: type) -> BaseModel:
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise LLMInvalidOutput("model JSON did not match %s: %s"
                               % (schema.__name__, exc.error_count())) from exc


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def cache_key(request: dict) -> str:
    canonical = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, "%s.json" % key)


def cache_read(key: str) -> dict | None:
    path = cache_path(key)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def cache_write(key: str, request: dict, content: str, label: str) -> None:
    """Saves the prompt and the answer. The key is not part of the request dict, so it
    cannot land here."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path(key), "w", encoding="utf-8") as handle:
        json.dump({"label": label, "request": request, "content": content},
                  handle, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# The call
# --------------------------------------------------------------------------


def _post(request: dict) -> str:
    """One HTTP call plus one retry. Headers are built here and never leave this function."""
    headers = {AUTH_HEADER: _api_key(), "Content-Type": "application/json"}
    last: Exception | None = None

    for attempt in range(RETRIES + 1):
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.post(ENDPOINT, headers=headers, json=request)
        except httpx.HTTPError as exc:
            last = LLMUnavailable("network call to Sarvam failed: %s" % type(exc).__name__)
            continue

        if response.status_code == 403:
            # Sarvam answers 403 for auth failures, not 401. Do not retry, do not debug.
            raise LLMAuthError("Sarvam refused the request with 403. Check the key or the "
                               "account, not the code.")
        if response.status_code == 429 or response.status_code >= 500:
            last = LLMUnavailable("Sarvam returned %d" % response.status_code)
            continue
        if response.status_code >= 400:
            raise LLMError("Sarvam returned %d for this request" % response.status_code)

        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMInvalidOutput("unexpected response envelope from Sarvam: %s"
                                   % type(exc).__name__) from exc

        if not content:
            raise LLMInvalidOutput(
                "Sarvam returned empty content, finish_reason %r. If reasoning is on it "
                "can spend the whole token budget before answering."
                % choice.get("finish_reason"))
        return content

    raise last if last else LLMUnavailable("Sarvam call failed")


def complete(messages: list, schema: type, label: str = "",
             temperature: float = DEFAULT_TEMPERATURE,
             max_tokens: int = DEFAULT_MAX_TOKENS,
             seed: int = DEFAULT_SEED,
             reasoning_effort: str | None = DEFAULT_REASONING_EFFORT) -> tuple:
    """Returns (validated pydantic object, meta dict). Raises on anything less."""
    request = {
        "model": model_id(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "seed": seed,
        "reasoning_effort": reasoning_effort,
    }
    key = cache_key(request)
    current = mode()

    cached = cache_read(key) if current in ("record", "replay") else None
    if cached is not None:
        return validate(parse_json(cached["content"]), schema), {
            "mode": current, "source": "cache", "cache_key": key, "model": request["model"],
            "label": label,
        }

    if current == "replay":
        raise LLMUnavailable(
            "replay mode and nothing cached for %s (key %s). Run once in record mode first."
            % (label or "this prompt", key))

    content = _post(request)
    result = validate(parse_json(content), schema)
    if current == "record":
        cache_write(key, request, content, label)
    return result, {"mode": current, "source": "sarvam", "cache_key": key,
                    "model": request["model"], "label": label}
