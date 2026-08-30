"""Apply grounded extractions (Document.extraction_json) to :AuctionProperty.

This is the bridge that makes the grounded LangExtract path (populated by
pipeline/load_extractions.py, reviewed via /review/extraction) the source of
the user-facing property fields — replacing the flat per-file blob path for
every Document that has an extraction.

Per Document:
  1. Parse extraction_json ([{id, cls, text, start, end, attrs}]) and overlay
     reviewer corrections (extraction_corrections_json {field_id: {value,..}} —
     a correction replaces the entity's text).
  2. Group entities by lot_index into per-lot records: description
     (full_description spans), location attrs, boundaries (adjacency +
     measurement per side), extent/UDS, door numbers, reserve price, and the
     normalized property type / asset category (pipeline.property_taxonomy).
  3. Match lots to the Document's linked AuctionProperty listings:
       - 1 lot        -> every listing (the 'single' case)
       - N lots       -> reserve price exact, then ±1%, then EMD exact/±1%
                         (for listings the portal shows without a reserve
                         price), then borrower-name overlap (separates lots
                         that tie on money), then, if exactly one lot and one
                         listing remain, pair them
     Unmatched listings are logged to data/grounded_unmatched.csv.
  4. Write fields onto AuctionProperty. The description write treats the
     grounded notice text as the sole source — it overwrites even the legacy
     pipeline's human-verified rows (stashing them once into
     description_human_backup) — but on a multi-lot notice only for a listing
     that is its lot's SOLE claimant. A schedule published on two rival
     listings is false about at least one of them, and reads as authoritative
     while it is; the portal's own text is the honest fallback, and a listing
     withheld here is REVERTED to it (see revert_withheld_descriptions) —
     a gate alone only stops the next write and leaves everything an earlier
     run published still live. Enrichment fields use the same property names as
     pipeline/load_enriched.flatten_enrichment so the API and UI keep working
     unchanged; only non-null values are written (SET +=).
  5. Also write AuctionProperty.resolved_lot_key from the SAME lot match
     step 3 already computed — the match was previously used only to route
     field/description writes and then discarded. This is a strictly better
     source for "which lot is this listing" than pipeline/lot_resolution.py
     (reserve+borrower only, run separately against the :Lot graph): it adds
     EMD and survey/door-identifier tiers, and reads the live extraction
     directly rather than a possibly-stale graph copy of it. It overwrites
     any prior AUTOMATED lot-match verdict when the two disagree, but never
     a human's (see human_decided_lot_matches / write_lot_matches).

Run:  python -m pipeline.apply_extractions [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from api.neo4j_client import run_query, run_read_query
from pipeline.obs import get_logger
from pipeline.property_taxonomy import asset_category, classify_property_type
from pipeline.resolution_review import lot_match_key
from pipeline.text_overlap import description_overlap

log = get_logger(__name__)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UNMATCHED_CSV = REPO_ROOT / "data" / "grounded_unmatched.csv"

PRICE_TOLERANCE_PCT = 1.0
WRITE_CHUNK = 200

_SIDES = ("north", "south", "east", "west")


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ── pure: entity parsing ─────────────────────────────────────────────────────

def parse_money(v) -> int | None:
    """Coerce an attr value to integer rupees. The prompt already asks for
    integer rupees; this only absorbs strings with grouping ('12,50,000',
    'Rs. 950000/-'). No unit arithmetic here — a bare number is trusted as
    rupees, anything unparseable is None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return int(float(m.group(0)))
    except ValueError:
        return None


