"""
Reply brain: decides what to do when a merchant (or customer) replies.

Rule-first on purpose. The dangerous situations (auto-replies, opt-outs, abuse,
"let's do it") are handled by fast, deterministic rules so the bot never
wastes turns or ignores a clear signal. Only open questions will go to the
LLM (added in Step 5).

Order of checks matters:
  opt-out > hostile > auto-reply > busy/later > off-topic > commitment > decline > question > engaged
"""
import hashlib
import re

from app import conversations
from app.store import store

MAX_BOT_TURNS = 5

# ---------------------------------------------------------------- patterns

OPT_OUT = [
    r"\bstop\b", r"unsubscribe", r"not interested", r"no interest", r"don'?t (message|text|contact|send)",
    r"do not (message|text|contact|send)", r"leave me alone", r"remove me", r"\bblock\b",
    r"mat bhejo", r"band karo", r"nahi chahiye", r"interest nahi", r"message mat", r"pareshan mat",
]

HOSTILE = [
    r"useless", r"\bspam\b", r"stupid", r"idiot", r"nonsense", r"rubbish", r"bakwas", r"bekar",
    r"waste of time", r"shut up", r"bothering me", r"irritat", r"fraud", r"scam", r"pagal", r"bewakoof",
]

AUTO_REPLY = [
    r"thank(s| you) for (contacting|reaching|your message|messaging)",
    r"(our|the) team will (respond|reply|get back|contact)",
    r"will get back to you", r"we('ll| will) respond shortly", r"respond (to you )?shortly",
    r"currently (closed|unavailable|away)", r"outside (our )?(business|working) hours",
    r"(i am|i'm|main) (an? |ek )?(automated|auto|virtual) (assistant|reply|response)",
    r"automated (message|response|reply)", r"this is an auto",
    r"jaankari ke liye .*shukriya", r"team tak pahuncha", r"hum jald (hi )?sampark",
]

BUSY = [
    r"\bbusy\b", r"\blater\b", r"baad (mein|me)", r"abhi nahi", r"call (me )?later", r"tomorrow",
    r"kal baat", r"in a meeting", r"driving", r"not now", r"some other time", r"give me (some )?time",
]

OFF_TOPIC = [
    r"\bgst\b", r"income tax", r"\bitr\b", r"\btax(es)? (return|filing)", r"\bloan\b", r"insurance",
    r"\bvisa\b", r"passport", r"electricity bill", r"\bcricket score", r"election", r"stock tip",
    r"crypto", r"recipe for", r"homework", r"\bjob\b.*\bfor me\b",
]

HESITANT = [r"not sure", r"\bunsure\b", r"\bmaybe\b", r"let me think", r"thinking about", r"soch(ta|ti|ke)"]

# Phrases so clear that they mean "act now" even if a question follows ("Ok let's do it. What's next?")
STRONG_COMMIT = [
    r"let'?s do (it|this)", r"lets do", r"go ahead", r"\bproceed\b", r"sign me up", r"do it",
    r"what'?s next", r"\bconfirm(ed)?\b", r"want to join", r"judna hai", r"judrna hai", r"kar (do|dijiye)",
]

COMMIT = [
    r"let'?s do (it|this)", r"lets do", r"go ahead", r"\bproceed\b", r"sign me up", r"\bi'?m in\b",
    r"do it", r"please do", r"send (it|me|the)", r"\bconfirm(ed)?\b", r"i want to join", r"want to join",
    r"\bjoin\b", r"start (it|now)", r"\bsure\b", r"sounds good", r"\bok(ay)?\b.*\b(do|go|start|send)\b",
    r"^(yes|yeah|yep|ya|haan|han|ha|ji|ji haan|ok|okay|done|chalo|chalega|theek hai|thik hai)[\s!.]*$",
    r"\byes\b", r"\bhaan\b", r"kar (do|dijiye|dena)", r"karo\b", r"bhej (do|dijiye)", r"judna hai", r"judrna hai",
]

