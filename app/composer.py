"""
Composer: turns a fact sheet into the final WhatsApp message.

    compose(category, merchant, trigger, customer) -> dict with
        body, cta, send_as, suppression_key, rationale,
        template_name, template_params, topic, next_action, source

Flow:
    1. build the fact sheet (app/facts.py)
    2. ask the LLM, with a prompt tuned to the trigger kind
    3. validate it (app/validator.py); if it breaks a rule, send the problems back for ONE repair
    4. if it still fails (or the LLM is down) -> deterministic fact-only template
"""
import json
import re

from app.facts import build_facts
from app.llm import complete_json
from app.validator import validate

VALID_CTAS = {"binary_yes_no", "binary_confirm_cancel", "multi_choice_slot", "open_ended", "none"}

# How to play each trigger kind. Short, concrete guidance; the facts do the rest.
KIND_PLAYBOOK = {
    "research_digest": "Lead with the finding and its source citation. Connect it to THIS merchant's patient/customer cohort. Offer to pull the summary and draft shareable content. Curiosity + reciprocity.",
    "regulation_change": "Lead with the rule change, the deadline and what exactly changes. Say who is affected. Offer a concrete compliance checklist. Calm urgency, no fear-mongering.",
    "cde_opportunity": "Name the event, date/time, credits and fee exactly as given. One line on why it's relevant to this practice. Offer to share registration details.",
    "supply_alert": "Urgent and precise: molecule, batch numbers, manufacturer exactly as given. Say what it means for their customers. Offer to draft the customer note + pickup workflow.",
    "perf_dip": "Name the metric and the exact drop. Loss-aversion framing. Pair it with ONE concrete fix grounded in their data (stale posts, no active offer, below-peer CTR). Offer to do the fix.",
    "seasonal_perf_dip": "Pre-empt anxiety: say the dip is expected for this season (only if the facts say so). Reframe toward retention of existing members/customers. Offer one retention action.",
    "perf_spike": "Celebrate the exact number briefly, name the likely driver ONLY if given. Offer to double down while momentum lasts.",
    "competitor_opened": "Factual, not fear-mongering: name, distance and their offer exactly as given. Compare with the merchant's own offer if relevant. Offer a differentiating move. Never insult the competitor.",
    "festival_upcoming": "Tie the festival date to the merchant's offer. If the event is far away, frame it as early planning (e.g. booking calendar, packages) rather than a promo push.",
    "review_theme_emerged": "Quote the theme and count exactly. Treat it as fixable. Offer a reply template + one fix. Empathetic, practical.",
    "milestone_reached": "Name the milestone and how close they are. Social proof. Offer a small push to cross it (e.g. review nudge to happy customers).",
    "renewal_due": "Lead with days remaining and the renewal amount exactly as given. Loss aversion using THEIR OWN numbers (e.g. the views/calls/leads their listing brought in the last 30 days) — that's what's at stake if it lapses. Don't claim specific features stop unless the facts say so. End with a one-tap 'Reply YES to renew'.",
    "dormant_with_vera": "Don't guilt them. Reciprocity: lead with ONE useful finding from their data. Very low-effort ask.",
    "winback_eligible": "Acknowledge the gap without blame. Anchor on what they are losing (exact numbers given). Offer to restart with one step.",
    "curious_ask_due": "ASK THE MERCHANT one specific, easy question about this week. Make it concrete: name 1-2 of their ACTUAL active offers with prices as a smart guess (e.g. 'is it the Haircut @ ₹99 or the Hair Spa @ ₹499?') and anchor on ONE real number from their data (e.g. calls up X% this week). Offer to turn the answer into a Google post + ready WhatsApp reply. No pitch, one-word answer possible.",
    "gbp_unverified": "Explain the concrete benefit using the given uplift number. Say the verification path. Offer to walk them through it now.",
    "ipl_match_today": "Use the match, venue and the exact start time. You MUST check the merchant's offers against the match day: if their offer only runs on certain days and the match is outside them, say so explicitly in the message (e.g. 'aapka BOGO sirf Tue-Thu hai') and propose a match-night delivery special instead — that judgment is the point of the message. If reviews complain about late delivery, factor that in (e.g. promise realistic delivery times). Add operator judgment WITHOUT inventing statistics: if 'is weeknight: no' (weekend match), many fans watch at home, so push DELIVERY (using an existing offer only if it is valid that day) rather than a dine-in match-night promo; on a weeknight, a dine-in screening push can work. Offer a ready banner/story.",
    "active_planning_intent": "The merchant already said yes to exploring this. Do NOT ask qualifying questions. Deliver a concrete starter draft built ONLY from their real offers/prices, and ask for one edit or a go-ahead.",
    "category_seasonal": "Name the seasonal demand shifts exactly as given. Recommend one shelf/offer action. Offer to draft it.",
    "recall_due": "Customer-facing, from the clinic. Name the recall and time since last visit. Offer the exact available slots given. Price only from the merchant's active offers. Slot choice CTA is fine.",
    "appointment_tomorrow": "Customer-facing. Confirm the appointment details given. Easy confirm/reschedule.",
    "chronic_refill_due": "Customer-facing, respectful (may be a senior or family member). Molecules and run-out date exactly as given; mention delivery/discount ONLY if in active offers. Single CONFIRM.",
    "customer_lapsed_soft": "Customer-facing, warm, no guilt. Reference their past visits/services if given. One easy reason to come back from the merchant's ACTIVE offers only.",
    "customer_lapsed_hard": "Customer-facing, warm, zero shame ('happens to everyone'). Link to their previous goal if given. No-commitment offer from ACTIVE offers only. Single YES.",
    "trial_followup": "Customer-facing. Reference the trial date, offer the next session options exactly as given. Easy booking CTA.",
    "wedding_package_followup": "Customer-facing. Use the wedding date/days to go and the next-step window exactly as given. Warm, excited but not pushy. Price ONLY if it's in the merchant's active offers.",
}

