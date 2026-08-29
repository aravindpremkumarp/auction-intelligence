"""
scripts/find_duplicate_notices.py
---------------------------------
Find sale notices we hold more than once under different file names.

``scripts/dedupe_documents.py`` groups on ``filename``, which is exactly what a
portal varies: it names each upload with the millisecond it was uploaded, so one
notice published against six lots arrives as six names 1.6 seconds apart
(``KARNTK17819383495370.jpg`` … ``KARNTK17819391325440.jpg``). This script
ignores the name and asks three questions of the file itself, strongest first:

    identical   same SHA-256. The same bytes stored twice — certain.
    same page   ink signatures within pipeline/ink_fingerprint's threshold.
                A re-scan, a re-crop or another resolution of one page.
    same text   stored markdown agrees >= TEXT_DUPLICATE_MIN. The same notice
                re-typeset or re-photographed — a different page carrying the
                same words, which no page-shape measure can see.

The two weaker passes complement each other rather than rank: of the corpus's
confirmed same-page pairs, two agree on only ~0.70 of their OCR text (bad scans
of one page), and of 48 pairs whose text agrees >=0.95, the median ink distance
is 0.22 (one notice, two genuinely different pages). Running only one pass would
miss whichever class it is blind to.

**Reports, never merges.** A duplicate *document* is normal and wanted: one
multi-property notice is stored once and linked to every lot it advertises.
Collapsing nodes is ``scripts/dedupe_documents.py``'s job, and the "same page"
pass cannot distinguish a duplicate from a re-auction — the same lots
re-advertised on the same template with new dates (see
``scripts/link_reauctions.py``). So the output is a list to confirm.

Runs over HTTPS (Neo4j Query API v2 + R2 source), so it works from
Bolt-firewalled environments — same pattern as scripts/score_ink_coverage.py.

Usage:
    python -m scripts.find_duplicate_notices                  # report, all docs
    python -m scripts.find_duplicate_notices --limit 200      # sample
    python -m scripts.find_duplicate_notices --csv dupes.csv  # full pairs
    python -m scripts.find_duplicate_notices --write          # cache prints
    python -m scripts.find_duplicate_notices --cached         # no downloads
    python -m scripts.find_duplicate_notices --calibrate      # threshold evidence

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE) in the environment.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from pipeline.ink_fingerprint import (
    SAME_PAGE_MAX_DISTANCE,
    content_hash,
    ink_signature,
    signature_distance,
)
from pipeline.text_overlap import tokenize


# Jaccard overlap at which two documents' stored markdown is the same notice.
# Deliberately severe. Notices from one bank share a page of boilerplate, and
# sibling lots in one auction share the schedule around them, so overlap runs
# high between documents that are not duplicates at all: across the corpus the
# 0.80-0.90 band is mostly siblings and re-auctions, and only above 0.95 does
# the band become notices that differ by nothing but OCR noise.
TEXT_DUPLICATE_MIN = 0.95
# Below this many tokens the markdown is a stub — a failed OCR, a caption — and
# its overlap with another stub is an artefact of both being short.
TEXT_MIN_TOKENS = 40


# ── Neo4j over HTTPS (Query API v2) ─────────────────────────────────────────

def _endpoint() -> tuple[str, str]:
    host = os.environ["NEO4J_URI"].split("//", 1)[1].rstrip("/")
    db = os.environ.get("NEO4J_DATABASE", "neo4j")
    auth = base64.b64encode(
        f'{os.environ["NEO4J_USERNAME"]}:{os.environ["NEO4J_PASSWORD"]}'.encode()
    ).decode()
    return f"https://{host}/db/{db}/query/v2", auth


def nq(statement: str, parameters: dict | None = None) -> list[list]:
    url, auth = _endpoint()
    body = json.dumps({"statement": statement,
                       "parameters": parameters or {}}).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Authorization": "Basic " + auth,
                         "Content-Type": "application/json",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)["data"]["values"]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


# ── selection ───────────────────────────────────────────────────────────────

def select_targets(*, limit: int | None, since_iso: str | None) -> list[dict]:
    """Documents with a fetchable raster source.

    PDFs are excluded for the same reason coverage excludes them: the ink
    measure needs a rendered page, and a PDF would have to be rasterized per
    page first. The auction ids come along because a document linked to six lots
    is the normal shape, and a reader needs that to tell a stored-once notice
    from a stored-six-times one.
    """
    rows = nq(
        f"""
        MATCH (d:Document)
        WHERE d.public_url IS NOT NULL AND d.public_url <> ''
          AND toLower(d.public_url) =~ '.*\\\\.(png|jpg|jpeg|webp)$'
        OPTIONAL MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d)
        WITH d, collect(DISTINCT a.auction_id) AS auction_ids,
             max(a.auction_start_dt)           AS latest_auction
        WHERE $t IS NULL OR latest_auction >= datetime($t)
        RETURN d.file_path, d.filename, d.public_url, d.markdown,
               d.content_sha256, d.ink_signature, auction_ids
        ORDER BY d.filename
        {'LIMIT $lim' if limit else ''}
        """,
        {"t": since_iso, **({"lim": limit} if limit else {})},
    )
    return [{"file_path": r[0], "filename": r[1], "public_url": r[2],
             "markdown": r[3], "sha": r[4], "signature": r[5],
             "auction_ids": [x for x in (r[6] or []) if x]}
            for r in rows]


# ── fingerprinting ──────────────────────────────────────────────────────────

def fingerprint_one(t: dict, *, cached: bool) -> dict:
    """Hash and fingerprint one notice. Never raises — a failure is reported.

    With ``cached``, a document that already carries both prints is taken at its
    word and never downloaded; that is what makes a re-run over a corpus this
    size cost seconds instead of a gigabyte of transfer.
    """
    out = {**t, "note": ""}
    out.pop("markdown", None)
    if cached and t.get("sha") and t.get("signature"):
        out["cached"] = True
        return out
    try:
        r = requests.get(t["public_url"], timeout=120)
        r.raise_for_status()
        sig = ink_signature(r.content)
        out["sha"] = content_hash(r.content)
        out["signature"] = sig["signature"]
        out["aspect"] = sig["aspect"]
        out["note"] = sig["skipped"] or ""
    except Exception as e:
        out["note"] = f"{type(e).__name__}: {e}"
    return out


def fingerprint_all(targets: list[dict], *, cached: bool,
                    concurrency: int) -> list[dict]:
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futs = [pool.submit(fingerprint_one, t, cached=cached) for t in targets]
        for i, fut in enumerate(as_completed(futs), 1):
            results.append(fut.result())
            if i % 100 == 0 or i == len(targets):
                print(f"  fingerprinted {i}/{len(targets)}", flush=True)
    return results


# ── grouping ────────────────────────────────────────────────────────────────

def exact_groups(rows: list[dict]) -> list[list[dict]]:
    """Documents sharing a SHA-256, grouped."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("sha"):
            by.setdefault(r["sha"], []).append(r)
    return [g for g in by.values() if len(g) > 1]


