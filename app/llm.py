"""
LLM client with automatic fallback, rate-limit handling and caching.

Settings come from the .env file. LLM_ORDER lists what to try, in order.
Each entry is "provider" or "provider:model". Different models have SEPARATE
free-tier quotas, so listing several gives you more capacity:

    LLM_ORDER=gemini:gemini-flash-latest,gemini:gemini-3.1-flash-lite,groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b
    GEMINI_API_KEY=...
    GROQ_API_KEY=...
    OPENAI_API_KEY=...      (optional)
    ANTHROPIC_API_KEY=...   (optional)

Determinism: temperature 0 + a cache keyed by the prompt hash. The cache is also
saved to disk (.llm_cache.json) so restarting the server doesn't change outputs
or burn quota again.
"""
import asyncio
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT", "15"))
MAX_RETRY_WAIT = float(os.getenv("LLM_MAX_RETRY_WAIT", "6"))   # seconds we're willing to wait on a 429
CACHE_FILE = Path(os.getenv("LLM_CACHE_FILE", ".llm_cache.json"))

KEY_ENV = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY",
           "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
DEFAULT_MODEL = {"gemini": "gemini-flash-latest", "groq": "openai/gpt-oss-120b",
                 "openai": "gpt-4o-mini", "anthropic": "claude-sonnet-4-5"}

_cooldown_until: dict[str, float] = {}     # "provider:model" -> unix time it's usable again
_cache_lock = threading.Lock()


def _cfg(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


# ------------------------------------------------------------------ cache

def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_cache: dict[str, dict] = _load_cache()


def _save_cache():
    try:
        with _cache_lock:
            CACHE_FILE.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # a failed cache write must never break a response


# ------------------------------------------------------------------ config

def configured_providers() -> list[tuple[str, str]]:
    """[(provider, model), ...] in the order to try, only those with an API key."""
    result = []
    for entry in _cfg("LLM_ORDER", "gemini,groq").split(","):
        entry = entry.strip()
        if not entry:
            continue
        provider, _, model = entry.partition(":")
        provider = provider.strip().lower()
        if provider not in KEY_ENV or not _cfg(KEY_ENV[provider]):
            continue
        model = model.strip() or _cfg(f"{provider.upper()}_MODEL", DEFAULT_MODEL[provider])
        result.append((provider, model))
    return result


def _extract_json(text: str) -> dict:
    """Parse JSON even if the model wrapped it in ```json fences or added words around it."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise
        return json.loads(match.group())


def _retry_after_seconds(resp: httpx.Response) -> float:
    """How long the provider asks us to wait after a 429."""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', resp.text)   # Gemini format
    if match:
        return float(match.group(1))
    match = re.search(r"try again in (\d+(?:\.\d+)?)(ms|s)", resp.text)   # Groq message format
    if match:
        value = float(match.group(1))
        return value / 1000 if match.group(2) == "ms" else value
    return 60.0


# ------------------------------------------------------------------ provider calls

async def _call(client: httpx.AsyncClient, provider: str, model: str, system: str, user: str) -> str:
    if provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = await client.post(url, params={"key": _cfg("GEMINI_API_KEY")}, json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 4096,
                                 "responseMimeType": "application/json"},
        })
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    if provider in ("groq", "openai"):
        base = "https://api.groq.com/openai/v1" if provider == "groq" else "https://api.openai.com/v1"
        body = {
            "model": model, "temperature": 0, "seed": 7, "max_tokens": 1200,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if "gpt-oss" in model:
            # reasoning model: keep thinking short so it stays fast and leaves room for the answer
            body["reasoning_effort"] = "low"
            body["max_tokens"] = 4000
        r = await client.post(f"{base}/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {_cfg(KEY_ENV[provider])}"})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    if provider == "anthropic":
        r = await client.post("https://api.anthropic.com/v1/messages", headers={
            "x-api-key": _cfg("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01",
        }, json={
            "model": model, "max_tokens": 1200, "temperature": 0, "system": system,
            "messages": [{"role": "user", "content": user}],
        })
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])

    raise ValueError(f"unknown provider {provider}")


# ------------------------------------------------------------------ main entry

async def complete_json(system: str, user: str) -> tuple[dict | None, str]:
    """
    Returns (parsed_json, source). source is e.g. "gemini:gemini-flash-latest",
    or "none | <errors>" if every provider failed.
    """
    key = hashlib.sha256(f"{system}\n---\n{user}".encode()).hexdigest()
    if key in _cache:
        return _cache[key]["data"], _cache[key]["source"] + " (cached)"

    errors = []
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for provider, model in configured_providers():
            name = f"{provider}:{model}"
            if _cooldown_until.get(name, 0) > time.time():
                errors.append(f"{name}: cooling down after rate limit")
                continue
            for attempt in range(2):
                try:
                    text = await _call(client, provider, model, system, user)
                    data = _extract_json(text)
                    _cache[key] = {"data": data, "source": name}
                    _save_cache()
                    return data, name
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 429:
                        wait = _retry_after_seconds(exc.response)
                        if attempt == 0 and wait <= MAX_RETRY_WAIT:
                            await asyncio.sleep(wait + 0.5)
                            continue          # short wait -> retry the same model once
                        _cooldown_until[name] = time.time() + min(wait, 300)
                    errors.append(f"{name}: HTTP {exc.response.status_code} {exc.response.text[:120]}")
                    break
                except Exception as exc:     # timeout, bad JSON, network...
                    errors.append(f"{name}: {type(exc).__name__} {str(exc)[:120]}")
                    break
    return None, ("none | " + " || ".join(errors)) if errors else "none | no provider configured"