SYSTEM_PROMPT = """You write WhatsApp messages for Vera, magicpin's AI assistant for Indian local merchants.
You are scored on: decision quality, specificity, category fit, merchant fit, engagement compulsion.

HARD RULES (breaking any of these is a failure):
1. Use ONLY facts from the FACT SHEET. Never invent numbers, prices, dates, names, sources, slots, statistics or events.
   Every number you write must appear in the fact sheet.
2. Build the message around the ANCHOR (the one signal that matters now). Use at most 2-3 supporting facts. Don't dump data.
3. Say WHY NOW in the first sentence.
4. Exactly ONE call-to-action, in the LAST sentence, easy to answer (ideally yes/no).
5. No URLs. No hashtags. No "I hope you are doing well" preambles. Don't introduce yourself at length.
6. Never use any TABOO word listed. Match the category VOICE.
7. "Category offer ideas" are suggestions — never say the merchant already runs them.
8. Don't expose internal field names (like ctr_below_peer_median, delta_7d, placeholder). Write like a human.
9. Keep it tight: 2-4 short sentences, under 450 characters unless a draft/plan is being delivered.
10. Don't invent NON-number details either: schedules or timings ("evening sessions", "morning delivery"),
    stock ("fresh batch"), class formats, events, seasons, awards, reviews. If it isn't in the fact sheet, don't say it.
11. Category-wide trends and demand shifts are MARKET-WIDE ("across pharmacies in your area"), never "at your store".
12. Vera is female. In Hindi/Hinglish use feminine verb forms: "karti hoon", "kar deti hoon", "bhej deti hoon".
13. If writing Hinglish, keep the WHOLE message Hinglish, including the final question.
14. ENGAGEMENT: state the stake with one real number (what they gain or lose), and make the final ask answerable
    with ONE word (e.g. "Reply YES", "Haan bolo?"). Offer to do the work for them.
16. Respect offer limits: if an offer is restricted to certain days/times (e.g. "(Tue-Thu)"), never suggest using it outside them.
17. Mention the exact time/date of an event when the facts give it (e.g. "7:30 PM tonight").
18. Make every number verifiable: say where it comes from and its time window
    ("180 delivery orders in the last 30 days", "per DCI circular", "on your Google profile"), never a bare number.
15. Write drops as words, not signs: "calls 21% gir gaye" / "calls are down 21%" (never "-21% gir gaye").

OUTPUT: only a JSON object, no other text:
{"body": "...", "cta": "binary_yes_no|binary_confirm_cancel|multi_choice_slot|open_ended|none",
 "topic": "3-6 word description of what this message is about",
 "rationale": "1-2 sentences: which signal you chose and why, and which lever you used"}"""

LANGUAGE_RULES = {
    "hi-en mix": "Write natural Hinglish in Roman script: mostly English with a few everyday Hindi words/phrases (e.g. 'aapke', 'bas', 'chalega?'). Never Devanagari.",
    "hi": "Write in simple Hindi using Roman script (Hinglish spelling). Never Devanagari.",
}