def same_page_pairs(rows: list[dict]) -> list[tuple[dict, dict, float]]:
    """Pairs whose ink signatures put them on the same page.

    Only one representative per SHA is compared: exact copies are already
    reported, and leaving them in would bury the interesting pairs under their
    own zero-distance matches. O(n²) over distinct pages, which is ~1.2M cheap
    integer comparisons at this corpus size; a corpus an order of magnitude
    larger would want bucketing by signature band first.
    """
    seen: dict[str, dict] = {}
    for r in rows:
        if r.get("signature") and r.get("sha"):
            seen.setdefault(r["sha"], r)
    d = list(seen.values())
    out = []
    for i in range(len(d)):
        for j in range(i + 1, len(d)):
            dist = signature_distance(d[i]["signature"], d[j]["signature"])
            if dist is not None and dist <= SAME_PAGE_MAX_DISTANCE:
                out.append((d[i], d[j], dist))
    return sorted(out, key=lambda p: p[2])


def same_text_pairs(rows: list[dict], markdown: dict[str, str],
                    ) -> list[tuple[dict, dict, float]]:
    """Pairs whose stored markdown is the same notice.

    One representative per SHA, as with the ink pass: exact copies are reported
    already and would otherwise fill this list with their own perfect scores.

    Tokenized once per document rather than once per pair: over a corpus this
    size ``text_overlap.description_overlap`` would re-tokenize both sides
    1.2M times. The overlap itself is that module's definition, arithmetic and
    all — shared tokens over all tokens, symmetric.
    """
    seen: dict[str, dict] = {}
    for r in rows:
        if r.get("sha"):
            seen.setdefault(r["sha"], r)
    scorable = [(r, tokenize(markdown.get(r["filename"]))) for r in seen.values()]
    d = [r for r, t in scorable if len(t) >= TEXT_MIN_TOKENS]
    toks = [t for _, t in scorable if len(t) >= TEXT_MIN_TOKENS]
    out = []
    for i in range(len(d)):
        a = toks[i]
        for j in range(i + 1, len(d)):
            b = toks[j]
            inter = len(a & b)
            if not inter:
                continue
            score = inter / (len(a) + len(b) - inter)
            if score >= TEXT_DUPLICATE_MIN:
                out.append((d[i], d[j], round(score, 4)))
    return sorted(out, key=lambda p: -p[2])


