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
  4. Write fields onto AuctionProperty. On a multi-lot notice a listing whose
     lot is NOT confirmed (a rival claim, or unmatched) gets only the fields
     every lot agrees on — notice-facts, true whichever lot it is — and any
     contested field an earlier run wrote onto it is cleared
     (consensus_and_contested / clear_unsafe_fields). The description write
     treats the
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
from collections import Counter, defaultdict
from datetime import datetime, timezone

from api.neo4j_client import run_query, run_read_query
from pipeline.obs import get_logger
from pipeline.area_agreement import check_match as check_area_match
from pipeline.price_agreement import check_document
from pipeline.property_taxonomy import (
    asset_category, classify_portal_type, classify_lot_type,
    classify_property_type, conflict_severity, effective_bucket,
)
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
            "portal_aid": None,
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

        # The listing this lot says it is (langextract_examples.
        # portal_roster_block). Read off ANY entity rather than only
        # `property`: the model stamps it where the guide says to, but an
        # attribute this cheap to accept and this heavily verified downstream
        # is not worth losing to a misplacement. First non-null wins, as
        # everywhere else here. It stays OUT of `fields` — nothing writes it
        # to the graph; `match_lots_to_listings` is its only reader.
        if rec["portal_aid"] is None:
            claim = attrs.get("portal_aid")
            if claim is not None and str(claim).strip():
                rec["portal_aid"] = str(claim).strip()

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
        description = "\n\n".join(rec["description_parts"]) or None
        # The type is re-derived here, not where the `property` entity was
        # read, because entity order is not guaranteed: the schedule spans
        # may arrive after it, and the correction needs the whole assembled
        # description. `classify_lot_type` only ever upgrades bare ground (or
        # UNKNOWN) to a unit the schedule names outright, so a lot whose
        # schedule names nothing keeps exactly the bucket it had.
        if f.get("property_type_raw"):
            corrected = classify_lot_type(f["property_type_raw"], description)
            if corrected != f.get("property_type_norm"):
                f["property_type_norm"] = corrected
                f["asset_category_norm"] = asset_category(
                    corrected, f["property_type_raw"])
        out[li] = {
            "lot_index": li,
            "description": description,
            "fields": f,
            "reserve": rec["reserve"],
            "emd": rec["emd"],
            "portal_aid": rec["portal_aid"],
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


def _claim_contradicted(lot: dict, listing: dict) -> bool:
    """Whether the portal's own money says this lot is NOT this listing.

    The single check that stops a confident wrong claim: both sides carry a
    reserve price, independently — the portal parsed it from a structured
    field, the extraction read it off the notice — and they disagree by more
    than the ±1% every other price comparison here allows. Only a positive
    disagreement counts. A lot whose price the extraction missed, or a listing
    the portal never priced, contradicts nothing; it is simply unverified, and
    the caller's other keys decide what that is worth.
    """
    lot_reserve, price = lot.get("reserve"), listing.get("price")
    if lot_reserve is None or price is None:
        return False
    return abs(lot_reserve - price) > abs(price) * PRICE_TOLERANCE_PCT / 100.0


def match_lots_to_listings(lots: dict[str, dict],
                           listings: list[dict]) -> tuple[list[tuple[dict, dict, str]],
                                                          list[tuple[dict, str]]]:
    """Assign each listing to at most one lot.

    listings: [{aid, price, emd?, borrowers?, id_text?}]. Returns (matches,
    unmatched) where matches is [(listing, lot, reason)] and unmatched is
    [(listing, reason)]. reason ∈ 'single' | 'exact' | 'tolerance' | 'emd' |
    'emd_tolerance' | 'borrower' | 'identifier' | 'portal_aid' | 'remainder' |
    'ambiguous' | 'portal_aid_conflict' | 'none'.

    Keys narrow in order of trustworthiness: reserve price exact/±1%, then
    EMD exact/±1% (rescues listings the portal shows without a price, and 10x
    price typos), then borrower-name overlap, then survey/door identifiers
    found in the listing's own text (id_text: title + portal description).
    Borrower and identifiers are what separate lots that tie on money — EMD
    cannot, being 10% of the reserve almost everywhere. Every key must reduce
    to exactly one lot; a tie that survives all keys stays 'ambiguous' rather
    than being guessed, and a listing none of whose keys hit anything falls
    through to the unique-remainder rule.

    **The lot's own claim (`portal_aid`) is checked, never obeyed.** The
    extraction prompt shows the model this notice's portal rows by id and asks
    each lot to name the row it is (langextract_examples.portal_roster_block),
    which is the one signal generated while reading both sides at once —
    exactly the case the keys above cannot decide, sibling flats that tie on
    money. But it is a model's assertion about external data it cannot quote,
    so it is admitted only where the keys have nothing to say against it:

      * keys reduced to one lot AND it is the claimed one -> unchanged, the
        key's own reason stands (the claim merely agreed);
      * keys reduced to one lot AND it is a DIFFERENT one -> 'portal_aid_
        conflict', unmatched. Two independent signals disagree about which
        property this is; writing either would be a coin toss with a
        description and a village on the end of it, so a human decides;
      * keys left several candidates (or found nothing) and the claim is among
        them -> 'portal_aid', the claim breaks the tie;
      * keys left several candidates and the claim is not among them -> also
        'portal_aid_conflict': a claim excluded by the evidence is a wrong
        claim, not a tiebreak.

    A claim two lots make, or one naming a listing not on this notice, is
    dropped before any of that — the model is confused, and today's behaviour
    is the safe fallback.
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

    # {aid: lot index} for every claim exactly one lot makes. A duplicated
    # claim is dropped outright rather than resolved: one listing is one lot,
    # so a model that stamped the same id twice has told us nothing about
    # either — and silently keeping the first would make the answer depend on
    # entity order.
    claim_counts: Counter = Counter(
        lo["portal_aid"] for lo in lot_list if lo.get("portal_aid"))
    claimed_by: dict[str, int] = {
        lo["portal_aid"]: i for i, lo in enumerate(lot_list)
        if lo.get("portal_aid") and claim_counts[lo["portal_aid"]] == 1}

    for listing in listings:
        claim_idx = claimed_by.get(str(listing.get("aid")))
        price = listing.get("price")
        emd = listing.get("emd")
        borrowers = set()
        for name in listing.get("borrowers") or []:
            borrowers |= _name_tokens(name)
        if price is None and emd is None and not borrowers:
            # Nothing to match on — unless a lot named this listing. The claim
            # is unverifiable here (there is no portal figure to check it
            # against), but it is also uncontradicted, and the alternative is
            # the listing staying unlinked for good — this branch exists
            # because the portal does publish listings with no price, no EMD
            # and no borrower at all.
            if claim_idx is not None:
                matches.append((listing, lot_list[claim_idx], "portal_aid"))
                taken.add(claim_idx)
            else:
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

        if claim_idx is not None:
            # Verify the lot's own claim against the keys just computed. The
            # order matters: the direct price check first, because when NO lot
            # matched the listing's price every lot is still a candidate and
            # the claim would otherwise sail through unexamined.
            if (_claim_contradicted(lot_list[claim_idx], listing)
                    or claim_idx not in cands):
                unmatched.append((listing, "portal_aid_conflict"))
                continue
            if reason is None or len(cands) > 1:
                cands = [claim_idx]
                reason = "portal_aid"
            # else the keys already chose this same lot: their reason stands,
            # having been reached without the claim's help.

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


def consensus_and_contested(lots: dict[str, dict]) -> tuple[dict, set[str]]:
    """Split a notice's field values into what is safe for ANY of its listings
    and what is only safe for a listing whose lot is known.

    A value every lot agrees on — same non-null value on all of them — is a
    fact about the notice: it holds for this listing whichever lot it turns
    out to be, so it is safe to write even when the lot match is refused.
    A value the lots differ on (or that only some lots carry) names ONE lot,
    so writing it to an unresolved listing asserts a match `sole_claimants`
    declined to make. 63 unlinked listings live today carry a village copied
    that way from notices that span more than one place — stated with full
    confidence while the lot link itself says "unknown".

    Returns ``(consensus, contested)``: the fields to write for an unresolved
    listing, and the keys that must be CLEARED off one (they could only have
    come from a specific lot). A single-lot notice has no contested keys by
    construction — its one lot's fields are notice-facts.
    """
    lot_list = list(lots.values())
    if not lot_list:
        return {}, set()
    all_keys: set[str] = set()
    for lo in lot_list:
        all_keys |= set(lo.get("fields") or {})
    consensus: dict = {}
    for k in all_keys:
        vals = [(lo.get("fields") or {}).get(k) for lo in lot_list]
        if all(v is not None for v in vals) and len({str(v) for v in vals}) == 1:
            consensus[k] = vals[0]
    return consensus, all_keys - set(consensus)


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

    The field write goes through a finer version of the same idea:
    `consensus_and_contested` keeps a rival listing's fields only where every
    lot on the notice agrees on the value — those are notice-facts, safe for
    whichever lot the listing turns out to be — and gates (and clears) the
    rest. See `clear_unsafe_fields`.
    """
    claims: dict[int, int] = defaultdict(int)
    for _listing, lot, _reason in matches:
        claims[id(lot)] += 1
    return [m for m in matches if claims[id(m[1])] == 1]


# ── explain: the writer's own verdict, read-only ─────────────────────────────

#: One line per matching tier, phrased for a reviewer rather than for a log.
#: Keyed by every `reason` string `match_lots_to_listings` can return — a test
#: holds the two lists together, because a tier with no prose here reaches a
#: reviewer as a bare token like "emd_tolerance". The `rival` outcome has no
#: entry: it is not a tier, and its sentence is built around the tier it
#: overrides plus the rivals' auction_ids.
_EXPLAIN_TEXT = {
    "single": "the notice has one lot, so this listing is it",
    "exact": "reserve price matches this lot exactly",
    "tolerance": "reserve price matches this lot within 1%",
    "emd": "EMD matches this lot exactly (reserve price did not decide)",
    "emd_tolerance": "EMD matches this lot within 1% (reserve price did not decide)",
    "borrower": "borrower name matches this lot (money alone tied)",
    "identifier": "a survey/door number in the listing names only this lot",
    "portal_aid": "the extraction read this lot as this listing, and nothing "
                  "in the portal's own figures contradicts it",
    "remainder": "the last unplaced listing and the last free lot",
    "ambiguous": "several lots tie on every signal — reserve price, EMD, "
                 "borrower name and identifiers all fail to separate them",
    "portal_aid_conflict": "the extraction read this listing as one lot but "
                           "the portal's own reserve price points elsewhere — "
                           "two independent signals disagree about which "
                           "property this is",
    "none": "no signal on this listing hit any lot on the notice",
    "no_listing_price": "the listing has no reserve price, EMD or borrower "
                        "name to match on",
    "no_lots": "the notice extraction produced no lots",
}


def explain_lot_match(lots: dict[str, dict],
                      listings: list[dict]) -> dict[str, dict]:
    """Why each listing did or did not get its lot — the WRITER's verdict.

    Pure, read-only, and deliberately not a second matcher: it calls the same
    `match_lots_to_listings` and `sole_claimants` that `run()` uses to write
    the `IS_LOT` edge, then reports what they decided. Anything that reasons
    about lot matching from its own rules will drift from what actually gets
    written — `scripts/resolve_lots.py`'s retired auto-resolver did, and
    `pipeline/lot_resolution.py` still does: it weighs reserve price and
    borrower name only, off a graph copy of the extraction, and knows nothing
    of the EMD tier, the identifier tier, or the rivalry gate. A reviewer told
    "ambiguous" by that rule is not being told the truth when the real blocker
    was another listing holding the lot.

    Returns {auction_id: {outcome, tier, lot_index, rivals, reason}} where

      outcome  'linked'    the writer would write this edge
               'rival'     matched, but `sole_claimants` refuses — another
                           listing on the notice claims the same lot
               'unmatched' the matcher could not place it
      tier     the matching tier reached ('exact', 'borrower', …) or None
      lot_index the lot it matched, or None; combine with the document's
               filename for a lot_key ("<filename>#<lot_index>")
      rivals   the other listings claiming that same lot (empty unless 'rival')
      reason   one line of prose for the review UI
    """
    matches, unmatched = match_lots_to_listings(lots, listings)
    sole = {id(m[0]) for m in sole_claimants(matches)}

    # Who else landed on each lot, by auction_id — this is the fact the queue
    # could not show before, and the one a reviewer needs most: the rival is
    # usually the row they should be comparing against.
    claimants: dict[int, list[str]] = defaultdict(list)
    for listing, lot, _reason in matches:
        claimants[id(lot)].append(listing["aid"])

    out: dict[str, dict] = {}
    for listing, lot, reason in matches:
        rivals = [a for a in claimants[id(lot)] if a != listing["aid"]]
        linked = id(listing) in sole
        out[listing["aid"]] = {
            "outcome": "linked" if linked else "rival",
            "tier": reason,
            "lot_index": lot["lot_index"],
            "rivals": rivals,
            "reason": _EXPLAIN_TEXT.get(reason, reason) if linked else
                      (f"matched lot {lot['lot_index']} "
                       f"({_EXPLAIN_TEXT.get(reason, reason)}), but "
                       f"{'listing' if len(rivals) == 1 else 'listings'} "
                       f"{', '.join(rivals)} matched it too — one lot is one "
                       f"property, so this needs a human, not a guess"),
        }
    for listing, reason in unmatched:
        out[listing["aid"]] = {
            "outcome": "unmatched",
            "tier": None,
            "lot_index": None,
            "rivals": [],
            "reason": _EXPLAIN_TEXT.get(reason, reason),
        }
    return out


# ── Neo4j I/O ────────────────────────────────────────────────────────────────

def fetch_work(limit: int | None = None,
               filenames: list[str] | None = None) -> list[dict]:
    """Documents with a grounded extraction + their linked listings.

    ``filenames`` narrows to specific notices. The review queue passes it so
    `explain_documents` can run this same matcher over the page of listings a
    reviewer is looking at, instead of the whole 1,610-document corpus.
    """
    return run_read_query(
        "MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document) "
        "WHERE d.extraction_json IS NOT NULL "
        + ("AND d.filename IN $filenames " if filenames is not None else "")
        + "RETURN d.filename AS filename, "
        "       d.extraction_json AS extraction_json, "
        "       d.extraction_corrections_json AS corrections_json, "
        "       collect({aid: a.auction_id, price: a.reserve_price_num, "
        "                emd: a.emd_num, area_raw: a.total_area, "
        "                borrowers: [(a)-[:HAS_BORROWER]->(bo) | bo.name], "
        "                portal: a.website_description, "
        "                id_text: a.title + ' ' + coalesce(a.website_description, '')}) "
        "       AS listings "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else ""),
        {"filenames": filenames} if filenames is not None else None,
        max_rows=20_000, timeout=120.0)


