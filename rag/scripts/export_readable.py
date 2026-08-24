import csv
from pathlib import Path

rows = list(csv.DictReader(open("rag/data/exp_comparison.csv", encoding="utf-8")))
out = Path("rag/data/exp_comparison_readable.txt")
with out.open("w", encoding="utf-8") as f:
    for i, r in enumerate(rows, 1):
        f.write(f"===== ROW {i} =====\n")
        f.write("--- QUESTION ---\n" + r["question"].strip() + "\n")
        f.write("--- PROD ANSWER (no telegram) ---\n" + r["answer"].strip() + "\n")
        f.write("--- EXP ANSWER (with telegram) ---\n" + r["exp_answer"].strip() + "\n\n")
print("done", len(rows))