def entities_with_corrections(extraction_json: str,
                              corrections_json: str | None) -> list[dict]:
    """Parse the stored entity list; a reviewer correction replaces text."""
    try:
        ents = json.loads(extraction_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(ents, list):
        return []
    try:
        corr = json.loads(corrections_json or "{}")
    except json.JSONDecodeError:
        corr = {}
    out = []
    for i, e in enumerate(ents):
        if not isinstance(e, dict):
            continue
        e = dict(e)
        fid = e.get("id") or str(i)
        c = corr.get(fid) if isinstance(corr, dict) else None
        if isinstance(c, dict) and isinstance(c.get("value"), str) and c["value"].strip():
            e["text"] = c["value"].strip()
            e["corrected"] = True
        out.append(e)
    return out


def _first(d: dict, cur):
    return cur if cur not in (None, "") else d


# Honorifics and relation markers carry no identity — "W/o. Pavadai" names the
# husband, but the token "pavadai" still discriminates between lots, so only
# the markers themselves are stripped.
_HONORIFICS = re.compile(
    r"\b(sri|smt|shri|mr|mrs|ms|thiru|tmt|selvi|dr|m/s|late|alias|"
    r"s/o|w/o|d/o|h/o|borrower|guarantor|mortgagor|co-obligant)\b\.?", re.I)


def _name_tokens(name: str) -> set[str]:
    """Identity-bearing tokens of a person/firm name: lowercased words of 3+
    chars, honorifics stripped. Initials are dropped — they collide across
    family members far too often to discriminate lots."""
    s = _HONORIFICS.sub(" ", str(name or "").lower())
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return {t for t in s.split() if len(t) >= 3}


def _names_overlap(a: set[str], b: set[str]) -> bool:
    """True when the token sets share a name. Exact token or containment
    (len>=4) so OCR-fused variants like 'sgayathri' still hit 'gayathri'."""
    for x in a:
        for y in b:
            if x == y or (len(x) >= 4 and len(y) >= 4 and (x in y or y in x)):
                return True
    return False


# Survey/door/plot identifiers as they appear in notices and portal text:
# "491/1", "32-12B", "S.F.No. 203/2A". Separators vary per source, so they
# normalize to '/'. Bare years and 6-digit pincodes match this shape too and
# appear in almost every listing — they carry no lot identity and are dropped.
_ID_SHAPE = re.compile(r"\b\d+(?:[/\-.]\d*[a-z]*\d*)+\b|\b\d+[a-z]\b")


def _id_norm(v: str) -> str:
    return re.sub(r"[\-.]", "/", str(v).strip().lower())


def _id_tokens(text: str) -> set[str]:
    out = set()
    for m in _ID_SHAPE.finditer(str(text or "").lower()):
        tok = _id_norm(m.group(0))
        if re.fullmatch(r"(19|20)\d{2}|\d{6,}", tok):
            continue
        out.add(tok)
    return out




def group_lots(entities: list[dict]) -> dict[str, dict]:
    """Group grounded entities into per-lot records.

    Returns {lot_index: {description, fields{...flat props...}, reserve}}.
    First-non-null wins within a lot (mirrors the merge policy elsewhere in
    the pipeline); descriptions concatenate in entity order.
    """
    lots: dict[str, dict] = {}

    def lot(li: str) -> dict:
        return lots.setdefault(li, {
            "description_parts": [],
            "fields": {},
            "reserve": None,
            "emd": None,
            "borrower_tokens": set(),
            "id_tokens": set(),
            "doors_old": [],
            "doors_new": [],
        })

    for e in entities:
        cls = e.get("cls")
        attrs = e.get("attrs") or {}
        li = str(attrs.get("lot_index") or "1")
        text = (e.get("text") or "").strip()
        rec = lot(li)
        f = rec["fields"]

        if cls == "full_description":
            if text:
                rec["description_parts"].append(text)
                # The schedule text is where a per-unit identifier actually
                # lives — a property tax assessment number, most usefully.
                # Sibling flats in one building share their price, extent,
                # borrower and even the survey number of the land under
                # them, so those tiers all tie; the assessment number is
                # assigned per unit and is the one thing that differs.
                # Tokens shared across lots (the common survey number, the
                # neighbouring plot numbers in the boundaries) simply never
                # narrow the candidate set, so harvesting the whole schedule
                # costs nothing and catches the one token that does.
                rec["id_tokens"] |= _id_tokens(text)

        elif cls == "location":
            for k in ("village", "taluk", "district",
                      "registration_district", "registration_sub_district"):
                v = attrs.get(k)
                if v not in (None, "") and k not in f:
                    f[k] = str(v).strip()

        elif cls == "extent":
            for src, dst in (("undivided_share", "undivided_share"),
                             ("total_area", "total_area")):
                v = attrs.get(src)
                if v not in (None, "") and dst not in f:
                    f[dst] = str(v).strip()

        elif cls == "boundary":
            side = str(attrs.get("side") or "").strip().lower()
            if side in _SIDES:
                adj = attrs.get("adjacency")
                val = str(adj).strip() if adj not in (None, "") else text
                if val and f.get(f"boundary_{side}") in (None, ""):
                    f[f"boundary_{side}"] = val
                meas = attrs.get("measurement")
                if meas not in (None, "") and \
                        f.get(f"boundary_measurement_{side}") in (None, ""):
                    f[f"boundary_measurement_{side}"] = str(meas).strip()

        elif cls == "identifier":
            kind = str(attrs.get("kind") or "").strip().lower()
            val = attrs.get("value")
            val = str(val).strip() if val not in (None, "") else text
            if val:
                if kind == "door_old":
                    rec["doors_old"].append(val)
                elif kind == "door_new":
                    rec["doors_new"].append(val)
                rec["id_tokens"] |= _id_tokens(val)

        elif cls == "borrower":
            rec["borrower_tokens"] |= _name_tokens(text)

        elif cls == "property":
            v = attrs.get("property_type")
            if v not in (None, "") and "property_type_raw" not in f:
                raw = str(v).strip()
                bucket = classify_property_type(raw)
                f["property_type_raw"] = raw
                f["property_type_norm"] = bucket
                f["asset_category_norm"] = asset_category(bucket, raw)

        elif cls == "auction_terms":
            r = parse_money(attrs.get("reserve_price_num"))
            if r is not None and rec["reserve"] is None:
                rec["reserve"] = r
            m = parse_money(attrs.get("emd_num"))
            if m is not None and rec["emd"] is None:
                rec["emd"] = m

    # finalize
    out: dict[str, dict] = {}
    for li, rec in lots.items():
        f = dict(rec["fields"])
        if rec["doors_old"]:
            f["door_numbers_old"] = ", ".join(dict.fromkeys(rec["doors_old"]))
        if rec["doors_new"]:
            f["door_numbers_new"] = ", ".join(dict.fromkeys(rec["doors_new"]))
        out[li] = {
            "lot_index": li,
            "description": "\n\n".join(rec["description_parts"]) or None,
            "fields": f,
            "reserve": rec["reserve"],
            "emd": rec["emd"],
            "borrower_tokens": rec["borrower_tokens"],
            "id_tokens": rec["id_tokens"],
        }
    return out


# ── pure: lot ↔ listing matching ─────────────────────────────────────────────

def _key_match(lot_list: list[dict], key: str, value) -> tuple[list[int], bool]:
    """Indexes of lots whose `key` equals value, else within ±1%.
    Second element: whether the hit was exact."""
    exact = [i for i, lo in enumerate(lot_list)
             if lo.get(key) is not None and lo[key] == value]
    if exact:
        return exact, True
    tol = abs(value) * PRICE_TOLERANCE_PCT / 100.0
    return [i for i, lo in enumerate(lot_list)
            if lo.get(key) is not None and abs(lo[key] - value) <= tol], False


def match_lots_to_listings(lots: dict[str, dict],
                           listings: list[dict]) -> tuple[list[tuple[dict, dict, str]],
                                                          list[tuple[dict, str]]]:
    """Assign each listing to at most one lot.

    listings: [{aid, price, emd?, borrowers?, id_text?}]. Returns (matches,
    unmatched) where matches is [(listing, lot, reason)] and unmatched is
    [(listing, reason)]. reason ∈ 'single' | 'exact' | 'tolerance' | 'emd' |
    'emd_tolerance' | 'borrower' | 'identifier' | 'remainder' | 'ambiguous' |
    'none'.

    Keys narrow in order of trustworthiness: reserve price exact/±1%, then
    EMD exact/±1% (rescues listings the portal shows without a price, and 10x
    price typos), then borrower-name overlap, then survey/door identifiers
    found in the listing's own text (id_text: title + portal description).
    Borrower and identifiers are what separate lots that tie on money — EMD
    cannot, being 10% of the reserve almost everywhere. Every key must reduce
    to exactly one lot; a tie that survives all keys stays 'ambiguous' rather
    than being guessed, and a listing none of whose keys hit anything falls
    through to the unique-remainder rule.
    """
    lot_list = list(lots.values())
    if not lot_list:
        return [], [(l, "no_lots") for l in listings]

    if len(lot_list) == 1:
        return [(l, lot_list[0], "single") for l in listings], []

    matches: list[tuple[dict, dict, str]] = []
    unmatched: list[tuple[dict, str]] = []
    taken: set[int] = set()
    pending: list[dict] = []

    for listing in listings:
        price = listing.get("price")
        emd = listing.get("emd")
        borrowers = set()
        for name in listing.get("borrowers") or []:
            borrowers |= _name_tokens(name)
        if price is None and emd is None and not borrowers:
            unmatched.append((listing, "no_listing_price"))
            continue

        # Each key either narrows the candidate set or is ignored; reason
        # records the first key that produced a hit.
        cands = list(range(len(lot_list)))
        reason = None

        if price is not None:
            idx, was_exact = _key_match(lot_list, "reserve", price)
            if idx:
                cands = idx
                reason = "exact" if was_exact else "tolerance"

        if len(cands) > 1 and emd is not None:
            sub = [lot_list[i] for i in cands]
            idx, was_exact = _key_match(sub, "emd", emd)
            if idx:
                cands = [cands[i] for i in idx]
                reason = reason or ("emd" if was_exact else "emd_tolerance")

        if len(cands) > 1 and borrowers:
            idx = [i for i in cands
                   if _names_overlap(borrowers,
                                     lot_list[i].get("borrower_tokens") or set())]
            if idx and len(idx) < len(cands):
                cands = idx
                reason = reason or "borrower"
                if len(cands) == 1:
                    reason = "borrower"

        if len(cands) > 1:
            listing_ids = _id_tokens(listing.get("id_text") or "")
            if listing_ids:
                # Only what the candidates DON'T say in common can separate
                # them. Sibling flats quote the same land in their schedules
                # — the survey number, the neighbouring plots, the parcel's
                # measurements — so every candidate overlaps the listing and
                # a plain "has any overlap" test matches all of them. Discount
                # that shared ground, and what remains is the per-unit
                # identifier: an assessment number the listing quotes for
                # exactly one lot.
                #
                # Then demand that exactly ONE candidate has any of it. Ranking
                # by raw overlap instead would let a one-token lead decide, and
                # a one-token lead over sixteen shared is OCR noise, not
                # evidence — on the 12-lot PNB notice it picked lot 3 (17
                # tokens) over lot 2 (16) for two different listings at once,
                # which cannot both be right. Two candidates still carrying a
                # distinguishing token means the notice does not separate them,
                # so this leaves the listing for a human, as everywhere else
                # in this function.
                sets = [lot_list[i].get("id_tokens") or set() for i in cands]
                shared = set.intersection(*sets) if sets else set()
                distinct = [listing_ids & (s - shared) for s in sets]
                hits = [c for c, d in zip(cands, distinct) if d]
                if len(hits) == 1 and len(hits) < len(cands):
                    cands = hits
                    reason = "identifier"

        if reason is None:
            # no key hit anything — leave for the unique-remainder rule
            pending.append(listing)
        elif len(cands) == 1:
            matches.append((listing, lot_list[cands[0]], reason))
            taken.add(cands[0])
        else:
            # a tie that survived every key — assignment would be a guess
            unmatched.append((listing, "ambiguous"))

    # unique remainder: exactly one unmatched listing and one untaken lot
    free = [i for i in range(len(lot_list)) if i not in taken]
    if len(pending) == 1 and len(free) == 1:
        matches.append((pending[0], lot_list[free[0]], "remainder"))
    else:
        unmatched.extend((l, "none") for l in pending)

    return matches, unmatched


#: Below this Jaccard overlap with the portal's own text, a notice description
#: is withheld. Set at the boundary of the "different" band in
#: scripts/desc_divergence.py, because every listing under it in the live
#: corpus was wrong on review — two of the eight publish property in
#: Chhattisgarh and Bihar against Tamil Nadu listings, and seven of the eight
#: also carry an extraction score below 60. The band above it ("moderate",
#: 0.25-0.50, 86 listings) is mostly the notice supplying real detail the
#: portal's blurb omitted, which is the entire point of reading the notice —
#: so the cut-off sits here rather than higher.
MIN_DESCRIPTION_OVERLAP = 0.25

#: auction_ids a human confirmed, by reading the actual sale notice, sit on
#: the OTHER side of the overlap gate than the automatic script found: the
#: notice text is correct and it is the PORTAL's scraped text that is wrong
#: (bad scrape or a mismatched listing), so gating — or reverting — these on
#: overlap would replace a correct description with an incorrect one. The
#: overlap score cannot tell "our text is wrong" apart from "their text is
#: wrong"; only a human reading the source document can, which is exactly
#: what happened here (2026-08-26 review of the "very different" / "different"
#: tiers in scripts/desc_divergence.py's output).
#:
#: 840337 — notice: land in Durg, Chhattisgarh. Portal: a Chennai flat. The
#:          notice is this auction's actual property; the portal's own text
#:          does not even match its own title ("Muthailpet, Chennai" vs the
#:          George Town address it scraped).
#: 839880 — notice: land in Sheikhpura, Bihar. Portal: a Coimbatore building.
#:          Same shape as 840337.
DESCRIPTION_OVERLAP_REVIEWED_CORRECT = {"840337", "839880"}


def description_verdict(description: str, portal: str | None, *,
                        sole_claimant: bool) -> str | None:
    """Why this description must not be published, or None to publish it.

    Two independent ways a grounded description can be about the wrong
    property, so two gates:

    ``claimed_by_several`` — another listing on the same notice resolved to
    this same lot. One lot is one property, so at most one of them is it.

    ``diverges_from_portal`` — the text shares almost no wording with what the
    portal itself says about this listing. That catches what the first gate
    structurally cannot: a notice selling more lots than the portal scraped
    listings, where the one listing IS its lot's sole claimant and still gets
    handed the wrong lot. 837422 and 781964 are exactly that shape.

    A listing with no portal text is not gated on overlap: silence is not
    disagreement, and withholding there would strip descriptions from rows
    whose portal text was simply never scraped.
    """
    if not sole_claimant:
        return "claimed_by_several"
    if (portal or "").strip() and \
            description_overlap(portal, description) < MIN_DESCRIPTION_OVERLAP:
        return "diverges_from_portal"
    return None


def sole_claimants(matches: list[tuple[dict, dict, str]]) -> list[tuple[dict, dict, str]]:
    """The subset of ``matches`` whose lot no other listing also claims.

    One lot is one property, so two listings resolving to it cannot both be
    right. It happens when a notice yields more listings than the extraction
    found lots — a 12-lot PNB notice in the corpus carries 19 listings — and
    the surplus listings pile onto whichever lots they most resemble.

    Gates the ``resolved_lot_key`` write and, on a multi-lot notice, the
    description write. Both are lot-scoped claims a reader trusts: the lot key
    is what every agent3 tool reads as "this listing IS that lot", and the
    notice-grounded description reads as authoritative in a way the portal's
    own vague text does not. A value known to be wrong for at least one rival
    is worse than no value, so the rivals keep today's notice-scoped reading
    and stay on the human review queue.

    The field write deliberately does NOT go through this — it predates the
    lot key and narrowing it is a separate question.
    """
    claims: dict[int, int] = defaultdict(int)
    for _listing, lot, _reason in matches:
        claims[id(lot)] += 1
    return [m for m in matches if claims[id(m[1])] == 1]


# ── Neo4j I/O ────────────────────────────────────────────────────────────────

def fetch_work(limit: int | None = None) -> list[dict]:
    """Documents with a grounded extraction + their linked listings."""
    return run_read_query(
        "MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document) "
        "WHERE d.extraction_json IS NOT NULL "
        "RETURN d.filename AS filename, "
        "       d.extraction_json AS extraction_json, "
        "       d.extraction_corrections_json AS corrections_json, "
        "       collect({aid: a.auction_id, price: a.reserve_price_num, "
        "                emd: a.emd_num, "
        "                borrowers: [(a)-[:HAS_BORROWER]->(bo) | bo.name], "
        "                portal: a.website_description, "
        "                id_text: a.title + ' ' + coalesce(a.website_description, '')}) "
        "       AS listings "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else ""),
        max_rows=20_000, timeout=120.0)