def fetch_portal_types() -> dict[str, str]:
    """{auction_id: the portal's :PropertyType name}.

    Read from the edge, not from `AuctionProperty.portal_property_type`: that
    property is a copy `scripts/backfill_property_type.py` writes, and reading
    a copy to judge a copy is how the conflict flag drifted in the first
    place. A handful of listings carry two edges; the alphabetically first
    name is taken so repeated runs agree with each other — the same rule the
    backfill uses.
    """
    rows = run_read_query(
        "MATCH (a:AuctionProperty)-[:HAS_PROPERTY_TYPE]->(t:PropertyType) "
        "RETURN a.auction_id AS aid, t.name AS name",
        max_rows=50_000, timeout=120.0)
    out: dict[str, str] = {}
    for r in rows:
        aid, name = r.get("aid"), r.get("name")
        if aid and (aid not in out or (name or "") < out[aid]):
            out[aid] = name
    return out


def fetch_headline_sqft() -> dict[str, float]:
    """Each lot's headline extent in sq.ft, by lot_key — the figure agent3's
    property block serves. The area comparer reads it because that, not the
    extraction's own field text, is the second number a user actually sees."""
    rows = run_read_query(
        "MATCH (l:Lot)-[e:HAS_EXTENT]->(m:Measurement) "
        "WHERE e.is_headline AND m.sqft_norm IS NOT NULL "
        "RETURN l.lot_key AS lot_key, toFloat(m.sqft_norm) AS sqft",
        max_rows=20_000, timeout=60.0)
    return {r["lot_key"]: r["sqft"] for r in rows}


