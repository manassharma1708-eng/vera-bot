"""
Conversation memory.

Tracks every conversation the bot is part of, plus per-merchant flags
(opted out, auto-reply streak) that must survive across conversations.
"""
import threading

_lock = threading.Lock()
_conversations: dict[str, dict] = {}
_merchant_flags: dict[str, dict] = {}


def _new_conversation(conv_id: str) -> dict:
    return {
        "conversation_id": conv_id,
        "merchant_id": None,
        "customer_id": None,
        "trigger_id": None,
        "trigger_kind": None,
        "topic": None,          # short human description of what we pitched
        "next_action": None,    # what we'll do if they say yes, e.g. "draft 3 Google posts"
        "bot_bodies": [],       # everything we've sent (for anti-repetition)
        "their_messages": [],   # everything they've sent
        "status": "active",     # active | waiting | ended
        "offtopic_count": 0,
        "last_activity": None,  # ISO time of our last send or their last reply
    }


def get_or_create(conv_id: str) -> dict:
    with _lock:
        if conv_id not in _conversations:
            _conversations[conv_id] = _new_conversation(conv_id)
        return _conversations[conv_id]


def start(conv_id: str, merchant_id, customer_id, trigger_id, trigger_kind,
          topic, next_action, first_body, now: str | None = None):
    """Called by /v1/tick (Step 7) when the bot opens a new conversation."""
    conv = get_or_create(conv_id)
    with _lock:
        conv.update({
            "merchant_id": merchant_id, "customer_id": customer_id,
            "trigger_id": trigger_id, "trigger_kind": trigger_kind,
            "topic": topic, "next_action": next_action, "last_activity": now,
        })
        conv["bot_bodies"].append(first_body)
    return conv


def exists(conv_id: str) -> bool:
    with _lock:
        return conv_id in _conversations


def merchant_flags(merchant_id: str | None) -> dict:
    key = merchant_id or "_unknown"
    with _lock:
        if key not in _merchant_flags:
            _merchant_flags[key] = {"opted_out": False, "auto_reply_streak": 0, "last_auto_text": None,
                                    "wait_until": None, "sent_bodies": []}
        return _merchant_flags[key]


def active_conversation(recipient_key: str) -> dict | None:
    """An open (not ended) conversation with this merchant/customer, if any."""
    with _lock:
        for conv in _conversations.values():
            key = conv.get("customer_id") or conv.get("merchant_id")
            if key == recipient_key and conv["status"] != "ended":
                return conv
    return None


_sent_suppression_keys: set[str] = set()


def was_sent(suppression_key: str) -> bool:
    with _lock:
        return suppression_key in _sent_suppression_keys


def mark_sent(suppression_key: str):
    with _lock:
        _sent_suppression_keys.add(suppression_key)


def is_opted_out(merchant_id: str | None) -> bool:
    return merchant_flags(merchant_id)["opted_out"]


def clear():
    with _lock:
        _conversations.clear()
        _merchant_flags.clear()
        _sent_suppression_keys.clear()
