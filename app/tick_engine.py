"""
Tick engine: runs on every POST /v1/tick.

Decides WHICH triggers deserve a message right now, composes them in parallel
within the time budget, and records everything so replies can continue the thread.

Decision rules (in order):
  1. trigger, merchant and category must all be known
  2. never send twice for the same suppression_key
  3. never message an opted-out merchant, or one we were asked to wait on
  4. respect the fact sheet's blockers (consent, trigger/category mismatch)
  5. don't open a second thread with someone who has an open conversation,
     unless the new trigger is urgent (4-5)
  6. ONE message per recipient per tick: the highest-priority trigger wins
  7. at most 20 actions per tick
"""
import asyncio
import hashlib
import os
import time

from app import conversations
from app.composer import compose, fallback_message
from app.facts import build_facts
from app.store import store
from app.timeutil import parse_iso

MAX_ACTIONS = 20
TICK_BUDGET_SECONDS = float(os.getenv("TICK_BUDGET_SECONDS", "11"))   # judge_simulator times out at 15s
COMPOSE_CONCURRENCY = int(os.getenv("COMPOSE_CONCURRENCY", "8"))


def _priority(trigger: dict, facts: dict) -> tuple:
    """Higher sorts first: urgency, then real event data over placeholders, then customer-facing."""
    return (
        int(trigger.get("urgency") or 0),
        0 if facts.get("is_placeholder") else 1,
        0 if facts.get("warnings") else 1,
        1 if facts["send_as"] == "merchant_on_behalf" else 0,
        trigger.get("id", ""),   # stable tie-break -> deterministic
    )


def _conversation_id(merchant_id: str, customer_id: str | None, trigger_id: str) -> str:
    who = customer_id or merchant_id
    short = hashlib.sha1(f"{who}:{trigger_id}".encode()).hexdigest()[:6]
    return f"conv_{who[:28]}_{trigger_id[:24]}_{short}"


def select_candidates(now: str, trigger_ids: list[str]) -> tuple[list[dict], list[str]]:
    """Returns (chosen candidates, list of skip reasons for logging)."""
    now_dt = parse_iso(now)
    skipped, by_recipient = [], {}

    for trigger_id in dict.fromkeys(trigger_ids):          # de-duplicate, keep order
        trigger = store.get("trigger", trigger_id)
        if not trigger:
            skipped.append(f"{trigger_id}: trigger context never pushed")
            continue
        merchant_id = trigger.get("merchant_id") or (trigger.get("payload") or {}).get("merchant_id")
        merchant = store.get("merchant", merchant_id)
        if not merchant:
            skipped.append(f"{trigger_id}: merchant {merchant_id} unknown")
            continue
        category = store.get("category", merchant.get("category_slug"))
        if not category:
            skipped.append(f"{trigger_id}: category {merchant.get('category_slug')} unknown")
            continue
        customer_id = trigger.get("customer_id")
        customer = store.get("customer", customer_id) if customer_id else None

        facts = build_facts(category, merchant, trigger, customer)
        key = facts["suppression_key"]
        flags = conversations.merchant_flags(merchant.get("merchant_id"))
        wait_until = parse_iso(flags.get("wait_until"))

        if conversations.was_sent(key):
            skipped.append(f"{trigger_id}: already sent (suppression_key {key})")
            continue
        if flags.get("opted_out"):
            skipped.append(f"{trigger_id}: merchant opted out")
            continue
        if wait_until and now_dt and now_dt < wait_until and not customer_id:
            skipped.append(f"{trigger_id}: merchant asked us to wait until {flags['wait_until']}")
            continue
        if facts["send_blockers"]:
            skipped.append(f"{trigger_id}: blocked ({facts['send_blockers'][0]})")
            continue
        recipient = customer_id or merchant.get("merchant_id")
        if conversations.active_conversation(recipient) and int(trigger.get("urgency") or 0) < 4:
            skipped.append(f"{trigger_id}: conversation already open with {recipient}")
            continue

        cand = {"trigger": trigger, "merchant": merchant, "category": category,
                "customer": customer, "facts": facts, "recipient": recipient}
        best = by_recipient.get(recipient)
        if best is None or _priority(trigger, facts) > _priority(best["trigger"], best["facts"]):
            if best:
                skipped.append(f"{best['trigger']['id']}: lower priority than {trigger_id} for same recipient")
            by_recipient[recipient] = cand
        else:
            skipped.append(f"{trigger_id}: lower priority than {best['trigger']['id']} for same recipient")

    chosen = sorted(by_recipient.values(), key=lambda c: _priority(c["trigger"], c["facts"]), reverse=True)
    for extra in chosen[MAX_ACTIONS:]:
        skipped.append(f"{extra['trigger']['id']}: over the {MAX_ACTIONS}-actions-per-tick cap")
    return chosen[:MAX_ACTIONS], skipped


async def run_tick(now: str, trigger_ids: list[str]) -> dict:
    started = time.time()
    chosen, skipped = select_candidates(now, trigger_ids)
    if not chosen:
        return {"actions": [], "_skipped": skipped}

    sem = asyncio.Semaphore(COMPOSE_CONCURRENCY)

    async def one(c):
        async with sem:
            flags = conversations.merchant_flags(c["merchant"].get("merchant_id"))
            return await compose(c["category"], c["merchant"], c["trigger"], c["customer"],
                                 facts=c["facts"], avoid_bodies=flags.get("sent_bodies"))

    tasks = [asyncio.create_task(one(c)) for c in chosen]
    remaining = max(1.0, TICK_BUDGET_SECONDS - (time.time() - started))
    done, pending = await asyncio.wait(tasks, timeout=remaining)
    for t in pending:
        t.cancel()   # out of time: these get the instant fact-only template below

    actions = []
    for c, task in zip(chosen, tasks):
        msg = None
        if task in done and not task.cancelled() and task.exception() is None:
            msg = task.result()
        if msg is None:
            fb = fallback_message(c["facts"])
            msg = {"body": fb["body"], "cta": fb["cta"], "rationale": fb["rationale"],
                   "topic": fb["topic"], "send_as": c["facts"]["send_as"],
                   "suppression_key": c["facts"]["suppression_key"], "next_action": c["facts"]["next_action"],
                   "template_name": f"{'merchant' if c['facts']['send_as'] == 'merchant_on_behalf' else 'vera'}_{c['facts']['kind']}_v1",
                   "template_params": [c["facts"]["salutation"], fb["body"], ""], "source": "template (tick time budget)"}

        trigger, merchant, customer = c["trigger"], c["merchant"], c["customer"]
        merchant_id = merchant.get("merchant_id")
        customer_id = customer.get("customer_id") if customer else None
        conv_id = _conversation_id(merchant_id, customer_id, trigger["id"])

        conversations.start(conv_id, merchant_id, customer_id, trigger["id"], trigger.get("kind"),
                            msg["topic"], msg["next_action"], msg["body"])
        conversations.mark_sent(msg["suppression_key"])
        conversations.merchant_flags(merchant_id)["sent_bodies"].append(msg["body"])

        why = (f"[{trigger.get('kind')}, urgency {trigger.get('urgency')}] "
               f"Anchor: {c['facts']['anchor'][:90]}. {msg['rationale']}")
        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": msg["send_as"],
            "trigger_id": trigger["id"],
            "template_name": msg["template_name"],
            "template_params": msg["template_params"],
            "body": msg["body"],
            "cta": msg["cta"],
            "suppression_key": msg["suppression_key"],
            "rationale": why[:600],
        })
    return {"actions": actions, "_skipped": skipped}
