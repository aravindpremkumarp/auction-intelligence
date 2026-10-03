"""Gold from disagreements: a person checks only where two readers differ.

Verifying 40 notices value by value is hours of work, and most values are
plain. Two independent reads of the same notice — the stored v1 (LangExtract,
with any reviewer corrections) and a fresh v2 read — are put in the gold
shape evals/export_review_gold.py writes, then compared field by field:

  * a value both reads give is accepted as gold with no question;
  * a value they give differently, or that only one of them gives, becomes a
    question with both values and the quote each one came from;
  * on a multi notice, lots are paired by reserve price; a lot only one read
    has becomes a "is this a real lot?" question, and a different lot count
    is asked once.

Answers come back from the review page (an Artifact whose shared database
holds one row per question) and ``--answers`` merges them into
evals/langextract_gold_reviewed.json with ``source: "review-diff"``.

**Caveat:** two reads agreeing does not prove a value right; the same OCR
error fools both. Gold built this way is labelled, so the eval can report it
apart from fully hand-checked gold.

Run:
  NEO4J_HTTP_API=1 python -m evals.gold_disagreements --build     # reads v2, writes questions
  python -m evals.gold_disagreements --answers answers.json        # writes gold
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
from pathlib import Path

from evals.export_review_gold import (FIX, OUT, _gold_fields, _gold_lots,
                                      _records_from_stored, _safe, _strata)
from evals.langextract_eval import flatten_records

HERE = Path(__file__).resolve().parent
PICK = HERE / "gold_sprint_v1_pick.json"
QUESTIONS = HERE / "gold_sprint_v1_questions.json"

#: Two of each hard kind, taken from the 40 in gold_candidates.json.
QUOTA = (("40+ lots", 1), ("20-39 lots", 1), ("tamil", 2), ("poor scan", 2),
         ("stitched", 2), ("table, missed lots", 2), ("single, table", 1),
         ("single, short description", 1))

LABELS = {
    "legal_basis": "Legal basis", "bank_name": "Bank", "assignor_bank": "Assignor bank",
    "trust_name": "Trust", "court_reference": "Court reference",
    "possession_type": "Possession", "village": "Village", "taluk": "Taluk",
    "district": "District", "registration_district": "Registration district",
    "registration_sub_district": "Sub-registrar office", "borrower_primary": "Borrower",
    "reserve_price_num": "Reserve price", "emd_num": "EMD",
}


def pick() -> list[dict]:
    cands = json.loads((HERE / "gold_candidates.json").read_text(encoding="utf-8"))
    out = []
    for bucket, n in QUOTA:
        out += [c for c in cands if c.get("bucket") == bucket][:n]
    return out


def _fetch(filename: str) -> dict | None:
    from api.neo4j_client import run_read_query
    from pipeline.load_extractions import _fetch as fetch_doc
    docs = fetch_doc(None, True, filename)
    if not docs:
        return None
    d = docs[0]
    row = run_read_query(
        "MATCH (d:Document {filename: $fn}) RETURN d.extraction_json AS ej, "
        "coalesce(d.extraction_corrections_json, '{}') AS cj", {"fn": filename}, timeout=60.0)
    d["ej"], d["cj"] = row[0]["ej"], row[0]["cj"]
    return d


def gold_shape(records: list[dict], notice_type: str) -> dict:
    """{"fields": {...}, "identifiers": {...}, "lots": [...]} for one read."""
    lots = _gold_lots(records) if notice_type == "multi" else []
    fields, ids = _gold_fields(flatten_records(records), multi=bool(lots))
    return {"fields": fields, "identifiers": ids, "lots": lots}


def quote_for(records: list[dict], value, lot: str | None = None) -> str:
    """The text of the first entity whose attrs (or text) carry ``value``."""
    want = str(value).strip().lower()
    for r in records:
        a = r.get("attrs") or {}
        if lot is not None and str(a.get("lot_index") or "1") != lot:
            continue
        vals = [str(v).strip().lower() for v in a.values() if isinstance(v, (str, int, float))]
        if want in vals or want == (r.get("text") or "").strip().lower():
            t = " ".join((r.get("text") or "").split())
            return t[:240] + ("…" if len(t) > 240 else "")
    return ""


def _lot_of_reserve(records: list[dict], reserve: int) -> str | None:
    for r in records:
        a = r.get("attrs") or {}
        if a.get("reserve_price_num") is not None:
            try:
                if int(float(a["reserve_price_num"])) == reserve:
                    return str(a.get("lot_index") or "1")
            except (TypeError, ValueError):
                continue
    return None


def _pair_lots(a: list[dict], b: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Pair lots by reserve price (each used once); return pairs, a-only, b-only."""
    left = list(b)
    pairs, a_only = [], []
    for la in a:
        m = next((lb for lb in left if lb.get("reserve_price_num") == la.get("reserve_price_num")), None)
        if m is None:
            a_only.append(la)
        else:
            left.remove(m)
            pairs.append((la, m))
    return pairs, a_only, left


