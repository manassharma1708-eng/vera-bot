"""
Checks every provider/model in LLM_ORDER (from .env) with a tiny request.

Run from the vera-bot folder:
    python scripts/check_llm.py
"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import llm  # noqa: E402


async def list_models(provider):
    key = llm._cfg(llm.KEY_ENV[provider])
    async with httpx.AsyncClient(timeout=15) as c:
        if provider == "gemini":
            r = await c.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key})
            names = [m["name"].replace("models/", "") for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in m["name"]]
        elif provider == "groq":
            r = await c.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"})
            names = sorted(m["id"] for m in r.json().get("data", []))
        else:
            return
    print(f"   {provider} models your key can use: {', '.join(names[:15])}")


async def main():
    providers = llm.configured_providers()
    print("LLM_ORDER =", llm._cfg("LLM_ORDER", "gemini,groq (default)"))
    if not providers:
        print("No provider has an API key — check your .env file")
        return
    async with httpx.AsyncClient(timeout=20) as client:
        for provider, model in providers:
            try:
                text = await llm._call(client, provider, model,
                                       'Reply with only this JSON: {"status": "ok"}', "test")
                ok = llm._extract_json(text).get("status") == "ok"
                print(f"[{'PASS' if ok else 'FAIL'}] {provider}:{model}")
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                note = "rate limited right now (key and model are fine)" if code == 429 else exc.response.text[:200]
                print(f"[{'WAIT' if code == 429 else 'FAIL'}] {provider}:{model} -> HTTP {code}: {note}")
                if code == 404:
                    await list_models(provider)
            except Exception as exc:
                print(f"[FAIL] {provider}:{model} -> {type(exc).__name__}: {str(exc)[:200]}")


asyncio.run(main())
