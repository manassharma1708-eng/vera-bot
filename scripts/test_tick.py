"""
End-to-end test: pushes the dataset + all 100 triggers to your running bot,
calls /v1/tick like the judge does, then plays a merchant reply.

Run from the vera-bot folder (server running in Terminal 1, freshly restarted):
    python scripts/test_tick.py
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BOT_URL = "http://localhost:8080"
DATA = Path("challenge/expanded")
REQUIRED = ["conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
            "template_params", "body", "cta", "suppression_key", "rationale"]


def push(c, scope, cid, payload, version=1):
    return c.post(f"{BOT_URL}/v1/context", json={
        "scope": scope, "context_id": cid, "version": version, "payload": payload,
        "delivered_at": datetime.now(timezone.utc).isoformat()})


with httpx.Client(timeout=40) as c:
    # 1. Push everything (categories, merchants, customers, triggers)
    for scope, folder, key in [("category", "categories", "slug"), ("merchant", "merchants", "merchant_id"),
                               ("customer", "customers", "customer_id"), ("trigger", "triggers", "id")]:
        for f in sorted((DATA / folder).glob("*.json")):
            p = json.loads(f.read_text(encoding="utf-8"))
            push(c, scope, p[key], p)
    print("contexts:", c.get(f"{BOT_URL}/v1/healthz").json()["contexts_loaded"])

    # 2. Tick with the 30 canonical test-pair triggers (like the judge's "available_triggers")
    pairs = json.loads((DATA / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]
    trigger_ids = [p["trigger_id"] for p in pairs]
    start = time.time()
    r = c.post(f"{BOT_URL}/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": trigger_ids})
    secs = time.time() - start
    actions = r.json()["actions"]
    print(f"\ntick 1: {len(actions)} actions in {secs:.1f}s (judge_simulator allows 15s)")

    problems = 0
    for a in actions:
        missing = [k for k in REQUIRED if k not in a]
        problems += bool(missing)
        print(f"\n  {a['trigger_id'][:40]} -> {a['send_as']}  cta={a['cta']}")
        print(f"    {a['body'][:180]}")
        if missing:
            print(f"    MISSING FIELDS: {missing}")
    recipients = [a.get("customer_id") or a["merchant_id"] for a in actions]
    print(f"\nall required fields present: {'PASS' if not problems else 'FAIL'}")
    print(f"one message per recipient:  {'PASS' if len(recipients) == len(set(recipients)) else 'FAIL'}")
    print(f"within 20-action cap:       {'PASS' if len(actions) <= 20 else 'FAIL'}")

    # 3. Same tick again -> suppression should stop every repeat
    r2 = c.post(f"{BOT_URL}/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": trigger_ids})
    repeats = {a["suppression_key"] for a in r2.json()["actions"]} & {a["suppression_key"] for a in actions}
    print(f"no repeats on 2nd tick:     {'PASS' if not repeats else 'FAIL'} ({len(r2.json()['actions'])} new actions)")

    # 4. Play the merchant: say yes to the first merchant-facing message
    first = next((a for a in actions if a["send_as"] == "vera"), None)
    if first:
        rep = c.post(f"{BOT_URL}/v1/reply", json={
            "conversation_id": first["conversation_id"], "merchant_id": first["merchant_id"], "customer_id": None,
            "from_role": "merchant", "message": "Yes, go ahead", "received_at": "2026-04-26T10:40:00Z",
            "turn_number": 2}).json()
        print(f"\nreply to 'Yes, go ahead' on {first['trigger_id'][:30]}:")
        print(f"  {rep['action']}: {rep.get('body', rep.get('rationale'))}")

        rep = c.post(f"{BOT_URL}/v1/reply", json={
            "conversation_id": first["conversation_id"], "merchant_id": first["merchant_id"], "customer_id": None,
            "from_role": "merchant", "message": "How long will it take and what do you need from me?",
            "received_at": "2026-04-26T10:42:00Z", "turn_number": 3}).json()
        print(f"reply to a follow-up question:\n  {rep['action']}: {rep.get('body', rep.get('rationale'))}")
