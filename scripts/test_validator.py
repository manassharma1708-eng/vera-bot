"""
Tests the validator on good and deliberately bad messages, and checks that
every fallback template passes it. No server or API key needed.

Run from the vera-bot folder:
    python scripts/test_validator.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.composer import fallback_message  # noqa: E402
from app.facts import build_facts  # noqa: E402
from app.validator import validate  # noqa: E402

DATA = Path("challenge/expanded")


def load(folder, item_id):
    if not item_id:
        return None
    path = DATA / folder / f"{item_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def facts_for(trigger_id):
    t = load("triggers", trigger_id)
    m = load("merchants", t["merchant_id"])
    return build_facts(load("categories", m["category_slug"]), m, t, load("customers", t.get("customer_id")))


results = []


def check(name, problems, expect_ok):
    ok = (not problems) == expect_ok
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    for p in problems:
        print(f"        - {p}")


dci = facts_for("trg_002_compliance_dci_radiograph")
check("good DCI message passes", validate(
    "Dr. Meera, per the Dental Council of India circular, IOPA dose limits drop from 1.5 to 1.0 mSv on 15 Dec 2026 — "
    "D-speed film won't pass, RVG sensors are unaffected. Want me to prepare a 3-point compliance checklist for your setup?",
    "binary_yes_no", dci), True)
check("your real T30 output ('remain safe') gets flagged", validate(
    "Dr. Meera, the Dental Council of India has revised radiograph dose limits effective 15th Dec 2026, and you need to audit "
    "your setup before this deadline. The new limit drops to 1.0 mSv per IOPA, meaning D-speed films will no longer comply, "
    "though RVG sensors remain safe. Should I send over a 3-point compliance checklist to help you update your SOPs?",
    "binary_yes_no", dci), False)
check("invented statistic gets flagged", validate(
    "Dr. Meera, DCI just cut IOPA limits — 73% of Delhi clinics are affected. Want me to prepare the checklist?",
    "binary_yes_no", dci), False)
check("missing citation gets flagged", validate(
    "Dr. Meera, new radiograph dose limits start 15 Dec 2026: 1.0 mSv per IOPA. Want me to prepare a checklist?",
    "binary_yes_no", dci), False)
check("URL gets flagged", validate(
    "Dr. Meera, DCI revised IOPA limits to 1.0 mSv. Details at www.dci.gov.in — want the checklist?",
    "binary_yes_no", dci), False)
check("jargon gets flagged", validate(
    "Dr. Meera, your ctr_below_peer_median signal fired and DCI changed limits. Want the checklist?",
    "binary_yes_no", dci), False)
check("no CTA at the end gets flagged", validate(
    "Dr. Meera, DCI revised IOPA limits to 1.0 mSv from 15 Dec 2026. Reply if you want help. RVG is unaffected.",
    "binary_yes_no", dci), False)

priya = facts_for("trg_003_recall_due_priya")
check("customer message leaking 'magicpin' gets flagged", validate(
    "Hi Priya, Dr. Meera's clinic here via magicpin. Your 6 month cleaning is due — Wed 5 Nov, 6pm ya Thu 6 Nov, 5pm? Reply 1 or 2.",
    "multi_choice_slot", priya), False)
check("good customer recall passes", validate(
    "Hi Priya, Dr. Meera's clinic here 🦷 Aapki 6 month cleaning due hai. Slots: Wed 5 Nov, 6pm ya Thu 6 Nov, 5pm — "
    "Dental Cleaning @ ₹299. Reply 1 for Wed, 2 for Thu.", "multi_choice_slot", priya), True)
check("repeat of an earlier message gets flagged", validate(
    "Hi Priya, Dr. Meera's clinic here. Your cleaning is due — reply YES for a slot.", "binary_yes_no", priya,
    avoid_bodies=["Hi Priya, Dr. Meera's clinic here. Your cleaning is due — reply YES for a slot."]), False)

# Every fallback template must pass the validator (it's our safety net)
pairs = json.loads((DATA / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]
bad = []
for p in pairs:
    f = facts_for(p["trigger_id"])
    if f["send_blockers"]:
        continue
    fb = fallback_message(f)
    probs = validate(fb["body"], fb["cta"], f)
    if probs:
        bad.append((p["test_id"], fb["body"], probs))
for tid, body, probs in bad:
    print(f"   {tid}: {body}\n      {probs}")
check(f"all fallback templates pass the validator ({26 - len(bad)}/26)", bad and ["some failed"] or [], True)

print(f"\n{sum(results)}/{len(results)} passed")