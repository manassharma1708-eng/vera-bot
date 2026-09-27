# Vera Bot — magicpin AI Challenge

A merchant-engagement bot that chooses what to say, whom to say it to, and whether saying it at all, and writes them — in context of what’s been delivered already.

Endpoints: `POST /v1/context` · `POST /v1/tick` · `POST /v1/reply` · `GET /v1/healthz` · `GET /v1/metadata` (+ optional `POST /v1/teardown`)

## Approach
```

contexts ──► fact sheet ──► LLM composer ──► validator ──► send
(verified)   (per-trigger   (10 rules)   │
playbook)     │ fail   │
▼      │
one LLM repair ──► fact-only template
```

1. Fact sheet (deterministic). For each (category, merchant, trigger, customer) the bot selects verifiable facts, scores them, picks one anchor — what drives the message (digest item > trigger event > CTR gap / big metric swing > review theme…), sets hard blockers: no consent for this message type, trigger that doesn’t match the category (e.g. `recall_due` for a gym), missing customer. Messages to customers never expose internal metrics.
2. Composer (LLM). Temperature 0, one prompt per trigger kind (26 playbooks: research digests cite sources, `curious_ask` asks instead of pitching, `active_planning` delivers a draft instead of qualifying, IPL checks whether the merchant’s offer is even valid on match day…). Output is JSON: body, CTA, topic, rationale.
3. Validator (deterministic). All numbers present in the fact sheet (prices match, percentages real), no taboo/overclaiming words, no URLs, jargon, multiple CTAs, last sentence has the CTA, source cited for research/compliance, name used, no repetitions, customer messages don’t mention Vera/magicpin/metrics. Violations are sent back to the LLM as explicit fix instructions; if the LLM fails to fix, a fact-only template is emitted.
4. Tick engine. Suppression-key dedup, opt-out and “wait” respected, one message per recipient per tick (highest urgency wins), no second thread while one is live (unless urgent or quiet 30+ min), max 20 actions, parallel composition inside an 11 s budget — anything not composed in time gets its template instead of being dropped.
5. Reply brain (rule-first). Opt-out → end; hostility → one apology with a STOP exit; auto-reply (English + Hindi patterns, tracked per merchant across replies) → one nudge, then wait 24 h, then end; busy → wait; off-topic (GST, loans…) → polite decline + redirect; commitment ("let's do it", "judna hai") → straight to action, no qualifying. Open questions get a grounded LLM reply that also passes the validator. Replies use the same language as the message (Hinglish in, Hinglish out).

## Model choice

Provider-agnostic chain from `LLM_ORDER`, e.g. Gemini Flash → Groq gpt-oss-120b → gpt-oss-20b. Each model has its own quota; 429s result in a short retry or a cooldown so a blocked model is skipped immediately. Every prompt is cached by hash (memory + disk), so that identical input yields identical output.

## Results (local, official `judge_simulator.py`)

- Scored messages: ~41.9 / 50 average (84%); 9 of 10 between 41 and 47.
- Replay scenarios: auto-reply, intent transition, hostile — all PASS.
- Canonical 30 pairs: 26 composed, 4 deliberately skipped (consent doesn’t allow the message, or trigger doesn’t match the category).

## Tradeoffs

- Restraint over volume. Skipping incoherent or non-consented pairs loses those messages but avoids spam and consent violations.
- Rules before the LLM. Replies to opt-outs, auto-replies and commitments are regex/state-based: faster, use no tokens, deterministic — but a novel phrasing may be falsely identified as such, and fall into the safe “engaged” category.
- Validator strictness. Rejecting any number not on the fact sheet may wrongly reject a valid one, wasting a repair turn; I prefer that to accepting a made-up statistic.
- In-memory state. Fast and light, but a crash or a restart erases everything (the judge re-posts contexts); it runs as a single always-on instance.
- Time budget. On free-tier quotas some messages in a large tick may be deferred to the next tick; paid quota removes the problem.

## What more context would help

Real appointment slots and service catalog for each merchant, offer validity windows as structured fields (not text like "(Tue–Thu)"), review text beyond one sample quote, and the merchant’s reply history per topic (what they engaged / didn’t engage with).