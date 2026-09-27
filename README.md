# Vera Bot — magicpin AI Challenge

A merchant-engagement bot that decides **what** to say, **to whom**, and **whether to say anything at all**, then writes messages that are grounded in the delivered context and nothing else.

**Endpoints:** `POST /v1/context` · `POST /v1/tick` · `POST /v1/reply` · `GET /v1/healthz` · `GET /v1/metadata` (+ optional `POST /v1/teardown`)

## Approach

```
contexts ──► fact sheet ──► LLM composer ──► validator ──► send
             (verified)     (per-trigger     (10 rules)     │
                             playbook)          │ fail      │
                                                ▼           │
                                      one LLM repair ──► fact-only template
```

1. **Fact sheet (deterministic).** For each (category, merchant, trigger, customer) the bot extracts only verifiable facts, scores them, and picks **one anchor** — the signal that should drive the message (digest item > trigger event > CTR gap / big metric swing > review theme…). It also sets **hard blockers**: no consent for this message type, trigger that doesn't fit the category (e.g. `recall_due` for a gym), missing customer. Customer-facing messages never see internal metrics.
2. **Composer (LLM).** Temperature 0, one prompt per trigger kind (26 playbooks: research digests cite sources, `curious_ask` asks instead of pitching, `active_planning` delivers a draft instead of qualifying, IPL checks whether the merchant's offer is even valid on match day…). Output is JSON: body, CTA, topic, rationale.
3. **Validator (deterministic).** Every number must exist in the fact sheet (prices must match real prices, percentages real percentages), no taboo/overclaiming words, no URLs, no internal jargon, one CTA in the last sentence, source cited for research/compliance, name used, no repeats, customer messages never mention Vera/magicpin/metrics. Violations go back to the LLM **once** as explicit fix instructions; if it still fails, a fact-only template is sent.
4. **Tick engine.** Suppression-key dedup, opt-out and "wait" respected, one message per recipient per tick (highest urgency wins), no second thread while one is live (unless urgent or quiet 30+ min), max 20 actions, parallel composition inside an 11 s budget — anything not ready gets its template instead of being dropped.
5. **Reply brain (rule-first).** Opt-out → end; hostility → one apology with a STOP exit; auto-reply (English + Hindi patterns, tracked per merchant across conversations) → one nudge, then wait 24 h, then end; busy → wait; off-topic (GST, loans…) → polite decline + redirect; commitment ("let's do it", "judna hai") → straight to action, no qualifying. Open questions get a grounded LLM answer that also passes the validator. Replies match the merchant's language (Hinglish in, Hinglish out).

## Model choice

Provider-agnostic chain from `LLM_ORDER`, e.g. Gemini Flash → Groq gpt-oss-120b → gpt-oss-20b. Each model has its own quota; 429s trigger a short retry or a cooldown so a blocked model is skipped instantly. Every prompt is cached by hash (memory + disk), so identical input gives identical output.

## Results (local, official `judge_simulator.py`)

- Scored messages: **~41.9 / 50 average (84%)**; 9 of 10 between 41 and 47.
- Replay scenarios: **auto-reply, intent transition, hostile — all PASS**.
- Canonical 30 pairs: 26 composed, **4 deliberately skipped** (consent doesn't cover the message, or trigger doesn't fit the category).

## Tradeoffs

- **Restraint over volume.** Skipping incoherent or non-consented pairs loses those messages but avoids spam and consent violations.
- **Rules before the LLM.** Replies to opt-outs, auto-replies and commitments are regex/state-based: instant, free, deterministic — but a novel phrasing can be misclassified (it then falls to the safe "engaged" path).
- **Validator strictness.** Rejecting any unlisted number occasionally rejects a harmless one, costing a repair call; I preferred that to letting a fabricated statistic through.
- **In-memory state.** Fast and simple; a restart wipes context (the judge re-pushes), so it runs as a single always-on instance.
- **Time budget.** On free-tier quotas some messages in a large tick fall back to templates; paid quota removes that.

## What more context would help

Real appointment slots and service catalog per merchant, offer validity windows as structured fields (not text like "(Tue–Thu)"), review text beyond one sample quote, and the merchant's reply history per topic (what they ignored vs. engaged with).