def explain_documents(filenames: list[str]) -> dict[tuple[str, str], dict]:
    """`explain_lot_match` over named notices, off the live extraction.

    The extraction JSON, not the `:Lot` graph copy of it, for the same reason
    `run()` reads it: promote_extractions may not have run since the last
    re-extraction, and a queue explaining a match off stale nodes explains a
    match the writer is not making.

    Keyed by (auction_id, filename), not auction_id alone: 12 listings link to
    two scans of the same notice, and each scan is extracted separately, so
    they get one verdict per scan. The caller already picked which scan it is
    showing and looks the verdict up under that pair. Listings on documents
    with no extraction are simply absent — the caller decides what to say.
    """
    if not filenames:
        return {}
    out: dict[tuple[str, str], dict] = {}
    for w in fetch_work(filenames=sorted(set(filenames))):
        ents = entities_with_corrections(w["extraction_json"],
                                         w.get("corrections_json"))
        if not ents:
            continue
        listings = [l for l in (w.get("listings") or []) if l.get("aid")]
        for aid, verdict in explain_lot_match(group_lots(ents), listings).items():
            out[(aid, w["filename"])] = verdict
    return out


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


def clear_unsafe_fields(rows: list[dict]) -> int:
    """REMOVE contested notice fields from listings whose lot is unresolved.

    `write_fields` used to hand every matched listing its lot's full field
    set, rival or not — so a listing the rivalry gate refused to link still
    carries a specific lot's village, taluk or extent from an earlier run,
    stated as plain fact. The new consensus write stops adding those; this
    clears the ones already on the graph.

    Only a listing whose `grounded_source_file` is THIS document is touched —
    the same provenance rule `clear_stale_lot_matches` uses — so a value some
    other pipeline (or the other scan of a dual-document listing) wrote is
    never stripped by a pass over the wrong file. Consensus keys are not in
    ``rows`` at all: they were true for the listing whichever lot it is.

    Cypher has no dynamic REMOVE, so rows are grouped by their exact key set
    and one literal REMOVE clause is built per group. Keys come from
    `group_lots`' own field names, backtick-quoted anyway out of caution.
    """
    if not rows:
        return 0
    by_keys: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for row in rows:
        by_keys[tuple(row["keys"])].append(row)
    cleared = 0
    for keys, group in by_keys.items():
        remove_clause = ", ".join(f"a.`{k}`" for k in keys)
        exists_clause = " OR ".join(f"a.`{k}` IS NOT NULL" for k in keys)
        for batch in chunked(group, WRITE_CHUNK):
            res = run_query(f"""
                UNWIND $rows AS row
                MATCH (a:AuctionProperty {{auction_id: row.aid}})
                WHERE a.grounded_source_file = row.filename
                  AND ({exists_clause})
                REMOVE {remove_clause}
                RETURN a.auction_id AS aid
            """, {"rows": batch})
            cleared += len(res) if res else 0
    return cleared


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
    """Link a listing to its lot with (:AuctionProperty)-[:IS_LOT]->(:Lot).

    `match_lots_to_listings()` already computes the pairing (reserve price,
    then EMD, then borrower-name overlap, then survey/door identifiers, then
    unique-remainder) to route field/description writes; recording it is what
    makes `api/agent3/common.py::scope_of()` and the agent3 tools see the
    listing as lot-scoped, not just this pipeline's own writes.

    Phase 4: this used to SET a `resolved_lot_key` string of the form
    "<filename>#<lot_index>". lot_index is the extraction model's own
    numbering, so a re-extraction renumbered the lots and every stored key
    silently began naming a different property. The edge names the node.

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
        # Phase 4: the edge IS the resolution. MATCH on the :Lot rather than
        # writing its key onto the listing — a key is only a way to find a
        # node, and lot_index is the extraction model's own numbering, so a
        # re-extraction made every stored key a guess.
        #
        # MATCH, not MERGE, on the lot: a listing whose lot does not exist
        # yet must come back as unwritten rather than conjuring an empty
        # :Lot. Rows that miss are counted by the caller, not swallowed.
        res = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            MATCH (l:Lot {lot_key: row.lot_key})
            MERGE (a)-[r:IS_LOT]->(l)
              ON CREATE SET r.linked_at = datetime($at)
            SET r.method = row.reason
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
    missing = len(rows) - written
    if missing:
        # Not a silent loss: after Phase 4 the edge IS the resolution, so a
        # row whose :Lot does not exist yet is a listing left UNRESOLVED, not
        # merely a key that fails to dereference later. promote_extractions
        # must have run for the document before this can link it.
        print(f"  NOTE: {missing} match(es) had no :Lot to link to — "
              f"run pipeline.promote_extractions for those documents, then "
              f"this step again", flush=True)
    return written


def clear_stale_lot_matches(rows: list[dict]) -> int:
    """Drop an :IS_LOT edge this run did NOT re-derive.

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
        # Only an edge to a lot on THIS document. 12 listings link to two
        # notices, and a pass over one must not drop the edge the other
        # legitimately made — the filename guard the key version carried,
        # expressed against the lot instead of against a string prefix.
        res = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})-[r:IS_LOT]->(l:Lot)
            WHERE l.lot_key STARTS WITH (row.filename + '#')
            DELETE r
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

