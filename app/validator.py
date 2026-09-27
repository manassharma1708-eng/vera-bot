"""
Validator: checks a composed message against the challenge's hard rules.

    validate(body, cta, facts, avoid_bodies) -> list of problems (empty = message is OK)

Each problem is written as an instruction the LLM can act on, because the
composer sends the list back to the LLM for one repair attempt.
"""
import re

from app.facts import numbers_in

# Numbers that are always fine: option numbers ("Reply 1 / 2") and effort claims ("2-min", "3 steps").
ALWAYS_OK = {"0", "1", "2"}
EFFORT_RE = re.compile(r"\b(\d{1,2})\s*-?\s*(min|mins|minute|minutes|sec|secs|seconds|step|steps|point|points|line|lines)\b", re.I)

GLOBAL_TABOO = ["guarantee", "100%", "risk-free", "risk free", "miracle", "best in city", "best in town",
                "no.1", "#1", "cure", "lowest price", "act now", "limited time only"]
CLINICAL_RISKY = ["safe", "harmless", "side-effect free", "no side effects", "painless"]
JARGON_RE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b|placeholder|why now|fact sheet|\banchor\b|delta_|signal:|\bjson\b", re.I)
URL_RE = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(com|in|co|org|net|io|ly|app)\b(/|\s|$)", re.I)
CITE_KINDS = {"research_digest", "regulation_change", "supply_alert", "cde_opportunity"}


def _norm(n: str) -> str:
    """'2,410' -> '2410', '3.0' -> '3', '05' -> '5'."""
    n = n.replace(",", "")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n.lstrip("0") or "0"


def _taboo_list(facts: dict) -> list[str]:
    taboo = []
    for t in (facts.get("voice") or {}).get("vocab_taboo") or []:
        t = re.sub(r"\(.*?\)", "", str(t)).strip().lower()
        if t:
            taboo.append(t)
    return taboo + GLOBAL_TABOO


def validate(body: str, cta: str, facts: dict, avoid_bodies: list[str] | None = None) -> list[str]:
    problems: list[str] = []
    text = (body or "").strip()
    low = text.lower()

    if len(text) < 40:
        return ["The message is empty or too short. Write a complete 2-4 sentence message."]

    # 1. Every number must come from the fact sheet
    allowed = {_norm(n) for n in facts.get("allowed_numbers", [])} | ALWAYS_OK
    effort = {_norm(m.group(1)) for m in EFFORT_RE.finditer(text)}
    invented = sorted({_norm(n) for n in numbers_in(text)} - allowed - effort, key=len)
    if invented:
        problems.append(f"These numbers are NOT in the fact sheet, remove or replace them with real ones: {', '.join(invented)}.")

    # 1b. Prices and percentages must match a price / percentage in the facts (stops coincidental matches)
    fact_text = " ".join(facts.get("facts", [])) + " " + str(facts.get("next_action", ""))
    fact_rupees = {_norm(n) for n in re.findall(r"₹\s?(\d[\d,]*(?:\.\d+)?)", fact_text)}
    fact_rupees |= {_norm(n) for n in re.findall(r"(?:amount|price|fee|cost|mrp|rs\.?)\s*:?\s*(\d[\d,]*)", fact_text, re.I)}
    fact_pcts = {_norm(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s?%", fact_text)}
    fact_pcts |= {_norm(n) for n in re.findall(r"[+-](\d+)(?=\b)", fact_text)}      # e.g. "ORS demand +40"
    bad_rupees = sorted({_norm(n) for n in re.findall(r"₹\s?(\d[\d,]*(?:\.\d+)?)", text)} - fact_rupees)
    bad_pcts = sorted({_norm(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s?%", text)} - fact_pcts)
    if bad_rupees:
        problems.append(f"These prices are not real prices from the facts: ₹{', ₹'.join(bad_rupees)}. Use only listed prices.")
    if bad_pcts:
        problems.append(f"These percentages are not in the facts: {'%, '.join(bad_pcts)}%. Use only listed percentages.")

    # 2. Taboo words and risky claims
    hits = [t for t in _taboo_list(facts) if re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", low)]
    if facts.get("category") in ("dentists", "pharmacies"):
        hits += [w for w in CLINICAL_RISKY if re.search(rf"\b{re.escape(w)}\b", low)]
    if hits:
        problems.append(f"Remove these banned/overclaiming words: {', '.join(sorted(set(hits)))}. Use neutral factual wording.")

    # 3. No URLs
    if URL_RE.search(text):
        problems.append("Remove all links/URLs. WhatsApp templates with links get rejected.")

    # 4. Internal jargon
    jargon = sorted({m.group(0) for m in JARGON_RE.finditer(text)})
    if jargon:
        problems.append(f"Remove internal/system wording a merchant wouldn't say: {', '.join(jargon)}.")

    # 5. Customer-facing messages must not leak Vera/magicpin/metrics
    if facts.get("send_as") == "merchant_on_behalf":
        leaks = [w for w in ("vera", "magicpin", "ctr", "peer", "views", "dashboard", "listing") if re.search(rf"\b{w}\b", low)]
        if leaks:
            problems.append(f"This goes to a CUSTOMER from the business. Don't mention: {', '.join(leaks)}.")

    # 6. Exactly one call-to-action, at the end
    if cta != "none":
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        last = sentences[-1].lower() if sentences else ""
        ask_words = ("?", "reply", "confirm", "yes", "haan", "chalega", "shall i", "want me", "should i", "let me know")
        if not any(w in last for w in ask_words):
            problems.append("End with ONE clear, easy question or 'Reply YES' ask as the final sentence.")
        if text.count("?") > 2 or low.count("reply") > 2:
            problems.append("There are several asks. Keep exactly ONE call-to-action.")

    # 7. Research / compliance claims must cite the source
    item = facts.get("digest_item") or {}
    if facts.get("kind") in CITE_KINDS and item.get("source"):
        tokens = [w for w in re.findall(r"[A-Za-z]{3,}", item["source"]) if w[0].isupper()]
        caps = [w for w in re.findall(r"[A-Za-z]+", item["source"]) if w[0].isupper()]
        if len(caps) >= 2:
            tokens.append("".join(w[0] for w in caps if w.lower() not in ("of", "and")))   # e.g. "DCI"
        if tokens and not any(re.search(rf"\b{re.escape(t.lower())}\b", low) for t in tokens):
            problems.append(f"Cite the source by name (from: '{item['source']}').")

    # 8. Address the right person
    name = str(facts.get("salutation") or "")
    first = name.replace("Dr. ", "").split(" ")[0].lower()
    if first and first != "there" and first not in low:
        problems.append(f"Address them by name: {name}.")

    # 9. Script and length
    if re.search(r"[\u0900-\u097F]", text):
        problems.append("Use Roman script only (no Devanagari).")
    limit = 900 if facts.get("kind") == "active_planning_intent" else 650
    if len(text) > limit:
        problems.append(f"Too long ({len(text)} characters). Cut to under {limit - 150} characters, keep the anchor and the ask.")

    # 10. Never repeat an earlier message
    if avoid_bodies and text in avoid_bodies:
        problems.append("This exact message was already sent. Write a different message.")

    return problems