def diff(aid: str, v1: dict, v2: dict, rec1: list[dict], rec2: list[dict]) -> tuple[dict, list[dict]]:
    """(agreed gold, questions) for one notice."""
    agreed: dict = {"fields": {}, "identifiers": {}, "lots": []}
    qs: list[dict] = []

    def ask(scope, key, a, b, qa, qb, lot_label=None):
        qs.append({"id": f"{aid}:{scope}:{key}", "aid": aid, "scope": scope, "key": key,
                   "label": LABELS.get(key, key.replace("_", " ")), "lot": lot_label,
                   "a": a, "b": b, "quote_a": qa, "quote_b": qb})

    for part in ("fields", "identifiers"):
        for k in sorted(set(v1[part]) | set(v2[part])):
            x, y = v1[part].get(k), v2[part].get(k)
            if x == y:
                agreed[part][k] = x
            else:
                ask(part, k, x, y, quote_for(rec1, x) if x else "", quote_for(rec2, y) if y else "")

    pairs, only1, only2 = _pair_lots(v1["lots"], v2["lots"])
    if v1["lots"] or v2["lots"]:
        if len(v1["lots"]) != len(v2["lots"]):
            ask("lot_count", "lot_count", len(v1["lots"]), len(v2["lots"]), "", "")
        for i, (la, lb) in enumerate(pairs, 1):
            lot = {"reserve_price_num": la["reserve_price_num"]}
            li1 = _lot_of_reserve(rec1, la["reserve_price_num"])
            li2 = _lot_of_reserve(rec2, lb["reserve_price_num"])
            label = f"lot with reserve ₹{la['reserve_price_num']:,}"
            for k in sorted((set(la) | set(lb)) - {"reserve_price_num", "identifiers"}):
                x, y = la.get(k), lb.get(k)
                if x == y:
                    lot[k] = x
                else:
                    ask(f"lot:{la['reserve_price_num']}", k, x, y,
                        quote_for(rec1, x, li1) if x else "", quote_for(rec2, y, li2) if y else "",
                        label)
            ids_a, ids_b = la.get("identifiers") or {}, lb.get("identifiers") or {}
            same = {k: v for k, v in ids_a.items() if ids_b.get(k) == v}
            if same:
                lot["identifiers"] = same
            agreed["lots"].append(lot)
        for side, extra, rec in (("a", only1, rec1), ("b", only2, rec2)):
            for lot in extra:
                li = _lot_of_reserve(rec, lot["reserve_price_num"])
                q = quote_for(rec, lot["reserve_price_num"], li)
                ask(f"lot:{lot['reserve_price_num']}", "real_lot",
                    lot if side == "a" else None, lot if side == "b" else None,
                    q if side == "a" else "", q if side == "b" else "",
                    f"lot with reserve ₹{lot['reserve_price_num']:,}")
    return agreed, qs


def _v2_records(d: dict) -> list[dict]:
    from pipeline.extract_entry import read_document
    ents, _model, _meta = read_document(d, reader="v2", stability="verify")
    return [{"cls": e.get("cls"), "text": e.get("text", ""), "start": e.get("start"),
             "end": e.get("end"), "attrs": e.get("attrs") or {}} for e in ents]