def write_fields(rows: list[dict]) -> int:
    """SET += per-listing grounded fields (non-null only, built per row)."""
    if not rows:
        return 0
    written = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for batch in chunked(rows, WRITE_CHUNK):
        res = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            SET a += row.props,
                a.enrichment_source = 'grounded_extraction',
                a.grounded_source_file = row.filename,
                a.grounded_applied_at = datetime($at)
            RETURN a.auction_id AS aid
        """, {"rows": batch, "at": now_iso})
        written += len(res) if res else 0
    return written


def write_descriptions(rows: list[dict]) -> int:
    """LangExtract full_description is the sole automated description source:
    it overwrites everything, including the legacy description pipeline's
    human-verified rows (that pipeline is being scrapped; those texts are
    stashed once into description_human_backup). The one thing it never
    touches is description_source='reviewer' — a correction someone made
    after eyeballing the sale notice outranks any automated write.

    ``rows`` reaches here already filtered by ``sole_claimants`` on multi-lot
    notices (see ``run``), so a listing whose lot another listing also claims
    keeps its portal text rather than being handed a rival's schedule."""
    if not rows:
        return 0
    written = 0
    for batch in chunked(rows, WRITE_CHUNK):
        res = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            WHERE coalesce(a.description_source, '') <> 'reviewer'
            SET a.description_human_backup = CASE
                    WHEN a.description_source = 'human'
                         AND a.description_human_backup IS NULL
                    THEN a.description
                    ELSE a.description_human_backup END,
                a.description = row.desc,
                a.description_source = 'notice'
            RETURN a.auction_id AS aid
        """, {"rows": batch})
        written += len(res) if res else 0
    return written


def revert_withheld_descriptions(rows: list[dict]) -> int:
    """Put the portal's own text back on a listing whose notice description we
    have withdrawn.

    Withholding only stops the NEXT write; on its own it leaves whatever this
    pipeline published on an earlier run sitting in the graph, still labelled
    `description_source = 'notice'` and still reading as authoritative. A gate
    with no revert therefore fixes nothing already live — the 126 rows the two
    gates now withhold were all written before the gates existed.

    So a withheld listing is restored to `website_description` and relabelled
    `description_source = 'website'`, with the gate's own verdict kept on
    `description_withheld_reason` so the row stays queryable and a later run
    can tell a deliberate revert from a listing that never had a notice
    description at all.

    Two things are never touched. A reviewer's text (`'reviewer'`, and the
    legacy `'human'`) outranks every automated write, revert included. And a
    listing with no portal text is skipped rather than blanked: an empty
    description is its own kind of wrong, and the caller reports the residue
    instead of hiding it.
    """
    if not rows:
        return 0
    reverted = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for batch in chunked(rows, WRITE_CHUNK):
        res = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            WHERE a.description_source = 'notice'
              AND a.website_description IS NOT NULL
              AND trim(a.website_description) <> ''
            SET a.description = a.website_description,
                a.description_source = 'website',
                a.description_withheld_reason = row.reason,
                a.description_withheld_at = datetime($at)
            RETURN a.auction_id AS aid
        """, {"rows": batch, "at": now_iso})
        reverted += len(res) if res else 0
    return reverted


