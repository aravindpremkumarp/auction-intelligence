"""Refresh banks.csv: each notice's raw bank_name, so compare.py runs offline.

    NEO4J_HTTP_API=1 python experiments/bank_entity_resolution/fetch_banks.py banks.csv

Same query and same one-lender-per-notice rule as scripts/resolve_bank_names.py,
so the snapshot matches what production resolves.
"""
import csv
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scripts.score_ink_coverage import nq  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else str(
    pathlib.Path(__file__).with_name("banks.csv"))
rows = nq("""
    MATCH (d:Document)
    WHERE d.extraction_json IS NOT NULL
    RETURN d.file_path, d.extraction_json
""")
out = []
for file_path, ej in rows:
    try:
        entities = json.loads(ej or "[]")
    except (TypeError, ValueError):
        continue
    for e in entities:
        name = ((e.get("attrs") or {}).get("bank_name") or "").strip()
        if name:
            out.append((file_path, name))
            break
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["file_path", "bank_name"])
    w.writerows(out)
print(f"{len(out)} notices, {len({n for _, n in out})} distinct names -> {OUT}")
