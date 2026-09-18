"""Diagnose what the ``full_description_incomplete`` population is made of.

Read-only, zero LLM calls — it re-validates entities already stored in
``Document.extraction_json``, the same source ``extract_batch --from-graph``
uses.

Why this exists
---------------
The 2026-09-17 corpus run flagged 1,173 of 3,162 notices with
``full_description_incomplete`` — the largest single issue in the corpus. Before
spending model budget on a fix, this answers what the flag actually contains.
``validators.full_description_coverage`` flags a descriptive entity only when
BOTH of its arms fail (``validators.py:180-182``)::

    by_span = fd["span"][0] <= sp[0] <= sp[1] <= fd["span"][1]
    by_text = txt in fd["text"]
    if not (by_span or by_text):   # <- only then is it "escaped"

``by_text`` compares stored entity text against stored full_description text and
never reads the markdown, so a re-ingested page cannot produce this flag on its
own. Counting the arms separates a positioning problem from a genuine
text-truncation one — and, as the 2026-09-18 run shows, from a third thing
nobody was looking for: entities carrying no span at all.

What it reports
---------------
A. **Failing arm.** Which half of the check each escaped entity fell through.
   ``no_span_text_missing`` means the entity was never anchored — the same root
   cause as the ``ungrounded`` issue, and untouched by anything that changes how
   much text the model reads.
B. **Span fidelity.** Per doc, the share of stored spans where
   ``markdown[start:end]`` still aligns with the entity text. ``grounding.py``
   measured 73% exact / 83% within similarity 85 corpus-wide, so a flagged doc
   far below that has drifted markdown and its span arm is unreliable.
C. **Whether the free repair works.** Widen a lot's full_description span to the
   envelope of its descriptive spans, re-slice the text from the markdown, and
   re-test both arms. This needs no model call, so it is the cheapest available
   fix and belongs measured before any alternative. Also counts lots whose
   widened block runs into terms-and-conditions boilerplate, which is a
   different kind of wrong and needs a stop-at-terms guard.
D. **Single vs multi.** The corpus run carries no notice_type breakdown, so the
   share of the problem reachable by single-lot-only work was unknown.
E. **Escaped entities by class**, and by class x failing arm.

Only the rollup is worth keeping — the per-doc detail is ~1.4 MB and
regenerable at any time by re-running this, the same convention
``pipeline/extract_batch.py`` follows for its per-batch reports.

Run:  python -m evals.diagnose_fd_coverage [--limit N] [--json OUT.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

from pipeline.validators import _DESCRIPTION_CLASSES, _norm_ws, validate_stored

_CYPHER = (
    "MATCH (d:Document) WHERE d.extraction_json IS NOT NULL "
    "  AND d.stitched_into IS NULL "
    "OPTIONAL MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d) "
    "WITH d, collect(a.auction_id) AS aids "
    "RETURN coalesce(aids[0], d.storage_key, d.filename) AS aid, "
    "       coalesce(d.stitched_markdown, d.markdown) AS md, "
    "       d.extraction_json AS ej, "
    "       d.notice_type AS notice_type, "
    "       d.notice_type_overridden AS overridden "
    "ORDER BY aid SKIP $skip LIMIT $take"
)


def _aligned(md: str, start, end, text: str) -> bool:
    """Does the markdown under this span still carry this entity's text?

    Deliberately as forgiving as ``api/review/grounding.py``, which documents
    that a stored span is an approximate alignment and not a quotation:
    langextract normalises whitespace and punctuation as it extracts, so exact
    equality would condemn a third of a healthy corpus. Normalised containment
    either way counts as aligned.
    """
    if start is None or end is None or not text:
        return False
    try:
        sliced = _norm_ws(md[int(start):int(end)])
    except (TypeError, ValueError):
        return False
    if not sliced:
        return False
    t = _norm_ws(text)
    return t in sliced or sliced in t


def _query_https(cypher: str, params: dict) -> list[dict]:
    """Aura's HTTPS Query API (443) instead of the Bolt driver (7687).

    Sandboxed runners (Claude Code on the web, most CI containers) allow
    outbound HTTPS only, so ``api.neo4j_client`` cannot open a Bolt socket
    there and fails with ``ServiceUnavailable: Unable to retrieve routing
    information``. Same credentials, same query, still read-only.
    """
    import base64
    import urllib.request
    host = os.environ["NEO4J_URI"].split("://", 1)[1].split(":")[0]
    db = os.environ.get("NEO4J_DATABASE") or "neo4j"
    auth = base64.b64encode(
        f"{os.environ['NEO4J_USERNAME']}:{os.environ['NEO4J_PASSWORD']}".encode()
    ).decode()
    req = urllib.request.Request(
        f"https://{host}/db/{db}/query/v2",
        data=json.dumps({"statement": cypher, "parameters": params}).encode(),
        method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Basic " + auth)
    with urllib.request.urlopen(req, timeout=300) as r:
        payload = json.loads(r.read())
    fields = payload["data"]["fields"]
    return [dict(zip(fields, row)) for row in payload["data"]["values"]]


def _query(cypher: str, params: dict) -> list[dict]:
    """Bolt when it is reachable, HTTPS when it is not."""
    try:
        from api.neo4j_client import run_read_query
        return run_read_query(cypher, params, max_rows=20_000, timeout=300.0)
    except Exception:
        return _query_https(cypher, params)


def load_rows(limit: int | None, page: int = 150) -> list[tuple]:
    """Same source as ``extract_batch --from-graph``, plus notice_type.

    Paged because one response carrying every notice's markdown is tens of
    megabytes of JSON.
    """
    out: list[tuple] = []
    skip = 0
    while True:
        take = page if limit is None else min(page, limit - len(out))
        if take <= 0:
            break
        rows = _query(_CYPHER, {"skip": skip, "take": take})
        if not rows:
            break
        for r in rows:
            try:
                ents = json.loads(r["ej"] or "[]")
            except (json.JSONDecodeError, TypeError):
                ents = []
            out.append((r["aid"], r["md"] or "", ents,
                        r.get("notice_type"), r.get("overridden")))
        skip += len(rows)
        print(f"  fetched {len(out)}", flush=True)
        if len(rows) < take:
            break
    return out


def group_by_lot(ents: list[dict]) -> tuple[dict, dict]:
    """full_description blocks and descriptive entities, per lot.

    Mirrors ``full_description_coverage``'s own grouping, including its str()
    lot_index normalisation — the model emits lot_index as a number about as
    often as a string, and mixing the two raises TypeError downstream.
    """
    fd: dict = {}
    gran: dict = {}
    for e in ents:
        cls = e.get("cls")
        li = str((e.get("attrs") or {}).get("lot_index") or "1")
        sp = None if e.get("start") is None else (e.get("start"), e.get("end"))
        txt = _norm_ws(e.get("text") or "")
        if cls == "full_description":
            slot = fd.setdefault(li, {"span": None, "text": ""})
            if sp:
                slot["span"] = ((min(slot["span"][0], sp[0]),
                                 max(slot["span"][1], sp[1]))
                                if slot["span"] else sp)
            if txt:
                slot["text"] = (slot["text"] + " " + txt).strip()
        elif cls in _DESCRIPTION_CLASSES and (sp or txt):
            gran.setdefault(li, []).append((sp, txt, cls))
    return fd, gran


# Terms-of-sale boilerplate a property description must NOT swallow. Same
# vocabulary the prompt uses to tell the two blocks apart
# (pipeline/langextract_examples.py, the full_terms class).
_TERMS_MARKER = re.compile(
    r"(terms\s+(and|&)\s+conditions|as\s+is\s+where\s+is|as\s+is\s+what\s+is|"
    r"earnest\s+money|emd\s+(shall|will|is)|forfeit|bid\s+increment|"
    r"authorised\s+officer\s+(reserves|has\s+the\s+right))", re.I)


def diagnose_doc(aid, md, ents, notice_type, overridden) -> dict:
    rep = validate_stored(ents, source_text=md)
    flagged = "full_description_incomplete" in {i["code"] for i in rep["issues"]}

    spanned = [e for e in ents if e.get("start") is not None]
    aligned = sum(1 for e in spanned
                  if _aligned(md, e.get("start"), e.get("end"), e.get("text") or ""))
    fidelity = (aligned / len(spanned)) if spanned else None

    out = {
        "aid": aid, "score": rep["score"], "flagged": flagged,
        "notice_type": (notice_type or "unknown"),
        "overridden": bool(overridden),
        "n_entities": len(ents), "n_spanned": len(spanned),
        "span_fidelity": round(fidelity, 3) if fidelity is not None else None,
        "md_len": len(md),
        "arms": Counter(), "repair": Counter(), "swallow": [],
        "classes": Counter(), "class_by_arm": Counter(),
    }
    if not flagged:
        return out

    fd, gran = group_by_lot(ents)
    for li, spans in gran.items():
        block = fd.get(li)
        if not block or (block["span"] is None and not block["text"]):
            continue
        escaped = []
        for sp, txt, cls in spans:
            by_span = bool(sp and block["span"]
                           and block["span"][0] <= sp[0] <= sp[1] <= block["span"][1])
            by_text = bool(txt and block["text"] and txt in block["text"])
            if by_span or by_text:
                continue
            escaped.append((sp, txt, cls))
            # Which arm was the blocker. no_span_text_missing is the one to
            # watch: the entity was never anchored, so no amount of re-slicing
            # the notice can bring it inside the block.
            if sp is None and not txt:
                arm = "neither_span_nor_text"
            elif sp is None:
                arm = "no_span_text_missing"
            elif block["span"] is None:
                arm = "fd_has_no_span"
            else:
                arm = "span_outside_and_text_missing"
            out["arms"][arm] += 1
            out["classes"][cls] += 1
            out["class_by_arm"][f"{cls}|{arm}"] += 1
        if not escaped:
            continue

        # C. the free repair: widen the block to this lot's descriptive
        # envelope, re-slice its text from the markdown, re-test both arms.
        pts = ([sp for sp, _, _ in spans if sp]
               + ([block["span"]] if block["span"] else []))
        if not pts or not md:
            out["repair"]["no_spans_to_widen"] += 1
            continue
        lo, hi = min(p[0] for p in pts), max(p[1] for p in pts)
        try:
            widened = _norm_ws(md[int(lo):int(hi)])
        except (TypeError, ValueError):
            out["repair"]["bad_offsets"] += 1
            continue
        still_out = [c for sp, txt, c in escaped
                     if not (txt and txt in widened)
                     and not (sp and lo <= sp[0] <= sp[1] <= hi)]
        out["repair"]["lot_not_fixed" if still_out else "lot_fixed"] += 1
        grew = (hi - lo) - ((block["span"][1] - block["span"][0])
                            if block["span"] else 0)
        hits = _TERMS_MARKER.findall(md[int(lo):int(hi)])
        if hits:
            out["swallow"].append({"lot": li, "grew_chars": grew,
                                   "terms_markers": len(hits)})
    return out


def _fidelity_buckets(ds: list[dict]) -> dict:
    b: Counter = Counter()
    for d in ds:
        f = d["span_fidelity"]
        if f is None:
            b["no_spans"] += 1
        elif f >= 0.9:
            b["0.9-1.0"] += 1
        elif f >= 0.7:
            b["0.7-0.9"] += 1
        elif f >= 0.4:
            b["0.4-0.7"] += 1
        else:
            b["<0.4"] += 1
    return dict(b)


def summarise(docs: list[dict]) -> dict:
    flagged = [d for d in docs if d["flagged"]]
    arms: Counter = Counter()
    repair: Counter = Counter()
    classes: Counter = Counter()
    class_by_arm: Counter = Counter()
    by_type: Counter = Counter()
    flagged_by_type: Counter = Counter()
    swallow_docs = 0
    for d in docs:
        by_type[d["notice_type"]] += 1
        classes.update(d["classes"])
        class_by_arm.update(d["class_by_arm"])
        if d["flagged"]:
            flagged_by_type[d["notice_type"]] += 1
            arms.update(d["arms"])
            repair.update(d["repair"])
            if d["swallow"]:
                swallow_docs += 1

    def _clears(d, whole):
        fixed = d["repair"].get("lot_fixed", 0)
        unfixed = d["repair"].get("lot_not_fixed", 0)
        return (fixed > 0 and unfixed == 0) if whole else (fixed > 0 and unfixed > 0)

    n = max(len(flagged), 1)
    return {
        "docs": len(docs),
        "flagged_full_description_incomplete": len(flagged),
        "A_failing_arm": dict(arms.most_common()),
        "B_span_fidelity": {
            "all_docs": _fidelity_buckets(docs),
            "flagged_docs": _fidelity_buckets(flagged),
        },
        "C_span_union_repair": {
            "lots": dict(repair.most_common()),
            "docs_fully_cleared": sum(1 for d in flagged if _clears(d, True)),
            "docs_partly_cleared": sum(1 for d in flagged if _clears(d, False)),
            "docs_not_cleared": sum(1 for d in flagged
                                    if d["repair"].get("lot_fixed", 0) == 0),
            "docs_swallowing_terms_text": swallow_docs,
            "docs_fully_cleared_pct": round(
                sum(1 for d in flagged if _clears(d, True)) / n, 3),
        },
        "D_notice_type": {
            "all": dict(by_type),
            "flagged": dict(flagged_by_type),
            "flagged_rate": {k: round(flagged_by_type[k] / v, 3)
                             for k, v in by_type.items() if v},
            "flagged_fully_repairable": {
                nt: sum(1 for d in flagged
                        if d["notice_type"] == nt and _clears(d, True))
                for nt in by_type
            },
        },
        "E_escaped_by_class": dict(classes.most_common()),
        "E_escaped_by_class_and_arm": dict(class_by_arm.most_common()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="only diagnose the first N documents")
    ap.add_argument("--json", dest="out_path", default=None,
                    help="also write {summary, docs} here (per-doc detail is "
                         "large; the rollup alone is what is worth keeping)")
    args = ap.parse_args()

    rows = load_rows(args.limit)
    print(f"loaded {len(rows)} documents", flush=True)

    docs = []
    for i, (aid, md, ents, nt, ov) in enumerate(rows, 1):
        docs.append(diagnose_doc(aid, md, ents, nt, ov))
        if i % 250 == 0:
            print(f"  {i}/{len(rows)}", flush=True)

    summary = summarise(docs)
    for d in docs:
        for k in ("arms", "repair", "classes", "class_by_arm"):
            d[k] = dict(d[k])

    print(json.dumps(summary, indent=2))
    if args.out_path:
        with open(args.out_path, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "docs": docs}, f, indent=2)
        print(f"\nper-doc detail -> {args.out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