def human_decided_lot_matches() -> set[str]:
    """`auction_id`s whose lot-match decision was made by a person, not a
    rule — a reviewer who opened the notice and picked a lot outranks any
    automated matcher, this one included, so `write_lot_matches` must never
    touch them."""
    rows = run_read_query(
        """
        MATCH (r:ResolutionDecision {kind: 'lot-match', verdict: 'approved'})
        WHERE r.decided_by IS NOT NULL
          AND NOT r.decided_by STARTS WITH 'system:'
        RETURN r.payload_json AS payload_json
        """, max_rows=5000, timeout=30.0)
    out: set[str] = set()
    for r in rows:
        try:
            payload = json.loads(r.get("payload_json") or "{}")
        except (TypeError, ValueError):
            continue
        aid = payload.get("auction_id")
        if aid:
            out.add(aid)
    return out


def write_lot_matches(rows: list[dict]) -> int:
    """Persist `resolved_lot_key` from THIS pipeline's own listing<->lot
    match — `match_lots_to_listings()` already computes it (reserve price,
    then EMD, then borrower-name overlap, then survey/door identifiers, then
    unique-remainder pairing) to route field/description writes; recording
    it is what makes `api/agent3/common.py::scope_of()` and the agent3 tools
    actually see it as lot-scoped, not just this pipeline's own writes.

    Overwrites unconditionally against any prior AUTOMATED verdict —
    `pipeline/lot_resolution.py`'s reserve+borrower-only pass over the `Lot`
    graph is strictly less evidence than this pipeline's four-tier match run
    directly against the live extraction, so a disagreement means the older
    write was more likely wrong. Never touches a human's verdict (filtered
    out by the caller via `human_decided_lot_matches()`).

    A lot-key change deletes the superseded `ResolutionDecision` (its key
    embeds the lot_key, so a new pick is a new node) rather than leaving a
    now-wrong verdict sitting in the audit trail next to the current one.
    """
    if not rows:
        return 0
    written = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for batch in chunked(rows, WRITE_CHUNK):
        for row in batch:
            row["decision_key"] = lot_match_key(row["aid"], row["lot_key"])
            row["payload"] = json.dumps(
                {"auction_id": row["aid"], "lot_key": row["lot_key"],
                 "method": row["reason"]}, ensure_ascii=False)
        res = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            SET a.resolved_lot_key = row.lot_key,
                a.lot_resolved_at  = datetime($at)
            RETURN a.auction_id AS aid
        """, {"rows": batch, "at": now_iso})
        run_query("""
            UNWIND $rows AS row
            MATCH (old:ResolutionDecision {kind: 'lot-match'})
            WHERE old.key STARTS WITH ('lot-match:' + row.aid + '|')
              AND old.key <> row.decision_key
            DETACH DELETE old
        """, {"rows": batch})
        run_query("""
            UNWIND $rows AS row
            MERGE (r:ResolutionDecision {key: row.decision_key})
            SET r.kind = 'lot-match', r.verdict = 'approved',
                r.payload_json = row.payload, r.decided_at = datetime(),
                r.decided_by = 'system:apply_extractions'
        """, {"rows": batch})
        written += len(res) if res else 0
    return written


def clear_stale_lot_matches(rows: list[dict]) -> int:
    """Remove a `resolved_lot_key` this run did NOT re-derive.

    `write_lot_matches` only ever SET. A listing that stops resolving — its
    lot vanished from the extraction, or two listings now claim it and
    `sole_claimants` declined — kept its old key forever, and the key still
    RESOLVED, so nothing anywhere noticed.

    That is not hypothetical. 750335 held `CB17767669373793.jpg#2` after it
    stopped matching lot 2; a later run gave lot 2 to 750336, and the notice
    ended with two listings claiming one property — the exact outcome
    `sole_claimants` exists to prevent, reached by leaving a key behind rather
    than by writing a bad one.

    Only a key derived from THIS document is cleared (`lot_key` embeds the
    filename). 12 listings link to two documents, and a pass over one of them
    must not wipe a key the other legitimately wrote.

    Human decisions are filtered out by the caller and never reach here.
    """
    if not rows:
        return 0
    cleared = 0
    for batch in chunked(rows, WRITE_CHUNK):
        res = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            WHERE a.resolved_lot_key STARTS WITH (row.filename + '#')
            REMOVE a.resolved_lot_key, a.lot_resolved_at
            RETURN a.auction_id AS aid
        """, {"rows": batch})
        aids = [r["aid"] for r in (res or [])]
        cleared += len(aids)
        if aids:
            # The decision justified a value that no longer exists; leaving it
            # would let the review app's "Apply my decisions" put it straight
            # back. Automated verdicts only — a person's pick is never touched.
            run_query("""
                UNWIND $aids AS aid
                MATCH (r:ResolutionDecision {kind: 'lot-match'})
                WHERE r.key STARTS WITH ('lot-match:' + aid + '|')
                  AND r.decided_by STARTS WITH 'system:'
                DETACH DELETE r
            """, {"aids": aids})
    return cleared


