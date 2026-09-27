"""
LLM-written replies for open questions and engaged messages.

The reply brain (rule-based) handles opt-outs, auto-replies, hostility and
commitments on its own. Only when the merchant asks something or engages
openly does it ask this module for a real, grounded answer. If the LLM is slow,
unavailable, or its answer fails validation, the rule-based reply is kept.
"""
import asyncio

from app.facts import build_facts
from app.llm import complete_json
from app.store import store
from app.validator import validate

REPLY_TIMEOUT_SECONDS = 8

SYSTEM = """You are Vera, magicpin's AI assistant for Indian local merchants, replying INSIDE an ongoing WhatsApp conversation.
Rules:
1. Answer their latest message directly, using ONLY facts from the FACT SHEET. Never invent numbers, prices, dates, names or features.
   If the fact sheet can't answer it, say so honestly in one line and offer the concrete next step instead.
2. Don't re-introduce yourself. Don't repeat your earlier message. No preamble.
3. If they showed interest, move FORWARD (deliver or propose the concrete next step). Never ask more qualifying questions.
4. End with exactly ONE easy ask. 1-3 short sentences. No URLs.
5. Match their language: if they wrote Hinglish, reply in Hinglish (Roman script); Vera is female ("karti hoon").
6. Customer-facing conversations: speak as the business; never mention Vera, magicpin, metrics or peers.
OUTPUT only JSON: {"body": "...", "cta": "binary_yes_no|binary_confirm_cancel|open_ended|none", "rationale": "..."}"""


def _facts_for(conv: dict) -> dict:
    trigger = store.get("trigger", conv.get("trigger_id")) or {}
    merchant = store.get("merchant", conv.get("merchant_id")) or {}
    category = store.get("category", merchant.get("category_slug")) or {}
    customer = store.get("customer", conv.get("customer_id")) if conv.get("customer_id") else None
    return build_facts(category, merchant, trigger, customer)


async def write_reply(conv: dict, message: str, intent: str) -> dict | None:
    """Returns {"body", "cta", "rationale"} or None (caller keeps its rule-based reply)."""
    try:
        f = _facts_for(conv)
        earlier = conv["bot_bodies"][:-1][-3:]          # last one is the rule-based draft we may replace
        prompt = "\n".join([
            f"CONVERSATION TOPIC: {conv.get('topic') or 'general growth help'}",
            f"IF THEY SAY YES, WE WILL: {conv.get('next_action') or f['next_action']}",
            f"ADDRESS THEM AS: {f['salutation']}",
            "OUR EARLIER MESSAGES:", *([f"- {b}" for b in earlier] or ["- (none)"]),
            f"THEIR LATEST MESSAGE ({intent}): {message}",
            "", "FACT SHEET:", *[f"- {line}" for line in f["facts"]],
        ])
        data, source = await asyncio.wait_for(complete_json(SYSTEM, prompt), timeout=REPLY_TIMEOUT_SECONDS)
        if not isinstance(data, dict) or not data.get("body"):
            return None
        # Replies may skip the name, so don't require it; everything else must pass.
        problems = [p for p in validate(data["body"], data.get("cta", "open_ended"), f, conv["bot_bodies"][:-1])
                    if not p.startswith("Address them by name")]
        if problems:
            return None
        return {"body": data["body"].strip(), "cta": data.get("cta", "open_ended"),
                "rationale": f"{data.get('rationale', '')} [{source}]".strip()}
    except Exception:
        return None