DECLINE = [
    r"^(no|nope|nah|nahi|nahin|na)[\s!.]*$", r"no thanks", r"not (right )?now", r"not needed",
    r"\bnahi\b", r"don'?t need", r"no need",
]

QUESTION = [r"\?", r"^(what|how|why|when|where|which|who|can|could|is|are|does|do|kya|kaise|kab|kitna)\b"]

HINGLISH_WORDS = {
    "haan", "han", "nahi", "nahin", "karo", "kar", "kya", "hai", "hain", "mujhe", "aap", "aapka", "aapki",
    "bhai", "ji", "theek", "thik", "accha", "acha", "chalega", "bhejo", "mat", "baad", "abhi", "kaise",
    "kitna", "kab", "mein", "hum", "humara", "karna", "chahiye", "dijiye", "shukriya", "dhanyavad",
}


def _matches(text: str, patterns) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify(text: str) -> str:
    t = text.lower().strip()
    if not t:
        return "empty"
    if _matches(t, OPT_OUT):
        return "opt_out"
    if _matches(t, HOSTILE):
        return "hostile"
    if _matches(t, AUTO_REPLY):
        return "auto_reply"
    if _matches(t, BUSY):
        return "busy"
    if _matches(t, OFF_TOPIC):
        return "off_topic"
    if _matches(t, STRONG_COMMIT):
        return "commit"
    if _matches(t, HESITANT):
        return "engaged"
    if _matches(t, COMMIT):
        # "Yes, but how much does it cost?" is really a question
        return "question" if "?" in t else "commit"
    if _matches(t, DECLINE):
        return "decline"
    if _matches(t, QUESTION):
        return "question"
    return "engaged"


def is_hinglish(text: str) -> bool:
    if re.search(r"[\u0900-\u097F]", text):          # Devanagari script
        return True
    words = set(re.findall(r"[a-z]+", text.lower()))
    return len(words & HINGLISH_WORDS) >= 2


# ---------------------------------------------------------------- helpers

def _first_name(merchant: dict | None, customer: dict | None) -> str | None:
    if customer:
        return (customer.get("identity") or {}).get("name")
    if merchant:
        ident = merchant.get("identity") or {}
        owner = ident.get("owner_first_name")
        if owner and merchant.get("category_slug") == "dentists":
            return f"Dr. {owner}"
        return owner
    return None


def _pick(options: list[str], conv: dict, seed: str) -> str:
    """Deterministic choice that never repeats a body already sent in this conversation."""
    start = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(options)
    for i in range(len(options)):
        candidate = options[(start + i) % len(options)]
        if candidate not in conv["bot_bodies"]:
            return candidate
    return options[start] + " 🙏"   # last resort: still not a verbatim repeat


def _send(conv: dict, body: str, cta: str, rationale: str) -> dict:
    conv["bot_bodies"].append(body)
    return {"action": "send", "body": body, "cta": cta, "rationale": rationale}


def _wait(conv: dict, seconds: int, rationale: str) -> dict:
    conv["status"] = "waiting"
    return {"action": "wait", "wait_seconds": seconds, "rationale": rationale}


def _end(conv: dict, rationale: str) -> dict:
    conv["status"] = "ended"
    return {"action": "end", "rationale": rationale}


# ---------------------------------------------------------------- main entry