# ── main ─────────────────────────────────────────────────────────────────────

def run(limit: int | None = None, dry_run: bool = False) -> int:
    work = fetch_work(limit)
    print(f"Documents with grounded extraction: {len(work)}")

    human_decided = human_decided_lot_matches()

    field_rows: list[dict] = []
    desc_rows: list[dict] = []
    revert_rows: list[dict] = []
    lot_key_rows: list[dict] = []
    stale_key_rows: list[dict] = []
    unmatched_out: list[dict] = []
    stats = defaultdict(int)
    human_skipped = 0

    for w in work:
        ents = entities_with_corrections(w["extraction_json"],
                                         w.get("corrections_json"))
        if not ents:
            stats["empty_extraction"] += 1
            continue
        lots = group_lots(ents)
        listings = [l for l in (w.get("listings") or []) if l.get("aid")]
        matches, unmatched = match_lots_to_listings(lots, listings)
        sole = {id(m[0]) for m in sole_claimants(matches)}
        # Every listing on this document that does NOT come out of this pass
        # with a lot key must not keep one from an earlier pass — see
        # clear_stale_lot_matches. Filled as the loop decides, subtracted at
        # the end.
        resolved_this_doc: set[str] = set()
        for listing, lot, reason in matches:
            stats[f"match_{reason}"] += 1
            if lot["fields"]:
                field_rows.append({"aid": listing["aid"],
                                   "filename": w["filename"],
                                   "props": lot["fields"]})
            # The description goes through `sole_claimants` for the same
            # reason `resolved_lot_key` does: when two listings claim one lot,
            # at most one of them is that property, so publishing the lot's
            # schedule on both states something false about at least one — and
            # a notice-grounded description reads as authoritative in a way the
            # portal's own vague text does not. Leaving the portal text in
            # place is the honest fallback.
            #
            # Reviewing the 26 rows scripts/desc_divergence.py flags turned
            # this from a theory into four counted cases: 794656, 811144,
            # 837423 and 837424 each carry a neighbouring lot's schedule while
            # `resolved_lot_key` is NULL, because this write was the one path
            # the gate did not cover.
            #
            # Single-lot notices are exempt, exactly as the lot-key write is:
            # every listing there legitimately claims the only lot, so
            # `sole_claimants` would drop all of them and strip descriptions
            # from the notices that are least ambiguous.
            if lot["description"]:
                verdict = description_verdict(lot["description"],
                                              listing.get("portal"),
                                              sole_claimant=(len(lots) == 1
                                                             or id(listing) in sole))
                # A human read the actual notice for this specific listing and
                # found the OVERLAP gate wrong about it (our text is correct;
                # the portal's is not) — that reviewed fact overrides the
                # overlap heuristic, but not the rivalry gate: if two listings
                # are still fighting over this lot, that conflict is real
                # regardless of what the text says.
                if (verdict == "diverges_from_portal"
                        and listing["aid"] in DESCRIPTION_OVERLAP_REVIEWED_CORRECT):
                    verdict = None
                if verdict is None:
                    desc_rows.append({"aid": listing["aid"],
                                      "desc": lot["description"]})
                else:
                    stats[f"description_dropped_{verdict}"] += 1
                    revert_rows.append({"aid": listing["aid"],
                                        "reason": verdict})
            # Only a genuinely multi-lot notice needs resolved_lot_key —
            # scope_of() already reads a single-lot notice as lot-scoped
            # without it, same as scripts/resolve_lots.py's own scope.
            if len(lots) > 1:
                if id(listing) not in sole:
                    stats["lot_key_dropped_claimed_by_several"] += 1
                elif listing["aid"] in human_decided:
                    human_skipped += 1
                else:
                    resolved_this_doc.add(listing["aid"])
                    lot_key_rows.append({
                        "aid": listing["aid"],
                        "lot_key": f"{w['filename']}#{lot['lot_index']}",
                        "reason": reason,
                    })
        for listing, reason in unmatched:
            stats[f"unmatched_{reason}"] += 1
            # No lot means no description this run. Anything an earlier run
            # published is now unbacked by a match, so it reverts as well.
            revert_rows.append({"aid": listing["aid"],
                                "reason": f"unmatched_{reason}"})
            unmatched_out.append({"aid": listing["aid"],
                                  "filename": w["filename"],
                                  "price": listing.get("price"),
                                  "reason": reason,
                                  "lot_reserves": [lo["reserve"]
                                                   for lo in lots.values()]})

        # Whatever this document did not resolve this pass must not keep a key
        # from a previous one. A human's pick is exempt: it outranks the rule.
        for listing in listings:
            aid = listing["aid"]
            if aid not in resolved_this_doc and aid not in human_decided:
                stale_key_rows.append({"aid": aid, "filename": w["filename"]})

    print(f"  match/unmatch stats: {dict(stats)}")
    print(f"  field rows: {len(field_rows)}  description rows: {len(desc_rows)}  "
          f"lot-key rows: {len(lot_key_rows)}  "
          f"stale keys to clear: {len(stale_key_rows)}  "
          f"descriptions to revert: {len(revert_rows)}  "
          f"skipped (human-decided): {human_skipped}  "
          f"unmatched: {len(unmatched_out)}")

    if unmatched_out and not dry_run:
        UNMATCHED_CSV.parent.mkdir(parents=True, exist_ok=True)
        with UNMATCHED_CSV.open("w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow(["auction_id", "file", "listing_price", "reason",
                         "lot_reserves"])
            for u in unmatched_out:
                wr.writerow([u["aid"], u["filename"], u["price"], u["reason"],
                             ";".join(str(r) for r in u["lot_reserves"])])
        print(f"  unmatched logged to {UNMATCHED_CSV}")

    if dry_run:
        print("  DRY RUN — nothing written")
        return 0

    nf = write_fields(field_rows)
    nd = write_descriptions(desc_rows)
    nl = write_lot_matches(lot_key_rows)
    # After the write, so a listing that moved from one lot to another this run
    # is not cleared by its own new key's document pass.
    nc = clear_stale_lot_matches(stale_key_rows)
    # Revert last: a listing can only appear in one of desc_rows/revert_rows
    # per run, but ordering it after the write keeps the invariant obvious —
    # nothing this run published can then be reverted by it.
    nr = revert_withheld_descriptions(revert_rows)
    print(f"  wrote fields to {nf} listings, descriptions to {nd} listings "
          f"(legacy human descriptions overwritten, backed up once), "
          f"lot key to {nl} listings, cleared {nc} stale lot key(s), "
          f"reverted {nr} listings to portal text")
    if nr < len(revert_rows):
        print(f"  NOTE: {len(revert_rows) - nr} withheld listings kept their old "
              f"description — no portal text to fall back on, or already "
              f"reverted / reviewer-owned")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N Documents")
    ap.add_argument("--dry-run", action="store_true",
                    help="report matches/fields without writing to Neo4j")
    args = ap.parse_args()
    return run(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