# ── persistence ─────────────────────────────────────────────────────────────

def write_prints(rows: list[dict]) -> int:
    """Cache each document's prints so the next run needs no download."""
    payload = [{"file_path": r["file_path"], "sha": r["sha"],
                "signature": r.get("signature")}
               for r in rows if r.get("file_path") and r.get("sha")
               and not r.get("cached")]
    if not payload:
        return 0
    nq(
        """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.content_sha256   = row.sha,
            d.ink_signature    = row.signature,
            d.fingerprinted_at = datetime()
        """,
        {"rows": payload},
    )
    return len(payload)


# ── reporting ───────────────────────────────────────────────────────────────

def _label(r: dict) -> str:
    lots = len(r.get("auction_ids") or [])
    return f"{r['filename'][:44]:46} {lots} lot(s)"


def report(rows: list[dict], markdown: dict[str, str], *, csv_path: str | None,
           ) -> list[dict]:
    exact = exact_groups(rows)
    pages = same_page_pairs(rows)
    texts = same_text_pairs(rows, markdown)

    redundant = sum(len(g) - 1 for g in exact)
    print(f"\n{'=' * 72}")
    print(f"IDENTICAL BYTES — {len(exact)} group(s), {redundant} redundant copy/ies")
    print(f"{'=' * 72}")
    for g in sorted(exact, key=len, reverse=True):
        print(f"  x{len(g)}  {g[0]['sha'][:12]}…")
        for r in g:
            print(f"       {_label(r)}")

    seen_pairs = {(a["sha"], b["sha"]) for a, b, _ in pages}
    print(f"\n{'=' * 72}")
    print(f"SAME PAGE (ink distance <= {SAME_PAGE_MAX_DISTANCE}) — {len(pages)} pair(s)")
    print("  confirm before collapsing: a re-auction of the same lots looks "
          "like this too")
    print(f"{'=' * 72}")
    for a, b, dist in pages:
        print(f"  ink {dist:.3f}  {_label(a)}")
        print(f"             {_label(b)}")

    fresh = [(a, b, s) for a, b, s in texts
             if (a["sha"], b["sha"]) not in seen_pairs]
    print(f"\n{'=' * 72}")
    print(f"SAME TEXT (markdown overlap >= {TEXT_DUPLICATE_MIN}) — "
          f"{len(fresh)} further pair(s)")
    print("  the same notice on a different page — re-typeset, re-photographed")
    print(f"{'=' * 72}")
    for a, b, score in fresh:
        print(f"  text {score:.3f}  {_label(a)}")
        print(f"              {_label(b)}")

    unread = [r for r in rows if not r.get("sha")]
    if unread:
        print(f"\n{len(unread)} document(s) could not be fingerprinted:")
        for r in unread[:10]:
            print(f"    {r['filename'][:44]:46} {r['note'][:60]}")

    pairs: list[dict] = []
    for g in exact:
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                pairs.append({"kind": "identical", "score": 0.0,
                              "a": g[i]["filename"], "b": g[j]["filename"],
                              "a_url": g[i]["public_url"], "b_url": g[j]["public_url"],
                              "a_lots": len(g[i]["auction_ids"]),
                              "b_lots": len(g[j]["auction_ids"])})
    for kind, items in (("same-page", pages), ("same-text", fresh)):
        for a, b, score in items:
            pairs.append({"kind": kind, "score": score,
                          "a": a["filename"], "b": b["filename"],
                          "a_url": a["public_url"], "b_url": b["public_url"],
                          "a_lots": len(a["auction_ids"]),
                          "b_lots": len(b["auction_ids"])})
    if csv_path:
        with open(csv_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(pairs[0].keys()) if pairs else
                               ["kind", "score", "a", "b", "a_url", "b_url",
                                "a_lots", "b_lots"])
            w.writeheader()
            w.writerows(pairs)
        print(f"\nWrote {len(pairs)} pair(s) to {csv_path}")
    return pairs


