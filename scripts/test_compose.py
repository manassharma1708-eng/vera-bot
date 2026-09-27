"""
Composes the real message for the 30 canonical test pairs (skips blocked ones).
No server needed. Uses your LLM keys from .env.

Run from the vera-bot folder:
    python scripts/test_compose.py          # all sendable pairs
    python scripts/test_compose.py T24      # just one
Results are also saved to scripts/compose_output.json
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.composer import compose  # noqa: E402
from app.facts import build_facts  # noqa: E402

DATA = Path("challenge/expanded")
CONCURRENCY = 1   # one at a time: free tiers allow only a few requests per minute
PAUSE_SECONDS = 3  # gap between LLM calls (cached results skip the pause)


def load(folder, item_id):
    if not item_id:
        return None
    path = DATA / folder / f"{item_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


async def run_one(p, sem):
    trigger = load("triggers", p["trigger_id"])
    merchant = load("merchants", p["merchant_id"])
    category = load("categories", merchant["category_slug"])
    customer = load("customers", p.get("customer_id"))
    f = build_facts(category, merchant, trigger, customer)
    if f["send_blockers"]:
        return p["test_id"], {"skipped": f["send_blockers"]}, 0.0
    async with sem:
        start = time.time()
        msg = await compose(category, merchant, trigger, customer, facts=f)
        if "cached" not in msg["source"]:
            await asyncio.sleep(PAUSE_SECONDS)
        return p["test_id"], msg, time.time() - start


async def main():
    pairs = json.loads((DATA / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]
    if len(sys.argv) > 1:
        pairs = [p for p in pairs if p["test_id"] == sys.argv[1]]
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(run_one(p, sem) for p in pairs))

    saved = {}
    for test_id, msg, secs in results:
        if "skipped" in msg:
            print(f"\n{test_id}  SKIPPED: {msg['skipped'][0]}")
            saved[test_id] = msg
            continue
        print(f"\n{test_id}  [{msg['facts']['kind']}] -> {msg['send_as']}  ({secs:.1f}s, {msg['source'][:60]})")
        print(f"  BODY: {msg['body']}")
        print(f"  CTA:  {msg['cta']}   RATIONALE: {msg['rationale'][:140]}")
        saved[test_id] = {k: v for k, v in msg.items() if k != "facts"}

    Path("scripts/compose_output.json").write_text(json.dumps(saved, indent=2, ensure_ascii=False), encoding="utf-8")
    llm = sum(1 for m in saved.values() if "source" in m and not m["source"].startswith("template"))
    tmpl = sum(1 for m in saved.values() if "source" in m and m["source"].startswith("template"))
    print(f"\nDone: {llm} written by LLM, {tmpl} template fallbacks, "
          f"{sum(1 for m in saved.values() if 'skipped' in m)} skipped. Saved to scripts/compose_output.json")


asyncio.run(main())