def _language_rule(lang: str) -> str:
    lang = (lang or "en").lower()
    if lang in LANGUAGE_RULES:
        return LANGUAGE_RULES[lang]
    if "mix" in lang:
        return "Write in clear, warm English (the customer mixes languages; keep it simple and friendly)."
    return "Write in clear, simple English."


def build_prompt(f: dict) -> str:
    voice = f.get("voice") or {}
    facing = ("CUSTOMER-FACING: sent from the merchant's own WhatsApp number to their customer. "
              "Speak as the business (e.g. 'Dr. Meera's clinic here'). Never mention Vera, magicpin, metrics or peers."
              if f["send_as"] == "merchant_on_behalf" else
              "MERCHANT-FACING: Vera talking to the business owner, peer-to-peer, like a sharp growth advisor.")
    playbook = KIND_PLAYBOOK.get(f["kind"], "Lead with the anchor, make it specific to this merchant, one easy next step.")
    if f.get("is_placeholder"):
        playbook = ("This trigger came with NO event details. Do NOT mention the trigger's event at all "
                    f"(no {f['kind'].replace('_', ' ')}, no festival/milestone/season you have no data for). "
                    "Build the whole message on the ANCHOR fact from this merchant's own data, and offer the next step below.")
    lines = [
        facing,
        f"TRIGGER KIND: {f['kind']} (urgency {f.get('urgency')}/5)",
        f"PLAYBOOK: {playbook}",
        f"ADDRESS THEM AS: {f['salutation']}",
        f"LANGUAGE: {_language_rule(f['language'])}",
        f"CATEGORY: {f.get('category')} | VOICE tone: {voice.get('tone')} | register: {voice.get('register')}",
        f"VOCAB YOU MAY USE: {', '.join((voice.get('vocab_allowed') or [])[:12])}",
        f"TABOO (never use): {', '.join(voice.get('vocab_taboo') or [])}",
        f"IF THEY SAY YES, WE WILL: {f['next_action']}",
        "",
        f"ANCHOR: {f['anchor']}",
        "",
        "FACT SHEET:",
        *[f"- {line}" for line in f["facts"]],
    ]
    if f.get("warnings"):
        lines += ["", "WARNINGS:", *[f"- {w}" for w in f["warnings"]]]
    return "\n".join(lines)


# ------------------------------------------------------------------ fallback

HUMANIZE = [
    (r"^Merchant has NO active offers$", "you don't have an active offer on your profile right now"),
    (r"^Google Business Profile is NOT verified$", "your Google profile isn't verified yet"),
    (r"^Review theme '([^']+)' \(neg\), (\d+) mentions in 30 days.*$", r"\2 reviews in the last 30 days mention '\1'"),
    (r"^Profile CTR is ([\d.]+%) vs peer average ([\d.]+%) \(below peers\)$",
     r"only \1 of people who see your profile take action, vs \2 for similar businesses"),
    (r"^(\w+) changed -(\d+%) in the last 7 days$", lambda m: f"{m.group(1).lower()} are down {m.group(2)} in the last 7 days"),
    (r"^(\w+) changed \+(\d+%) in the last 7 days$", lambda m: f"{m.group(1).lower()} are up {m.group(2)} in the last 7 days"),
    (r"^Subscription expired (\d+) days ago$", r"your subscription lapsed \1 days ago"),
]


def _plain_anchor(anchor: str) -> str:
    for pattern, repl in HUMANIZE:
        if re.match(pattern, anchor or ""):
            return re.sub(pattern, repl, anchor)
    text = re.sub(r"^(WHY NOW \([^)]*\):|DIGEST ITEM:)\s*", "", anchor or "").strip()
    text = re.sub(r"\(source: [^)]*\)", "", text)
    return text.replace("; ", ", ").strip().rstrip(".,")


def _pct(v) -> str:
    try:
        return f"{abs(round(float(v) * 100))}%"
    except (TypeError, ValueError):
        return str(v)


