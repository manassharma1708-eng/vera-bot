"""
Fact sheet builder.

Turns the raw contexts (category, merchant, trigger, customer) into a small,
clean, VERIFIED set of facts for one message. The LLM (Step 5) only ever sees
this fact sheet, and the validator (Step 6) rejects any number in the output
that isn't in `allowed_numbers`. That's how the bot avoids hallucinating.

It also decides:
  - anchor:         the ONE signal that should drive this message (decision quality)
  - send_blockers:  hard reasons NOT to send (no consent, trigger doesn't fit category, ...)
  - warnings:       soft issues the composer should know (thin trigger payload, ...)
"""
import re

# ------------------------------------------------------------------ small formatters

def pct(x, signed=False) -> str:
    """0.021 -> '2.1%', -0.5 -> '-50%'"""
    v = round(float(x) * 100, 1)
    v = int(v) if v == int(v) else v
    return f"{'+' if signed and v > 0 else ''}{v}%"


def num(x) -> str:
    """2410 -> '2,410'"""
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return str(x)


def human(s: str) -> str:
    return str(s).replace("_", " ").strip()


NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> set[str]:
    """All numbers in a string, normalized (commas removed)."""
    return {n.replace(",", "") for n in NUMBER_RE.findall(str(text))}


# ------------------------------------------------------------------ category rules

# Which categories a trigger kind makes sense for (None = any category).
KIND_FITS = {
    "recall_due": {"dentists", "salons"},
    "chronic_refill_due": {"pharmacies"},
    "supply_alert": {"pharmacies"},
    "cde_opportunity": {"dentists"},
    "ipl_match_today": {"restaurants"},
    "wedding_package_followup": {"salons"},
    "trial_followup": {"gyms", "salons", "dentists"},
}

# Consent scopes that allow each customer-facing trigger kind.
CONSENT_FOR = {
    "recall_due": {"recall_reminders", "appointment_reminders"},
    "appointment_tomorrow": {"appointment_reminders"},
    "chronic_refill_due": {"refill_reminders"},
    "customer_lapsed_soft": {"winback_offers", "promotional_offers", "recall_reminders", "renewal_reminders"},
    "customer_lapsed_hard": {"winback_offers", "promotional_offers", "recall_reminders", "renewal_reminders"},
    "trial_followup": {"kids_program_updates", "program_updates", "appointment_reminders", "promotional_offers"},
    "wedding_package_followup": {"bridal_package_followup", "appointment_reminders"},
}

# Where a trigger may point at a category digest item.
DIGEST_KEYS = ("top_item_id", "digest_item_id", "alert_id")

# What we'd offer to do next, per trigger kind (used by the reply brain on "yes").
NEXT_ACTION = {
    "research_digest": "pull the summary and draft a short patient-education WhatsApp you can share",
    "regulation_change": "prepare a 3-point compliance checklist for your setup",
    "cde_opportunity": "share the registration details and add it to your calendar",
    "supply_alert": "draft the customer note and a replacement-pickup plan",
    "perf_dip": "draft a Google post and set up one strong offer to win back calls",
    "seasonal_perf_dip": "draft a member-retention message for this quieter period",
    "perf_spike": "draft a follow-up post while the momentum lasts",
    "competitor_opened": "draft a Google post that highlights what makes you different",
    "festival_upcoming": "draft a festival post and offer for your profile",
    "review_theme_emerged": "draft a reply template for those reviews and one fix-it post",
    "milestone_reached": "draft a thank-you post that nudges happy customers to review",
    "renewal_due": "set up the renewal so there's no gap in your listing",
    "dormant_with_vera": "run a quick profile check and share the top fix",
    "winback_eligible": "reactivate your listing and restart your best offer",
    "curious_ask_due": "turn your answer into a Google post and a ready WhatsApp reply",
    "gbp_unverified": "walk you through verification step by step",
    "ipl_match_today": "draft tonight's delivery banner and an Insta story",
    "active_planning_intent": "draft the full plan for you to edit",
    "category_seasonal": "draft a shelf and offer plan for the season",
}