def write_type_conflicts(rows: list[dict]) -> int:
    """Rebuild the portal/notice property-type verdict on every listing.

    This flag existed before — `scripts/backfill_property_type.py` wrote it —
    but nothing kept it in step: `run()` rewrites `property_type_norm` every
    pass and never touched the flag beside it, so the verdict aged against the
    value it judges. Live, that left 242 listings flagged over a plot/land
    wording difference and 27 flagged clean while genuinely conflicting.

    Written for AGREEMENT too, not only for conflicts: `false` is a real
    verdict ("these two were compared and match"), and writing only the
    conflicts is what let stale `false`s survive. The severity is cleared on
    an agreeing row so a downgraded verdict cannot leave its old severity
    behind.
    """
    if not rows:
        return 0
    written = 0
    for batch in chunked(rows, WRITE_CHUNK):
        out = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            SET a.property_type_conflict = row.conflict,
                a.property_type_conflict_severity = row.severity,
                a.property_type_effective = row.effective,
                a.portal_property_type = row.portal,
                a.property_type_conflict_at = datetime()
            RETURN count(a) AS n
        """, {"rows": batch})
        written += (out[0].get("n") or 0) if out else 0
    return written


def write_area_findings(rows: list[dict]) -> int:
    """Record every area disagreement on its listing, and clear the rest.

    Same rebuild-each-pass contract as `write_price_findings`: a finding is a
    statement about the current extraction, so one whose extent has since
    been corrected must disappear. Verdicts are recorded, never auto-applied
    (2026-08-31 plan decision: queue first, decide from the scorecard) —
    `total_area` itself is not touched here.
    """
    run_query("""
        MATCH (a:AuctionProperty) WHERE a.area_agreement IS NOT NULL
        REMOVE a.area_agreement, a.area_agreement_ratio,
               a.area_agreement_severity, a.area_agreement_listing_sqft,
               a.area_agreement_notice_sqft, a.area_agreement_at
        RETURN count(a) AS cleared
    """, {})
    written = 0
    for batch in chunked(rows, WRITE_CHUNK):
        out = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            SET a.area_agreement = row.verdict,
                a.area_agreement_ratio = row.ratio,
                a.area_agreement_severity = row.severity,
                a.area_agreement_listing_sqft = row.listing_sqft,
                a.area_agreement_notice_sqft = row.notice_sqft,
                a.area_agreement_at = datetime()
            RETURN count(a) AS n
        """, {"rows": batch})
        written += (out[0].get("n") or 0) if out else 0
    return written