def build(workers: int = 4) -> int:
    chosen = pick()
    PICK.write_text(json.dumps(chosen, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out = {"notices": [], "questions": []}

    def one(c):
        d = _fetch(c["filename"])
        if d is None:
            return c, None, None, "not found"
        try:
            return c, d, _v2_records(d), None
        except Exception as e:  # one bad read must not sink the batch
            return c, d, None, f"{type(e).__name__}: {e}"

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for c, d, rec2, err in ex.map(one, chosen):
            aid = _safe(str(c["aid"]))
            if err:
                print(f"  {aid}: skipped ({err})")
                continue
            nt = "multi" if (c.get("lots") or 0) > 1 or d.get("notice_type") == "multi" else "single"
            rec1 = _records_from_stored(d["ej"], d["cj"])
            v1, v2 = gold_shape(rec1, nt), gold_shape(rec2, nt)
            agreed, qs = diff(aid, v1, v2, rec1, rec2)
            (FIX / f"{aid}.txt").write_text(d["md"] or "", encoding="utf-8")
            out["notices"].append({"aid": aid, "filename": c["filename"], "bucket": c.get("bucket"),
                                   "notice_type": nt, "strata": _strata(aid, nt, rec1, d["md"] or ""),
                                   "agreed": agreed, "questions": len(qs)})
            out["questions"] += qs
            print(f"  {aid} ({c.get('bucket')}): {len(qs)} questions")
    QUESTIONS.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str) + "\n",
                         encoding="utf-8")
    print(f"{len(out['notices'])} notices, {len(out['questions'])} questions -> {QUESTIONS.name}")
    return 0


def apply_answer(entry: dict, q: dict, ans: dict) -> None:
    """Write one answered question into a gold entry. ``ans`` is
    {"choice": "a"|"b"|"other"|"none", "value": str|None}."""
    choice = ans.get("choice")
    if choice in ("a", "b"):
        val = q[choice]
    elif choice == "other":
        val = ans.get("value")
    else:
        val = None
    if q["key"] == "lot_count":
        entry["lot_count"] = int(val) if val not in (None, "") else None
        return
    if q["scope"] in ("fields", "identifiers"):
        if val not in (None, ""):
            entry[q["scope"]][q["key"]] = (int(float(val)) if q["key"].endswith("_num") and
                                           str(val).replace(".", "", 1).isdigit() else val)
        return
    reserve = int(q["scope"].split(":", 1)[1])
    lots = entry.setdefault("lots", [])
    lot = next((x for x in lots if x.get("reserve_price_num") == reserve), None)
    if q["key"] == "real_lot":
        if choice in ("a", "b") and lot is None:
            lots.append(dict(q[choice]))
        return
    if lot is not None and val not in (None, ""):
        lot[q["key"]] = int(float(val)) if q["key"].endswith("_num") else val


def answers(path: str) -> int:
    data = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    got = json.loads(Path(path).read_text(encoding="utf-8"))
    by_q = {q["id"]: q for q in data["questions"]}
    gold = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else []
    gold = [g for g in gold if g.get("source") != "review-diff"]
    open_q = 0
    for n in data["notices"]:
        mine = [q for q in data["questions"] if q["aid"] == n["aid"]]
        if any(q["id"] not in got for q in mine):
            open_q += sum(q["id"] not in got for q in mine)
            continue  # a half-answered notice is not gold yet
        entry = {"aid": n["aid"], "notice_type": n["notice_type"],
                 "fields": dict(n["agreed"]["fields"]),
                 "identifiers": dict(n["agreed"]["identifiers"]),
                 "source": "review-diff", "strata": n["strata"]}
        if n["agreed"]["lots"] or n["notice_type"] == "multi":
            entry["lots"] = [dict(x) for x in n["agreed"]["lots"]]
        for q in mine:
            apply_answer(entry, by_q[q["id"]], got[q["id"]])
        if not entry.get("lots"):
            entry.pop("lots", None)
        gold.append(entry)
    OUT.write_text(json.dumps(gold, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    done = sum(1 for g in gold if g.get("source") == "review-diff")
    print(f"{done} notices written to {OUT.name}; {open_q} questions still open")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--answers")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    if a.build:
        return build(a.workers)
    if a.answers:
        return answers(a.answers)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