CUSTOMER_KINDS = set(CONSENT_FOR)


# ------------------------------------------------------------------ builder

def build_facts(category: dict | None, merchant: dict | None,
                trigger: dict | None, customer: dict | None = None) -> dict:
    category = category or {}
    merchant = merchant or {}
    trigger = trigger or {}
    ident = merchant.get("identity") or {}
    perf = merchant.get("performance") or {}
    peer = category.get("peer_stats") or {}
    payload = trigger.get("payload") or {}
    kind = trigger.get("kind", "unknown")
    cat_slug = merchant.get("category_slug") or category.get("slug")

    lines: list[str] = []          # every fact the LLM is allowed to use
    blockers: list[str] = []
    warnings: list[str] = []
    candidates: list[tuple[int, str]] = []   # (strength, fact) for anchor selection

    def add(line: str, strength: int = 0):
        lines.append(line)
        if strength:
            candidates.append((strength, line))

    # ---- who we're talking to
    is_customer_facing = trigger.get("scope") == "customer" or bool(customer)
    owner = ident.get("owner_first_name")
    if is_customer_facing and customer:
        cid = customer.get("identity") or {}
        salutation = cid.get("name") or "there"
        language = cid.get("language_pref") or "en"
    else:
        salutation = (f"Dr. {owner}" if cat_slug == "dentists" and owner else owner) or ident.get("name") or "there"
        language = "hi-en mix" if "hi" in (ident.get("languages") or []) else "en"

    # ---- merchant identity
    if ident.get("name"):
        add(f"Business: {ident['name']}")
    if ident.get("locality") or ident.get("city"):
        add(f"Location: {', '.join(x for x in (ident.get('locality'), ident.get('city')) if x)}")
    if ident.get("verified") is False and not is_customer_facing:
        add("Google Business Profile is NOT verified", 3)

    # ---- performance vs peers (merchant-facing only: never show a customer our internal metrics)
    if perf and not is_customer_facing:
        window = perf.get("window_days", 30)
        bits = [f"{num(perf[k])} {k}" for k in ("views", "calls", "directions", "leads") if perf.get(k) is not None]
        if bits:
            add(f"Last {window} days: " + ", ".join(bits))
        ctr, peer_ctr = perf.get("ctr"), peer.get("avg_ctr")
        if ctr is not None and peer_ctr:
            if ctr < peer_ctr * 0.9:
                add(f"Profile CTR is {pct(ctr)} vs peer average {pct(peer_ctr)} (below peers)", 4)
            elif ctr > peer_ctr * 1.1:
                add(f"Profile CTR is {pct(ctr)} vs peer average {pct(peer_ctr)} (above peers)", 2)
        for metric, value in (perf.get("delta_7d") or {}).items():
            if value is None:
                continue
            name = metric.replace("_pct", "")
            strength = 4 if abs(value) >= 0.2 else 0
            add(f"{name.capitalize()} changed {pct(value, signed=True)} in the last 7 days", strength)
        peer_views = peer.get("avg_views_30d")
        if peer_views and perf.get("views"):
            add(f"Peer average views (30d): {num(peer_views)}")

    # ---- offers (merchant's own) and catalog (category suggestions, NOT merchant's)
    active = [o["title"] for o in merchant.get("offers") or [] if o.get("status") == "active" and o.get("title")]
    expired = [o["title"] for o in merchant.get("offers") or [] if o.get("status") in ("expired", "paused") and o.get("title")]
    if active:
        add("Merchant's ACTIVE offers: " + "; ".join(active))
    elif not is_customer_facing:
        add("Merchant has NO active offers", 3)
    if expired:
        add("Merchant's past (inactive) offers: " + "; ".join(expired))
    catalog = [o["title"] for o in (category.get("offer_catalog") or [])[:4] if o.get("title")]
    if catalog and not is_customer_facing:
        add("Category offer ideas (suggestions only, merchant does NOT run these yet): " + "; ".join(catalog))

    # ---- subscription
    sub = merchant.get("subscription") or {}
    if not is_customer_facing and (sub.get("status") in ("expired", "trial") or (sub.get("days_remaining") or 999) <= 15):
        if sub.get("status") == "expired" and sub.get("days_since_expiry"):
            add(f"Subscription expired {sub['days_since_expiry']} days ago", 3)
        elif sub.get("days_remaining") is not None:
            add(f"Subscription ({sub.get('plan', 'plan')}, {sub.get('status')}): {sub['days_remaining']} days remaining", 3)

    # ---- customer aggregate, review themes, signals (merchant-facing only)
    agg = {} if is_customer_facing else (merchant.get("customer_aggregate") or {})
    for key, value in agg.items():
        if value is None:
            continue
        shown = pct(value) if key.endswith("_pct") and isinstance(value, float) and value <= 1 else num(value)
        add(f"Customers — {human(key)}: {shown}")
    for rt in ([] if is_customer_facing else merchant.get("review_themes") or []):
        s = f"Review theme '{human(rt.get('theme'))}' ({rt.get('sentiment')}), {rt.get('occurrences_30d')} mentions in 30 days"
        if rt.get("common_quote"):
            s += f", e.g. \"{rt['common_quote']}\""
        add(s, 3 if rt.get("sentiment") == "neg" else 1)
    for sig in ([] if is_customer_facing else merchant.get("signals") or []):
        m = re.match(r"([a-z_]+):(\d+)d$", str(sig))
        if m:
            add(f"Signal: {human(m.group(1))} ({m.group(2)} days)", 2)
        else:
            add(f"Signal: {human(sig)}")

    # ---- recent conversation with Vera
    history = [] if is_customer_facing else (merchant.get("conversation_history") or [])
    if history:
        last = history[-1]
        add(f"Last message in history ({last.get('from')}): \"{str(last.get('body', ''))[:160]}\"")

    # ---- the trigger itself (the "why now")
    placeholder = bool(payload.get("placeholder"))
    if placeholder:
        warnings.append("trigger_payload_is_placeholder: no event details given — anchor on merchant facts only, "
                        "and do NOT invent any event details")
    else:
        parts = []
        for key, value in payload.items():
            if key in DIGEST_KEYS or value in (None, "", [], {}):
                continue
            if isinstance(value, bool):
                value = "yes" if value else "no"
            elif isinstance(value, list):
                if value and isinstance(value[0], dict):
                    value = " / ".join(v.get("label") or str(v) for v in value)
                else:
                    value = ", ".join(human(v) for v in value)
            elif isinstance(value, float) and abs(value) <= 1 and ("pct" in key or "delta" in key):
                value = pct(value, signed=True)
            elif isinstance(value, dict):
                value = ", ".join(f"{human(k)} {v}" for k, v in value.items())
            else:
                value = human(value)
            parts.append(f"{human(key)}: {value}")
        if parts:
            add(f"WHY NOW ({human(kind)}): " + "; ".join(parts), 5)
        days = payload.get("days_until")
        if isinstance(days, (int, float)) and days > 60:
            warnings.append(f"event is {days} days away — too early for a direct push; frame it as early planning or stay low-key")

    digest_id = next((payload.get(k) for k in DIGEST_KEYS if payload.get(k)), None)
    digest_item = None
    if digest_id:
        digest_item = next((d for d in category.get("digest") or [] if d.get("id") == digest_id), None)
        if digest_item:
            head = f"DIGEST ITEM: {digest_item.get('title')} (source: {digest_item.get('source', 'not given')})"
            for key in ("trial_n", "patient_segment", "date", "credits"):
                if digest_item.get(key) not in (None, ""):
                    head += f"; {human(key)}: {human(digest_item[key])}"
            add(head, 6)
            for key in ("summary", "actionable"):
                if digest_item.get(key):
                    add(f"Digest {key}: {digest_item[key]}")
        else:
            warnings.append(f"digest item '{digest_id}' referenced by trigger was not found — do not cite it")

    # ---- customer (customer-facing only)
    consent_scope: list[str] = []
    if customer:
        cid = customer.get("identity") or {}
        rel = customer.get("relationship") or {}
        pref = customer.get("preferences") or {}
        consent_scope = (customer.get("consent") or {}).get("scope") or []
        add(f"Customer: {cid.get('name')}, state {human(customer.get('state', 'unknown'))}, "
            f"language {cid.get('language_pref', 'en')}", 3)
        for key in ("first_visit", "last_visit", "visits_total"):
            if rel.get(key):
                add(f"Customer — {human(key)}: {rel[key]}")
        if rel.get("services_received"):
            add("Customer — services received: " + ", ".join(rel["services_received"]))
        if pref.get("preferred_slots"):
            add(f"Customer — preferred slots: {human(pref['preferred_slots'])}")
        if consent_scope:
            add("Customer consented to: " + ", ".join(human(s) for s in consent_scope))

    # ---- hard blockers (tick will skip these)
    fits = KIND_FITS.get(kind)
    if fits and cat_slug and cat_slug not in fits:
        blockers.append(f"trigger kind '{kind}' does not fit category '{cat_slug}'")
    if trigger.get("scope") == "customer" or kind in CUSTOMER_KINDS:
        if not customer:
            blockers.append("customer-scoped trigger but no customer context available")
        else:
            needed = CONSENT_FOR.get(kind)
            if not consent_scope:
                blockers.append("customer has not consented to any messages")
            elif needed and not (set(consent_scope) & needed):
                blockers.append(f"customer consent {consent_scope} does not cover '{kind}'")
    if not merchant:
        blockers.append("merchant context missing")

    # ---- pick the anchor: strongest trigger fact first, else strongest merchant fact
    candidates.sort(key=lambda c: -c[0])
    anchor = candidates[0][1] if candidates else (lines[0] if lines else "")

    # ---- placeholder trigger: the "why now" is really the anchor, so the next action must match it
    next_action = NEXT_ACTION.get(kind, "prepare the next step for you")
    if placeholder:
        a = anchor.lower()
        if "not verified" in a:
            next_action = "walk you through Google verification step by step"
        elif "no active offers" in a:
            next_action = "set up one strong service-at-price offer on your profile"
        elif "review theme" in a:
            next_action = "draft a reply template for those reviews and one fix-it post"
        elif "ctr is" in a:
            next_action = "fix the top 2 things that stop profile visitors from calling"
        elif "changed -" in a:
            next_action = "draft a fresh Google post and offer to win back that traffic"
        elif "changed +" in a:
            next_action = "draft a follow-up post while the momentum lasts"
        elif "subscription" in a:
            next_action = "set up the renewal so there's no gap in your listing"
        elif is_customer_facing:
            next_action = "share the next available slots"

    # ---- every number the message may legally contain
    allowed = set()
    for line in lines + [next_action]:
        allowed |= numbers_in(line)

    return {
        "kind": kind,
        "trigger_id": trigger.get("id"),
        "urgency": trigger.get("urgency"),
        "category": cat_slug,
        "voice": category.get("voice") or {},
        "send_as": "merchant_on_behalf" if is_customer_facing else "vera",
        "salutation": salutation,
        "language": language,
        "anchor": anchor,
        "facts": lines,
        "digest_item": digest_item,
        "next_action": next_action,
        "suppression_key": trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}",
        "is_placeholder": placeholder,
        "trigger_payload": {} if placeholder else payload,
        "business_name": ident.get("name"),
        "send_blockers": blockers,
        "warnings": warnings,
        "allowed_numbers": sorted(allowed),
    }