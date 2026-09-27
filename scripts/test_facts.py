"""
Builds the fact sheet for all 30 canonical test pairs and prints a summary.
No server needed.

Run from the vera-bot folder:
    python scripts/test_facts.py            # summary of all 30
    python scripts/test_facts.py T06        # full fact sheet for one test
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.facts import build_facts  # noqa: E402

DATA = Path("challenge/expanded")


def load(folder, item_id):
    if not item_id:
        return None
    path = DATA / folder / f"{item_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


pairs = json.loads((DATA / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]
only = sys.argv[1] if len(sys.argv) > 1 else None

sendable = 0
for p in pairs:
    if only and p["test_id"] != only:
        continue
    trigger = load("triggers", p["trigger_id"])
    merchant = load("merchants", p["merchant_id"])
    category = load("categories", merchant["category_slug"]) if merchant else None
    customer = load("customers", p.get("customer_id"))
    f = build_facts(category, merchant, trigger, customer)

    if only:
        print(json.dumps(f, indent=2, ensure_ascii=False))
        break

    status = "SKIP" if f["send_blockers"] else "SEND"
    sendable += status == "SEND"
    print(f"{p['test_id']} {status} {f['kind']:<26} {f['send_as']:<18} anchor: {f['anchor'][:70]}")
    for b in f["send_blockers"]:
        print(f"      blocked: {b}")

if not only:
    print(f"\n{sendable}/30 sendable, {30 - sendable} blocked")
