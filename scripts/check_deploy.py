"""
Checks your DEPLOYED bot without leaving test data behind.

Run from the vera-bot folder:
    python scripts/check_deploy.py https://your-app.up.railway.app

It calls healthz, metadata, an empty tick and a reply, then /v1/teardown so the
bot is empty again when the real judge arrives (the judge's warmup expects
exactly 5/50/200 contexts, pushed by itself).
"""
import sys
import time

import httpx

if len(sys.argv) < 2:
    sys.exit("Usage: python scripts/check_deploy.py https://your-app.up.railway.app")
URL = sys.argv[1].rstrip("/")
ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


with httpx.Client(timeout=30) as c:
    t = time.time()
    r = c.get(f"{URL}/v1/healthz")
    check("healthz", r.status_code == 200 and r.json().get("status") == "ok", f"({(time.time() - t) * 1000:.0f} ms)")
    print("        contexts_loaded:", r.json().get("contexts_loaded"))

    r = c.get(f"{URL}/v1/metadata")
    meta = r.json()
    check("metadata", r.status_code == 200 and meta.get("team_name") not in (None, "YOUR NAME"),
          f"team={meta.get('team_name')} model={meta.get('model')}")
    check("LLM configured", meta.get("model") not in (None, "", "template-only"),
          "(set GEMINI_API_KEY / GROQ_API_KEY / LLM_ORDER in Railway Variables)" if meta.get("model") == "template-only" else "")

    r = c.post(f"{URL}/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": []})
    check("tick (empty)", r.status_code == 200 and r.json() == {"actions": []})

    r = c.post(f"{URL}/v1/reply", json={"conversation_id": "deploy_check", "merchant_id": "deploy_check",
                                        "customer_id": None, "from_role": "merchant",
                                        "message": "Stop messaging me", "received_at": "2026-04-26T10:31:00Z",
                                        "turn_number": 2})
    check("reply", r.status_code == 200 and r.json().get("action") == "end")

    r = c.post(f"{URL}/v1/teardown")
    counts = c.get(f"{URL}/v1/healthz").json().get("contexts_loaded", {})
    check("teardown -> bot is empty for the judge", r.status_code == 200 and not any(counts.values()), str(counts))

print("\nREADY TO SUBMIT" if ok else "\nFix the FAIL lines above before submitting")