def fallback_message(f: dict) -> dict:
    """
    Deterministic, fact-only message used when the LLM is unavailable or its
    output can't be repaired. Uses only values that exist in the contexts.
    """
    name = f["salutation"]
    p = f.get("trigger_payload") or {}
    item = f.get("digest_item") or {}
    kind = f["kind"]
    ask = f"Want me to {f['next_action']}? Reply YES and I'll start."
    body = None

    offer = (f.get("active_offers") or [None])[0]
    business = f.get("business_name") or "the team"

    if item.get("title"):
        body = f"{name}, worth a look: {item['title']} ({item.get('source', 'this week')}). "
        if item.get("actionable"):
            body += item["actionable"].rstrip(".") + ". "
        body += ask
    elif kind in ("perf_dip", "perf_spike") and p.get("metric") and p.get("delta_pct") is not None:
        direction = "dropped" if p["delta_pct"] < 0 else "jumped"
        body = f"{name}, your {p['metric']} {direction} {_pct(p['delta_pct'])} in the last {p.get('window', '7d')}. {ask}"
    elif kind == "competitor_opened" and p.get("competitor_name"):
        body = (f"{name}, heads-up: {p['competitor_name']} opened {p.get('distance_km', 'nearby')} km away"
                f"{' with ' + p['their_offer'] if p.get('their_offer') else ''}. {ask}")
    elif kind == "renewal_due" and p.get("days_remaining"):
        amount = f" (renewal ₹{p['renewal_amount']:,})" if isinstance(p.get("renewal_amount"), (int, float)) else ""
        stake = next((l.replace("Last 30 days: ", "") for l in f["facts"] if l.startswith("Last 30 days: ")), None)
        body = (f"{name}, your {p.get('plan', '')} plan has {p['days_remaining']} days left{amount}."
                f"{' In the last 30 days your listing brought ' + stake + ' — keep that running.' if stake else ''}"
                f" Reply YES and I'll set up the renewal.").replace("  ", " ")
    elif kind == "milestone_reached" and p.get("value_now"):
        body = (f"{name}, you're at {p['value_now']} {str(p.get('metric', '')).replace('_', ' ')}"
                f"{' — just short of ' + str(p['milestone_value']) if p.get('milestone_value') else ''}. {ask}")
    elif kind == "curious_ask_due":
        body = (f"{name}, quick question — which service are customers asking for most this week? "
                f"I'll turn your answer into a Google post and a ready WhatsApp reply.")
    elif kind == "active_planning_intent" and p.get("intent_topic"):
        body = (f"{name}, picking up on your {str(p['intent_topic']).replace('_', ' ')} idea — "
                f"I can build a starter draft{' around your ' + offer if offer else ''} for you to edit. "
                f"Reply YES and I'll share it here.")
    elif kind == "ipl_match_today" and p.get("match"):
        weekend = p.get("is_weeknight") is False
        if offer and re.search(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)", offer):
            offer = None     # day-restricted offer: don't suggest it on a match day it may not cover
        body = (f"{name}, {p['match']} tonight{' at ' + p['venue'] if p.get('venue') else ''}. "
                f"{'Weekend match, so most fans will watch at home — a delivery push makes sense' if weekend else 'Good night for a match-night push'}"
                f"{' with your ' + offer if offer else ''}. {ask}")
    elif kind == "gbp_unverified":
        uplift = f" — verified profiles see about {_pct(p['estimated_uplift_pct'])} more visibility" if p.get("estimated_uplift_pct") else ""
        path = f" via {str(p['verification_path']).replace('_', ' ')}" if p.get("verification_path") else ""
        body = f"{name}, your Google profile isn't verified yet{uplift}. It can be done{path}. {ask}"
    elif kind == "recall_due" and p.get("available_slots"):
        slots = " or ".join(s.get("label", "") for s in p["available_slots"][:2] if isinstance(s, dict))
        body = (f"Hi {name}, {business} here. Your {str(p.get('service_due', 'check-up')).replace('_', ' ')} is due"
                f"{' — ' + offer if offer else ''}. We have {slots}. Reply 1 or 2 to book, or tell us a time that suits you.")
        return {"body": body, "cta": "multi_choice_slot", "topic": "recall booking",
                "rationale": "Deterministic fact-only template using the real due service, slots and active offer."}
    elif kind == "chronic_refill_due" and p.get("molecule_list"):
        meds = ", ".join(p["molecule_list"])
        runs_out = str(p.get("stock_runs_out_iso", ""))[:10]
        body = (f"Namaste {name}, {business} here. Your medicines ({meds}) run out on {runs_out}. "
                f"Same pack ready{' — ' + offer if offer else ''}. Reply CONFIRM and we'll prepare the refill.")
        return {"body": body, "cta": "binary_confirm_cancel", "topic": "chronic refill",
                "rationale": "Deterministic fact-only template using the real molecules, run-out date and active offer."}
    elif kind in ("customer_lapsed_soft", "customer_lapsed_hard") and f["send_as"] == "merchant_on_behalf":
        days = p.get("days_since_last_visit")
        body = (f"Hi {name}, {business} here. {'It has been ' + str(days) + ' days — no' if days else 'No'}"
                f" pressure, we'd simply love to see you again"
                f"{'. ' + offer + ' is on right now' if offer else ''}. Reply YES and we'll hold a spot for you.")
    elif kind == "festival_upcoming" and p.get("festival"):
        days = p.get("days_until")
        early = isinstance(days, (int, float)) and days > 60
        body = (f"{name}, {p['festival']} is {str(days) + ' days' if days else 'coming up'} away"
                f"{' — early, but the right time to plan festive packages' if early else ''}. {ask}")
    elif kind == "category_seasonal" and p.get("trends"):
        shifts = ", ".join(str(t).replace("_", " ").replace(" demand ", " ") + "%" for t in p["trends"][:3])
        body = f"{name}, seasonal demand across pharmacies is shifting ({shifts}). {ask}"
    elif f.get("merchant_anchor") and f["send_as"] == "vera" and (f.get("is_placeholder") or kind == "dormant_with_vera"):
        body = f"{name}, one thing I noticed on your profile — {_plain_anchor(f['merchant_anchor'])}. {ask}"

    if body is None and f["send_as"] == "merchant_on_behalf":
        body = f"Hi {name}, {business} here. We'd love to see you again soon — reply YES and we'll share the next available slots."
    if body is None:
        body = f"{name}, quick one — {_plain_anchor(f['anchor'])}. {ask}"

    cta = "open_ended" if kind == "curious_ask_due" else "binary_yes_no"
    return {"body": body, "cta": cta, "topic": kind.replace("_", " "),
            "rationale": f"Deterministic fact-only template; anchored on: {_plain_anchor(f['anchor'])[:90]}"}