def handle_reply(conversation_id: str, merchant_id: str | None, customer_id: str | None,
                 from_role: str, message: str, turn_number: int) -> dict:
    conv = conversations.get_or_create(conversation_id)
    conv["merchant_id"] = conv["merchant_id"] or merchant_id
    conv["customer_id"] = conv["customer_id"] or customer_id
    merchant_id = conv["merchant_id"]
    flags = conversations.merchant_flags(merchant_id)

    merchant = store.get("merchant", merchant_id)
    customer = store.get("customer", conv["customer_id"])
    name = _first_name(merchant, customer)
    hi = is_hinglish(message)
    seed = f"{conversation_id}:{turn_number}:{message}"
    intent = classify(message)
    conv["their_messages"].append(message)

    # A merchant who sends a real message is back: reset the auto-reply streak.
    if intent != "auto_reply":
        flags["auto_reply_streak"] = 0
        flags["last_auto_text"] = None
    # A merchant who opted out but now writes back positively has re-opened the door.
    if flags["opted_out"] and intent in ("commit", "question", "engaged"):
        flags["opted_out"] = False

    # Hard stop if we've already talked too much in this conversation.
    bot_turns = len(conv["bot_bodies"])
    if bot_turns >= MAX_BOT_TURNS and intent not in ("commit", "question"):
        return _end(conv, f"Already sent {bot_turns} messages in this conversation; closing to avoid spamming.")

    # 1. Explicit opt-out -> end immediately and never message this merchant again.
    if intent == "opt_out":
        flags["opted_out"] = True
        return _end(conv, "Explicit opt-out. Ending conversation and suppressing all future sends to this merchant.")

    # 2. Hostile without an explicit stop -> one short apology with an easy way out.
    if intent == "hostile":
        if conv.get("apologized"):
            flags["opted_out"] = True
            return _end(conv, "Still frustrated after an apology; exiting and suppressing future sends.")
        conv["apologized"] = True
        body = _pick([
            "Sorry for the bother — that's on me. I'll keep messages rare and only send what's useful for your business. Reply STOP and I won't message again.",
            "Apologies, I don't want to waste your time. If you'd rather not hear from me, just reply STOP and I'll stop right away.",
        ] if not hi else [
            "Maaf kijiye, aapko pareshan karna mera irada nahi tha. Sirf kaam ki baat bhejungi. Agar messages nahi chahiye, bas STOP reply kar dijiye.",
        ], conv, seed)
        return _send(conv, body, "binary_stop",
                     "Merchant frustrated but no explicit opt-out: one apology plus a one-word exit, no pitch.")

    # 3. Auto-reply -> nudge the owner once, then wait, then exit.
    if intent == "auto_reply" or (flags["last_auto_text"] and message.strip() == flags["last_auto_text"]):
        same_text = flags["last_auto_text"] == message.strip()
        flags["auto_reply_streak"] = flags["auto_reply_streak"] + 1 if same_text or flags["auto_reply_streak"] == 0 else 1
        flags["last_auto_text"] = message.strip()
        streak = flags["auto_reply_streak"]
        if streak == 1:
            body = _pick([
                "Looks like an auto-reply 🙂 No rush — when the owner sees this, a simple 'Yes' is all I need to continue.",
                "Seems this went to your auto-responder. Whenever the owner is free, just reply 'Yes' and I'll pick it up from there.",
            ] if not hi else [
                "Lagta hai yeh auto-reply hai 🙂 Owner jab dekhein, bas 'Haan' reply kar dein — main wahi se aage badhaungi.",
            ], conv, seed)
            return _send(conv, body, "binary_yes_no",
                         "First auto-reply detected: one short prompt addressed to the owner, no repeated pitch.")
        if streak == 2:
            return _wait(conv, 86400, "Second auto-reply in a row: owner isn't at the phone. Waiting 24h instead of burning turns.")
        return _end(conv, f"Auto-reply {streak} times in a row with no human reply; closing the conversation.")

    # 4. Busy / later -> back off politely.
    if intent == "busy":
        return _wait(conv, 14400, "Merchant asked for time; backing off 4 hours without sending anything.")

    # 5. Off-topic ask -> decline briefly and steer back once.
    if intent == "off_topic":
        conv["offtopic_count"] += 1
        topic = conv.get("topic") or "your magicpin listing and customer growth"
        if conv["offtopic_count"] > 1:
            return _wait(conv, 14400, "Repeated off-topic asks; pausing instead of looping on a redirect.")
        body = (f"That one's best handled by your CA or the right expert — it's outside what I can do. "
                f"I'm here for {topic}. Shall I continue with that?") if not hi else (
                f"Yeh kaam aapke CA ya expert ka hai — main isme madad nahi kar paungi. "
                f"Main {topic} mein help karti hoon. Usi pe aage badhein?")
        return _send(conv, body, "binary_yes_no",
                     "Out-of-scope request politely declined; redirected to the original thread with one yes/no.")

    # 6. Commitment -> switch to ACTION immediately. No more questions.
    if intent == "commit":
        action = conv.get("next_action")
        greet = f"{name}, " if name else ""
        if action:
            body = (f"Done — {greet}starting now: {action}. "
                    f"I'll send it here for a final look. Reply CONFIRM once you've checked it and I'll take it live.")
            if hi:
                body = (f"Ho gaya — {greet}abhi shuru kar rahi hoon: {action}. "
                        f"Final check ke liye yahin bhejungi. Theek lage toh CONFIRM reply kar dijiye, main live kar dungi.")
        else:
            body = (f"Done — {greet}I'm on it. Sending you the draft here next; reply CONFIRM once you've "
                    f"checked it and I'll take it live.")
            if hi:
                body = (f"Ho gaya — {greet}main kaam shuru kar rahi hoon. Draft yahin bhej rahi hoon; "
                        f"check karke CONFIRM reply kar dijiye, main live kar dungi.")
        if body in conv["bot_bodies"]:
            body = ("Sending the draft next — reply CONFIRM when you've checked it." if not hi
                    else "Draft bhej rahi hoon — check karke CONFIRM reply kar dijiye.")
        return _send(conv, body, "binary_confirm_cancel",
                     "Merchant committed; switched from pitching to execution with a single confirm step (no qualifying questions).")

    # 7. Soft decline -> graceful exit, door left open.
    if intent == "decline":
        body = ("No problem at all. I'll leave it here — just message 'Hi Vera' anytime you want to pick it up." if not hi
                else "Koi baat nahi. Jab bhi zarurat ho, bas 'Hi Vera' likh dijiye.")
        conv["bot_bodies"].append(body)
        conv["status"] = "ended"
        return {"action": "send", "body": body, "cta": "none",
                "rationale": "Soft decline: one polite close with a re-entry path, then the conversation ends."}

    # 8. Question / 9. Engaged -> placeholder until the LLM composer (Step 5).
    topic = conv.get("topic")
    action = conv.get("next_action")
    if hi:
        options = [
            f"Bilkul — {topic + ' ke liye ' if topic else ''}agla step main 5 minute mein ready kar sakti hoon. Shuru karun?",
            "Samajh gayi. Main draft ready karke yahin bhej deti hoon — bas HAAN reply kar dijiye.",
            "Theek hai. Ek HAAN reply kijiye, baaki kaam main sambhal lungi.",
        ]
    elif intent == "question":
        options = [
            f"Good question — I'll pull the exact details{' on ' + topic if topic else ''} and share them here. Want me to prepare it alongside?",
            "Fair ask. I'll get you the specifics in my next message — shall I also set up the draft so it's ready?",
            "Let me confirm that and come back with a clear answer. Reply YES if you'd like the draft ready too.",
        ]
    else:
        options = [
            f"Great — next step: {action}. Takes about 5 minutes. Want me to start?" if action else
            f"Great — I can set up the next step{' on ' + topic if topic else ''} in about 5 minutes. Want me to start?",
            "Happy to take it from here — reply YES and I'll get started.",
            "I can have it ready today. One YES from you and I'll begin.",
        ]
    body = _pick(options, conv, seed)
    result = _send(conv, body, "binary_yes_no",
                   f"Merchant {intent}; moving to a single low-friction next step.")
    result["_llm_upgrade"] = intent       # main.py will try a grounded LLM answer, keeping this as fallback
    return result
