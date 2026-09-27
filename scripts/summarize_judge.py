"""
Summarizes a judge_simulator run saved with:
    cd challenge && python judge_local.py full_evaluation 2>/dev/null | tee ../judge_run.txt && cd ..

Run from the vera-bot folder:
    python scripts/summarize_judge.py
"""
import re
from pathlib import Path

text = Path("judge_run.txt").read_text(encoding="utf-8", errors="ignore")
text = re.sub(r"\x1b\[[0-9;]*m", "", text)          # strip terminal colours

DIMS = ["Specificity", "Category Fit", "Merchant Fit", "Decision Quality", "Engagement"]
rows = []
for block in text.split("Message:")[1:]:
    opening = block.strip().splitlines()[0].strip().strip('"')[:45]
    scores = []
    for d in DIMS:
        m = re.search(rf"{d}\s+\[[^\]]*\]\s+(\d+)/10", block)
        scores.append(int(m.group(1)) if m else None)
    total = re.search(r"TOTAL:\s*(\d+)/50", block)
    fallback = "Fallback: counted numbers" in block
    rows.append((opening, scores, int(total.group(1)) if total else None, fallback))

if not rows:
    raise SystemExit("No scored messages found in judge_run.txt")

print(f"{'message':47} spec cat  mer  dec  eng  TOTAL")
for opening, s, total, fb in sorted(rows, key=lambda r: r[2] or 0):
    cells = "  ".join(f"{x:>3}" if x is not None else "  ?" for x in s)
    print(f"{opening:47} {cells}   {total}{'  <- judge LLM failed (rate limit)' if fb else ''}")

valid = [r for r in rows if r[2] is not None and not r[3]]
print(f"\nmessages scored: {len(rows)}   judge failures: {sum(r[3] for r in rows)}")
if valid:
    mean = sum(r[2] for r in valid) / len(valid)
    dims = [sum(r[1][i] for r in valid if r[1][i] is not None) / len(valid) for i in range(5)]
    print("true averages:  " + "  ".join(f"{d[:4]} {v:.1f}" for d, v in zip(DIMS, dims)))
    print(f"TRUE AVERAGE: {mean:.1f}/50 ({mean * 2:.0f}%)   (the simulator's summary rounds each dimension down)")