# ------------------------------------------------------------------ main entry

_DROP_WORDS = r"(gir|kam|ghat|down|drop|dropped|fell|fall|decline|declined|dip)"


def _tidy(body: str) -> str:
    """Fix double negatives such as 'calls -21% gir gaye' -> 'calls 21% gir gaye'."""
    body = re.sub(rf"-(\d+(?:\.\d+)?%)(\s+\w*\s*{_DROP_WORDS})", r"\1\2", body, flags=re.I)
    body = re.sub(rf"({_DROP_WORDS}\w*\s+(?:by\s+)?)-(\d)", r"\1\3", body, flags=re.I)
    return body


def _template_params(salutation: str, body: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", body.strip())
    return [salutation, " ".join(sentences[:-1]) or body, sentences[-1] if len(sentences) > 1 else ""]


def _repair_prompt(base_prompt: str, previous: dict, problems: list[str]) -> str:
    return (base_prompt + "\n\nYOUR PREVIOUS DRAFT:\n" + json.dumps(previous, ensure_ascii=False)
            + "\n\nIT BROKE THESE RULES — fix every one and return the corrected JSON:\n"
            + "\n".join(f"- {p}" for p in problems))


async def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None,
                  facts: dict | None = None, avoid_bodies: list[str] | None = None) -> dict:
    f = facts or build_facts(category, merchant, trigger, customer)
    prompt = build_prompt(f)
    history = []   # (source, problems) for each attempt, for the rationale/debugging

    data, source = await complete_json(SYSTEM_PROMPT, prompt)
    problems = validate(data.get("body", ""), data.get("cta", ""), f, avoid_bodies) if isinstance(data, dict) else ["no output"]
    history.append((source, problems))

    if problems and isinstance(data, dict):
        # One repair attempt: send the exact problems back to the LLM
        fixed, source2 = await complete_json(SYSTEM_PROMPT, _repair_prompt(prompt, data, problems))
        if isinstance(fixed, dict):
            problems2 = validate(fixed.get("body", ""), fixed.get("cta", ""), f, avoid_bodies)
            history.append((source2, problems2))
            if not problems2:
                data, source, problems = fixed, f"{source2} (repaired)", []

    if problems:
        data = fallback_message(f)
        source = f"template (after: {history[-1][0][:80]})"

    body = _tidy(str(data["body"]).strip())
    cta = data.get("cta") if data.get("cta") in VALID_CTAS else "binary_yes_no"
    prefix = "merchant" if f["send_as"] == "merchant_on_behalf" else "vera"

    return {
        "body": body,
        "cta": cta,
        "send_as": f["send_as"],
        "suppression_key": f["suppression_key"],
        "rationale": str(data.get("rationale") or f"Anchored on: {f['anchor'][:100]}").strip(),
        "template_name": f"{prefix}_{f['kind']}_v1",
        "template_params": _template_params(f["salutation"], body),
        "topic": str(data.get("topic") or f["kind"].replace("_", " ")),
        "next_action": f["next_action"],
        "source": source,
        "validation": [{"source": s, "problems": p} for s, p in history],
        "facts": f,
    }
