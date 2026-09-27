"""
Pushes the expanded dataset to your running bot, the same way the judge does in warmup.

Run from the vera-bot folder (with the server running in another terminal):
    python scripts/push_dataset.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

BOT_URL = "http://localhost:8080"
DATA = Path("challenge/expanded")

SCOPE_FOLDERS = {
    "category": ("categories", "slug"),
    "merchant": ("merchants", "merchant_id"),
    "customer": ("customers", "customer_id"),
}


def push(client, scope, context_id, version, payload):
    body = {
        "scope": scope,
        "context_id": context_id,
        "version": version,
        "payload": payload,
        "delivered_at": datetime.now(timezone.utc).isoformat(),
    }
    return client.post(f"{BOT_URL}/v1/context", json=body)


def main():
    if not DATA.exists():
        sys.exit("challenge/expanded not found. Run the generator first (Part C4 of Step 1).")

    with httpx.Client(timeout=10) as client:
        # 1. Push the base dataset: categories, merchants, customers (no triggers, like the judge)
        for scope, (folder, id_field) in SCOPE_FOLDERS.items():
            ok = fail = 0
            for f in sorted((DATA / folder).glob("*.json")):
                payload = json.loads(f.read_text(encoding="utf-8"))
                r = push(client, scope, payload[id_field], 1, payload)
                if r.status_code == 200:
                    ok += 1
                else:
                    fail += 1
                    print(f"  FAIL {scope}/{payload[id_field]}: {r.status_code} {r.text[:120]}")
            print(f"{scope:9} pushed OK: {ok:3}   failed: {fail}")

        # 2. Check healthz counts
        health = client.get(f"{BOT_URL}/v1/healthz").json()
        print("\nhealthz contexts_loaded:", health["contexts_loaded"])
        expected = {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}
        print("warmup check:", "PASS" if health["contexts_loaded"] == expected else "FAIL")

        # 3. Idempotency: re-push merchant v1 -> expect 409
        m = json.loads((DATA / "merchants" / "m_001_drmeera_dentist_delhi.json").read_text(encoding="utf-8"))
        r = push(client, "merchant", m["merchant_id"], 1, m)
        print(f"\nsame version re-push -> {r.status_code} (expect 409)  {r.json()}")

        # 4. Version bump: v2 with new views -> expect 200 and the new value stored
        m["performance"]["views"] = 2580
        r = push(client, "merchant", m["merchant_id"], 2, m)
        print(f"version 2 push       -> {r.status_code} (expect 200)")

        # 5. Bad scope -> expect 400
        r = push(client, "banana", "x", 1, {})
        print(f"invalid scope        -> {r.status_code} (expect 400)")


if __name__ == "__main__":
    main()