def calibrate(rows: list[dict], markdown: dict[str, str]) -> None:
    """Print the evidence behind ``SAME_PAGE_MAX_DISTANCE``.

    For each band of ink distance, how many pairs sit in it and how many of them
    the stored text independently calls the same notice. The threshold is where
    that agreement stops being the rule and starts being the exception, and this
    is how to re-derive it when the corpus or the ink rules change.
    """
    seen: dict[str, dict] = {}
    for r in rows:
        if r.get("signature") and r.get("sha"):
            seen.setdefault(r["sha"], r)
    d = list(seen.values())
    toks = [tokenize(markdown.get(r["filename"])) for r in d]

    def overlap(i: int, j: int) -> float | None:
        ta, tb = toks[i], toks[j]
        if len(ta) < TEXT_MIN_TOKENS or len(tb) < TEXT_MIN_TOKENS:
            return None
        inter = len(ta & tb)
        return inter / (len(ta) + len(tb) - inter) if inter else 0.0

    bands = [(0.0, 0.06), (0.06, 0.08), (0.08, 0.10), (0.10, 0.12),
             (0.12, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 1.01)]
    counts = {b: [0, 0, 0] for b in bands}       # pairs, text>=min, text scored
    for i in range(len(d)):
        for j in range(i + 1, len(d)):
            dist = signature_distance(d[i]["signature"], d[j]["signature"])
            if dist is None:
                continue
            for lo, hi in bands:
                if lo <= dist < hi:
                    c = counts[(lo, hi)]
                    c[0] += 1
                    o = overlap(i, j)
                    if o is not None:
                        c[2] += 1
                        c[1] += o >= TEXT_DUPLICATE_MIN
                    break
    print(f"\n{len(d)} distinct pages, {len(d) * (len(d) - 1) // 2} pairs")
    print(f"{'ink distance':>16}  {'pairs':>9}  {'text agrees':>12}")
    for lo, hi in bands:
        pairs, agree, scored = counts[(lo, hi)]
        share = f"{agree}/{scored}" if scored else "—"
        mark = "  <- threshold" if hi == SAME_PAGE_MAX_DISTANCE else ""
        print(f"{lo:6.2f}-{hi:<6.2f}  {pairs:12d}  {share:>12}{mark}")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None,
                    help="fingerprint at most this many Documents (default: all)")
    ap.add_argument("--since", default=None,
                    help="restrict to notices whose auction is on/after this date")
    ap.add_argument("--cached", action="store_true",
                    help="reuse prints already stored on the node; download only "
                         "what has none")
    ap.add_argument("--write", action="store_true",
                    help="store each document's prints so later runs can use "
                         "--cached")
    ap.add_argument("--calibrate", action="store_true",
                    help="print the ink-distance bands and how often the stored "
                         "text agrees, instead of the duplicate report")
    ap.add_argument("--csv", default=None, help="write every reported pair here")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    since_iso = f"{args.since}T00:00:00Z" if args.since else None
    targets = select_targets(limit=args.limit, since_iso=since_iso)
    print(f"Selected {len(targets)} Document(s) with a raster source")
    if not targets:
        return 0
    markdown = {t["filename"]: t.get("markdown") for t in targets}

    rows = fingerprint_all(targets, cached=args.cached,
                           concurrency=args.concurrency)
    scored = [r for r in rows if r.get("sha")]
    signed = [r for r in scored if r.get("signature")]
    print(f"Fingerprinted {len(scored)}/{len(rows)}; "
          f"{len(signed)} carry an ink signature")

    if args.calibrate:
        calibrate(rows, markdown)
    else:
        report(rows, markdown, csv_path=args.csv)

    if args.write:
        print(f"\nStored prints on {write_prints(rows)} Document(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
