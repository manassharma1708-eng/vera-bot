"""
Tests the reply brain against the judge's replay scenarios + extra edge cases.

Run from the vera-bot folder (server running in another terminal):
    python scripts/test_replies.py
"""
import httpx

BOT_URL = "http://localhost:8080"
MID = "m_001_drmeera_dentist_delhi"
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]
ACTIONING = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]

results = []


def reply(client, conv, msg, turn):
    r = client.post(f"{BOT_URL}/v1/reply", json={
        "conversation_id": conv, "merchant_id": MID, "customer_id": None,
        "from_role": "merchant", "message": msg,
        "received_at": "2026-04-26T10:42:00Z", "turn_number": turn,
    })
    return r.json()


def check(name, ok, data):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"        -> {data.get('action')}: {data.get('body', data.get('rationale', ''))[:110]}")


with httpx.Client(timeout=10) as c:
    # 1. Auto-reply hell (same canned text, new conversation id every turn, like judge_simulator.py)
    auto = "Thank you for contacting us! Our team will respond shortly."
    actions = [reply(c, f"conv_auto_{i}", auto, i + 1) for i in range(1, 5)]
    seq = [a["action"] for a in actions]
    check(f"auto-reply x4 -> {seq} (must reach 'end')", "end" in seq, actions[-1])

    # 2. Hindi auto-reply (real production example from the brief)
    d = reply(c, "conv_hindi_auto", "Aapki jaankari ke liye bahut-bahut shukriya. Main aapki yeh sabhi baatein team tak pahuncha deti hoon.", 2)
    check("Hindi auto-reply detected", d["action"] in ("send", "wait") and "auto" in d["rationale"].lower(), d)

    # 3. Intent transition
    d = reply(c, "conv_intent_1", "Ok lets do it. Whats next?", 2)
    body = d.get("body", "").lower()
    ok = any(w in body for w in ACTIONING) and not any(w in body for w in QUALIFYING)
    check("'Ok lets do it' -> action mode, no qualifying question", ok, d)

    # 4. Hindi commitment
    d = reply(c, "conv_intent_hi", "Mujhe magicpin judna hai", 2)
    check("'Mujhe magicpin judna hai' -> action mode", d.get("cta") == "binary_confirm_cancel", d)

    # 5. Hostile with explicit stop
    d = reply(c, "conv_hostile", "Stop messaging me. This is useless spam.", 2)
    check("hostile + stop -> end", d["action"] == "end", d)

    # 6. Hostile without stop -> apology
    d = reply(c, "conv_hostile_2", "This is useless, why are you bothering me", 2)
    ok = d["action"] == "end" or any(w in d.get("body", "").lower() for w in ["sorry", "apolog", "won't"])
    check("hostile (no stop) -> apology or end", ok, d)

    # 7. Off-topic curveball
    d = reply(c, "conv_gst", "Btw can you also help me with my GST filing this month?", 2)
    check("GST question -> polite decline + redirect", d["action"] == "send" and "CA" in d.get("body", ""), d)

    # 8. Busy
    d = reply(c, "conv_busy", "I'm busy right now, talk later", 2)
    check("busy -> wait", d["action"] == "wait", d)

    # 9. Soft decline
    d = reply(c, "conv_no", "No thanks", 2)
    check("'No thanks' -> polite close", d.get("cta") == "none", d)

    # 10. Question with a 'yes' in it should be treated as a question
    d = reply(c, "conv_q", "Yes but how much does it cost?", 2)
    check("'Yes but how much...?' -> not blindly committed", d.get("cta") != "binary_confirm_cancel", d)

    # 11. Hinglish reply gets a Hinglish answer
    d = reply(c, "conv_hinglish", "haan theek hai, kar do", 2)
    check("Hinglish commit -> Hinglish reply", "kar" in d.get("body", "").lower() or "ho gaya" in d.get("body", "").lower(), d)

    # 12. Anti-repetition: same engaged message 3 times in one conversation
    bodies = [reply(c, "conv_repeat", "sounds interesting", t).get("body") for t in (2, 3, 4)]
    check("no verbatim repeats in one conversation", len(set(bodies)) == len(bodies), {"action": "send", "body": " | ".join(b[:30] for b in bodies)})

print(f"\n{sum(results)}/{len(results)} passed")