def write_price_findings(rows: list[dict]) -> int:
    """Record every price disagreement on its listing, and clear the rest.

    The flag is REBUILT each pass rather than merged: a finding is a statement
    about the current extraction, so one whose price has since been corrected
    must disappear. Clearing first and writing second means a listing never
    carries a verdict the present data does not support.

    Written onto :AuctionProperty rather than a node of its own because it is
    one small fact about one listing, and every reader of it already has the
    listing in hand.
    """
    run_query("""
        MATCH (a:AuctionProperty) WHERE a.price_agreement IS NOT NULL
        REMOVE a.price_agreement, a.price_agreement_ratio,
               a.price_agreement_severity, a.price_agreement_at
        RETURN count(a) AS cleared
    """, {})
    written = 0
    for batch in chunked(rows, WRITE_CHUNK):
        out = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            SET a.price_agreement = row.verdict,
                a.price_agreement_ratio = row.ratio,
                a.price_agreement_severity = row.severity,
                a.price_agreement_at = datetime()
            RETURN count(a) AS n
        """, {"rows": batch})
        written += (out[0].get("n") or 0) if out else 0
    return written


def run(limit: int | None = None, dry_run: bool = False) -> int:
    work = fetch_work(limit)
    print(f"Documents with grounded extraction: {len(work)}")

    human_decided = human_decided_lot_matches()
    headline_sqft = fetch_headline_sqft()
    portal_types = fetch_portal_types()

    field_rows: list[dict] = []
    unsafe_field_rows: list[dict] = []
    area_rows: list[dict] = []
    type_rows: list[dict] = []
    desc_rows: list[dict] = []
    revert_rows: list[dict] = []
    lot_key_rows: list[dict] = []
    stale_key_rows: list[dict] = []
    price_rows: list[dict] = []
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
        # Two independently scraped prices for the same property; nothing
        # compared them until now. See pipeline.price_agreement.
        for finding in check_document(matches):
            stats[f"price_{finding['verdict']}"] += 1
            price_rows.append(dict(finding, filename=w["filename"]))
        # Fields split the same way the lot key and description do, but by
        # VALUE rather than wholesale: what every lot agrees on is a
        # notice-fact and stays writable for anyone; what the lots differ on
        # is only writable for a listing whose lot is confirmed. This closes
        # the one write path the rivalry gate did not cover — a contested
        # listing used to get its guessed lot's village/extent stated as
        # plain fact while the lot link itself was refused.
        consensus, contested = consensus_and_contested(lots)
        for listing, lot, reason in matches:
            stats[f"match_{reason}"] += 1
            safe_fields = lot["fields"] if id(listing) in sole else consensus
            if safe_fields:
                field_rows.append({"aid": listing["aid"],
                                   "filename": w["filename"],
                                   "props": safe_fields})
            # The portal/notice type disagreement, rebuilt from the type this
            # run is ACTUALLY writing. `apply_extractions` has always written
            # property_type_norm and never the flag beside it, so the flag
            # kept whatever the backfill last computed and drifted: 27 live
            # listings say "no conflict" while genuinely conflicting, 6 of
            # them flats filed under Plot. Reading `safe_fields` rather than
            # the lot means an unresolved listing is judged on the consensus
            # type it receives, not on a lot it may not be.
            portal_name = portal_types.get(listing["aid"])
            notice_bucket = (safe_fields or {}).get("property_type_norm")
            # One side is enough. A conflict needs both, but
            # `property_type_effective` — what search reads — must be rewritten
            # whenever EITHER side is known, or a listing with a notice type
            # and no portal type would keep whatever the last backfill left
            # while this run rewrites the notice value underneath it. That is
            # the same drift, one field over.
            if portal_name or notice_bucket:
                portal_bucket = classify_portal_type(portal_name)
                compared = bool(portal_name and notice_bucket)
                sev = (conflict_severity(notice_bucket, portal_bucket)
                       if compared else None)
                type_rows.append({"aid": listing["aid"],
                                  # null, not false, when only one side named
                                  # a type: `false` is the positive claim
                                  # "compared, and they agree", and letting a
                                  # gap wear it is exactly what let 27 stale
                                  # `false`s sit on genuinely conflicting
                                  # listings.
                                  "conflict": sev is not None if compared
                                  else None,
                                  "severity": sev,
                                  "portal": portal_name,
                                  "notice": notice_bucket,
                                  # What search resolves this listing to: the
                                  # notice, or the portal where no notice type
                                  # exists. Computed here, where the taxonomy
                                  # lives, so Cypher never re-implements it.
                                  "effective": effective_bucket(
                                      notice_bucket, portal_bucket)})
                if sev:
                    stats[f"type_conflict_{sev}"] += 1
            if id(listing) not in sole and contested:
                stats["fields_gated_claimed_by_several"] += 1
                unsafe_field_rows.append({"aid": listing["aid"],
                                          "filename": w["filename"],
                                          "keys": sorted(contested)})
            # Area agreement runs only on confirmed pairs: on a contested lot
            # the notice side is a guess, and a finding built on a guess sends
            # a reviewer to reconcile numbers from two different properties.
            if id(listing) in sole:
                area_finding = check_area_match(
                    listing, lot,
                    headline_sqft.get(f"{w['filename']}#{lot['lot_index']}"))
                if area_finding:
                    stats[f"area_{area_finding['verdict']}"] += 1
                    area_rows.append(dict(area_finding,
                                          filename=w["filename"]))
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
            # Single-lot notices are linked too. They used to be skipped —
            # scope_of() reads them as lot-scoped without an edge, so the link
            # added nothing. After Phase 4 the edge is the ONLY statement that
            # a listing is a given lot, and leaving 1,009 properties without
            # one means the graph cannot answer "which lot is this?" for a
            # third of the corpus even where the answer is unambiguous.
            #
            # No special case is needed: `sole_claimants` already decides.
            # 990 single-lot notices carry exactly one listing, so nothing
            # rivals them; the two that carry 2 and 17 are genuinely contested
            # (one lot cannot be all of them) and are dropped, same as any
            # multi-lot fight.
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
            # An unmatched listing may hold contested fields from an EARLIER
            # run, before this gate existed — clear those too. Its consensus
            # keys survive untouched: they were true regardless of lot.
            if contested:
                unsafe_field_rows.append({"aid": listing["aid"],
                                          "filename": w["filename"],
                                          "keys": sorted(contested)})
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
    print(f"  field rows: {len(field_rows)}  "
          f"contested-field clears: {len(unsafe_field_rows)}  "
          f"description rows: {len(desc_rows)}  "
          f"lot-key rows: {len(lot_key_rows)}  "
          f"stale keys to clear: {len(stale_key_rows)}  "
          f"descriptions to revert: {len(revert_rows)}  "
          f"skipped (human-decided): {human_skipped}  "
          f"price disagreements: {len(price_rows)}  "
          f"area disagreements: {len(area_rows)}  "
          f"type verdicts: {len(type_rows)}  "
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

    npf = write_price_findings(price_rows)
    naf = write_area_findings(area_rows)
    ntf = write_type_conflicts(type_rows)
    nf = write_fields(field_rows)
    # After the field write: a rival listing gets its consensus fields SET and
    # its contested keys REMOVEd in the same run — disjoint key sets by
    # construction, but this order keeps the invariant obvious.
    ncf = clear_unsafe_fields(unsafe_field_rows)
    nd = write_descriptions(desc_rows)
    nl = write_lot_matches(lot_key_rows)
    # After the write, so a listing that moved from one lot to another this run
    # is not cleared by its own new key's document pass.
    nc = clear_stale_lot_matches(stale_key_rows)
    # Revert last: a listing can only appear in one of desc_rows/revert_rows
    # per run, but ordering it after the write keeps the invariant obvious —
    # nothing this run published can then be reverted by it.
    nr = revert_withheld_descriptions(revert_rows)
    print(f"  wrote fields to {nf} listings, "
          f"cleared contested fields off {ncf}, descriptions to {nd} listings "
          f"(legacy human descriptions overwritten, backed up once), "
          f"lot key to {nl} listings, cleared {nc} stale lot key(s), "
          f"reverted {nr} listings to portal text, "
          f"flagged {npf} price and {naf} area disagreement(s), "
          f"{ntf} property-type verdict(s)")
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
