"""Phase B/C — promote Document.extraction_json into the unified graph.

    python -m pipeline.promote_extractions [--limit N] [--filename F]
                                           [--dry-run] [--skip-parcels]
                                           [--workers N] [--places-only]

Phase B promotes documents concurrently (--workers, default 8) — each document
is a few small writes dominated by the round trip to Aura, so a serial run over
the full corpus is hours of waiting on the network. Phase C stays serial: it is
a whole-corpus pass by design.

If Bolt (port 7687) is blocked in your environment — Claude Code on the web,
or any HTTP-only egress proxy — prefix with NEO4J_HTTP_API=1 to route through
Aura's HTTPS Query API instead.

Phase B turns each extracted notice into graph structure: one :Lot per lot,
with its identifiers, extents, boundaries, schedules, parties, sale event and
notice-level nodes. Each lot's village/taluk/district is resolved onto the
official gazetteer (:RevenueVillage / :Taluk / :District) through
pipeline.place_resolution — the notice's own spelling is never stored as the
answer, and a lot that cannot be placed keeps `place_status` and no edge.

Phase C then resolves :Parcel across every lot, which is a SECOND PASS on
purpose — you cannot tell which lots share a physical parcel until every
identifier in the corpus exists. It merges two lots only when they cite the
same identifier AND sit in the same revenue village, so phase B's gazetteer
links are what make it fire at all.

GATED ON `extraction_json IS NOT NULL`, not on review status. Every one of the
245 extracted documents is still `extraction_review_status = 'pending'`, so
gating on 'verified' would promote exactly nothing. Verification is instead
recorded per node (`verified_at` / `verified_by`) and carried forward from the
Document when it has been reviewed, so a trusted-subset query stays possible.

IDEMPOTENT. Everything MERGEs on a stable key (lot_key, (kind, value_norm),
account_no, hash …), so a re-run updates in place rather than duplicating. The
scraped :AuctionProperty is never written to — notice values live on :Lot and
:Auction so that a notice/website disagreement stays visible instead of one
silently overwriting the other.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from api.neo4j_client import run_query, run_read_query
from pipeline.apply_extractions import entities_with_corrections, parse_money
from pipeline.measures import (
    parse_area, parse_length, pick_headline, read_adjacency,
)
from pipeline.obs import get_logger
from pipeline.place_resolution import Gazetteer, resolve_place
from pipeline.resolve_places import norm_place
from pipeline.validators import normalize_identifier_kind

log = get_logger(__name__)

WRITE_CHUNK = 100
SIDES = ("north", "south", "east", "west")

# Documents are promoted concurrently. Each one is a handful of small write
# statements whose cost is almost entirely the round trip to Aura, so the
# useful width is set by the network, not by CPU — serial over the full corpus
# is hours of wall time for minutes of database work.
DEFAULT_WORKERS = int(os.environ.get("PROMOTE_WORKERS", "8"))

# Concurrent writers MERGE the same shared nodes (:Bank, :Platform,
# :TermsTemplate, :Contact, :EMDAccount), so lock contention is expected rather
# than exceptional. The Bolt driver retries these itself; the HTTP Query API
# path does not, so retry here — it is the same "re-run the unit of work"
# contract either way, and every statement is idempotent.
_TRANSIENT_MARKERS = (
    "DeadlockDetected",
    "LockClientStopped",
    "Neo.TransientError",
    "ConstraintValidationFailed",
    "LeaderSwitch",
    "NotALeader",
)
WRITE_RETRIES = 5


def _is_transient(exc: Exception) -> bool:
    msg = str(exc)
    return any(m in msg for m in _TRANSIENT_MARKERS)


def write(cypher: str, params: dict) -> list[dict]:
    """run_query with backoff on the contention a concurrent promote provokes."""
    for attempt in range(1, WRITE_RETRIES + 1):
        try:
            return run_query(cypher, params)
        except Exception as exc:
            if attempt == WRITE_RETRIES or not _is_transient(exc):
                raise
            delay = 0.25 * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            log.warning("write contention (attempt %d/%d), retrying in %.2fs: %s",
                        attempt, WRITE_RETRIES, delay, exc)
            time.sleep(delay)
    return []

# extent attr -> Measurement.kind. uds_parent is carried so it is not lost,
# but pick_headline() refuses to ever divide a price by it.
EXTENT_KINDS = {
    "total_area":          "total",
    "extent_sqft":         "extent",
    "super_built_up_area": "super_built_up",
    "built_up_area":       "built_up",
    "carpet_area":         "carpet",
    "undivided_share":     "uds",
    "uds_parent_extent":   "uds_parent",
}

POSSESSION_VALUES = {"symbolic", "constructive", "physical"}

# The catalogue's 8-value role enum. HAS_PARTY carries it; the existing
# HAS_BORROWER edge on :AuctionProperty loses it entirely.
PARTY_ROLES = {
    "borrower", "co-borrower", "guarantor", "director", "partner",
    "mortgagor", "proprietor", "legal-heir",
}


def _s(v) -> str | None:
    """Trimmed string, or None for the empty/placeholder values the prompt
    is told never to emit but occasionally does anyway."""
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in {"null", "na", "n/a", "nil known", "-"} else None


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ── gazetteer ────────────────────────────────────────────────────────────────
# The official TN hierarchy (38 districts / 316 taluks / 17,164 revenue
# villages) already lives in the graph. It is read once per process and shared
# across the phase-B workers — 17k rows is a few MB, and re-reading it per
# document would cost more than the promotion itself.
_GAZ: Gazetteer | None = None
_GAZ_LOCK = threading.Lock()


def gazetteer() -> Gazetteer:
    global _GAZ
    with _GAZ_LOCK:
        if _GAZ is None:
            _GAZ = Gazetteer(
                districts=[r["name"] for r in run_read_query(
                    "MATCH (d:District) RETURN d.name AS name",
                    max_rows=1_000)],
                taluks=[(r["taluk"], r["district"]) for r in run_read_query(
                    "MATCH (t:Taluk)-[:IN_DISTRICT]->(d:District) "
                    "RETURN t.name AS taluk, d.name AS district",
                    max_rows=5_000)],
                villages=[(r["village"], r["taluk"], r["district"])
                          for r in run_read_query(
                    "MATCH (v:RevenueVillage)-[:IN_TALUK]->(t:Taluk)"
                    "-[:IN_DISTRICT]->(d:District) "
                    "RETURN v.name AS village, t.name AS taluk, "
                    "       d.name AS district",
                    max_rows=50_000, timeout=120.0)],
            )
            log.info("gazetteer loaded: %d districts, %d taluks, %d villages",
                     len(_GAZ.districts), len(_GAZ.taluks), len(_GAZ.villages))
    return _GAZ


def lot_place(rec: dict) -> dict:
    """Resolve one lot's extracted village/taluk/district onto the gazetteer.

    The notice's own strings are never stored as the answer — they go through
    the same bottom-up resolver the scraped-listing path uses
    (pipeline.place_resolution), so an outdated district ("Kanchipuram" for a
    2019 Chengalpattu taluk) is corrected by the taluk beneath it rather than
    believed.

    Returns the row _WRITE_LOT_PLACE consumes: the resolved official names, or
    None at whatever level did not resolve, plus the reason. A lot that cannot
    be placed keeps `place_status` and no edge — a visible gap, never a guess.
    """
    gaz = gazetteer()
    loc = rec["location"]
    r = resolve_place(gaz,
                      district=loc.get("district"),
                      taluk=loc.get("taluk"),
                      village=loc.get("village"))
    status, source = r["village_status"], r["village_source"]
    district, taluk, village = r["district"], r["taluk"], r["village"]

    # Notices name the taluk more coarsely than the revenue record does —
    # "Dindigul" where the gazetteer has split it into Dindigul East and West,
    # "Udumalpet" for Udumalaipettai. resolve_place is right to refuse those
    # (guessing East vs West is a coin flip), but the *village* underneath is
    # still findable: search the district instead, on the gazetteer's own
    # guarded terms — exact match only, and only when the district holds
    # exactly one village of that name. That recovers the taluk for free,
    # since a village names its own parent.
    if village is None and district and loc.get("village"):
        wider = gaz.village_in_district(loc["village"], district)
        if wider:
            village, taluk = wider
            status, source = "resolved", "district"

    return {
        "lot_key": rec["lot_key"],
        "district": district,
        "taluk": taluk,
        "village": village,
        "status": status,
        "source": source,
        "conflict": r["conflict"],
    }


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def value_norm(v: str | None) -> str | None:
    """Identifier comparison key: case- and punctuation-insensitive, but the
    subdivision suffix is preserved because 72/1B and 72/1B1 are different
    parcels. Spaces around separators go; the separators stay."""
    if not v:
        return None
    s = re.sub(r"\s*/\s*", "/", str(v).strip())
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"[^\w/\-]", "", s)
    return s.lower() or None


# ── pure: entities -> per-lot records ────────────────────────────────────────

def build_lots(entities: list[dict], filename: str) -> tuple[dict, list[dict]]:
    """Split grounded entities into (notice_level, [lot records]).

    Mirrors apply_extractions.group_lots' lot_index convention, but keeps the
    structure the flat model threw away: every identifier (not just doors),
    every extent with its unit, and each boundary's road width and access kind.
    """
    notice: dict = {"facts": [], "contacts": [], "loan_accounts": []}
    lots: dict[str, dict] = {}

    def lot(li: str) -> dict:
        return lots.setdefault(li, {
            "lot_index": li,
            "lot_key": f"{filename}#{li}",
            "props": {},
            "description_parts": [],
            "identifiers": [],
            "measurements": [],
            "boundaries": {},
            "schedules": [],
            "parties": [],
            "facts": [],
            "auction": {},
            "outstanding": [],
            "location": {},
        })

    for e in entities:
        cls = e.get("cls")
        attrs = e.get("attrs") or {}
        text = _s(e.get("text"))
        li = str(attrs.get("lot_index") or "1")

        if cls == "secured_creditor":
            notice.update({k: v for k, v in {
                "legal_basis": _s(attrs.get("legal_basis")),
                "bank_name": _s(attrs.get("bank_name")),
                "branch": _s(attrs.get("branch")),
                "authorised_officer": _s(attrs.get("authorised_officer")),
                "liquidator": _s(attrs.get("liquidator")),
                "assignor_bank": _s(attrs.get("assignor_bank")),
                "trust_name": _s(attrs.get("trust_name")),
                "assignment_date": _s(attrs.get("assignment_date")),
                "court_reference": _s(attrs.get("court_reference")),
                "predecessor_entity": _s(attrs.get("predecessor_entity")),
                "sale_terms": _s(attrs.get("sale_terms")),
                "platform_url": _s(attrs.get("auction_platform_url")),
            }.items() if v is not None})

        elif cls == "contact":
            notice["contacts"].append({
                "phones": _s(attrs.get("phones")),
                "email": _s(attrs.get("email")),
            })

        elif cls == "emd_account":
            notice["emd_account"] = {
                "account_no": _s(attrs.get("account_no")),
                "ifsc": _s(attrs.get("ifsc")),
                "account_name": _s(attrs.get("account_name")),
                "bank": _s(attrs.get("bank")),
                "mode_of_payment": _s(attrs.get("mode_of_payment")),
            }

        elif cls == "full_terms":
            if text:
                notice["terms_text"] = text
                notice["terms_hash"] = _hash(text)

        elif cls == "borrower":
            role = (_s(attrs.get("role")) or "borrower").lower()
            name = _s(attrs.get("name")) or text
            if name:
                lot(li)["parties"].append({
                    "name": name,
                    "role": role if role in PARTY_ROLES else "borrower",
                    "address": _s(attrs.get("address")),
                })

        elif cls == "property":
            p = lot(li)["props"]
            for src, dst in (("property_type", "property_type"),
                             ("asset_category", "asset_category"),
                             ("construction_type", "construction_type"),
                             ("occupancy_status", "occupancy_status"),
                             ("address", "address"),
                             ("encumbrance", "encumbrance"),
                             ("possession_date", "possession_date"),
                             ("branch_of_lot", "branch_of_lot"),
                             ("title_deed_holder", "title_deed_holder")):
                v = _s(attrs.get(src))
                if v is not None and dst not in p:
                    p[dst] = v
            pt = (_s(attrs.get("possession_type")) or "").lower()
            # The prompt emits nothing when a notice prints the boilerplate
            # disjunction without choosing. Record that refusal explicitly —
            # `null` would be indistinguishable from a failed extraction.
            if pt in POSSESSION_VALUES:
                p["possession_type"] = pt
                p["possession_stated"] = True
            else:
                p.setdefault("possession_stated", False)

        elif cls == "full_description":
            if text:
                lot(li)["description_parts"].append(text)

        elif cls == "location":
            loc = lot(li)["location"]
            for k in ("village", "taluk", "district", "city", "area", "state",
                      "panchayat", "municipality_corporation", "ward_no",
                      "registration_district", "registration_sub_district",
                      "landmark", "latitude", "longitude"):
                v = _s(attrs.get(k))
                if v is not None and k not in loc:
                    loc[k] = v

        elif cls == "identifier":
            kind, _ = normalize_identifier_kind(_s(attrs.get("kind")) or "")
            raw = _s(attrs.get("value")) or text
            vn = value_norm(raw)
            if kind and vn:
                lot(li)["identifiers"].append({
                    "kind": kind, "value_norm": vn,
                    "value_raw": raw, "as_written": text,
                })

        elif cls == "extent":
            for src, kind in EXTENT_KINDS.items():
                raw = _s(attrs.get(src))
                if raw is None:
                    continue
                # extent_sqft is already a bare sq.ft number by contract
                if src == "extent_sqft":
                    val = _num(raw)
                    unit, sqft = ("sq_ft", val) if val is not None else (None, None)
                    norm_method = "stated"
                else:
                    val, unit, sqft = parse_area(raw)
                    norm_method = "converted" if unit and unit != "sq_ft" else "stated"
                lot(li)["measurements"].append({
                    "kind": kind, "raw": raw, "value": val,
                    "unit": unit, "sqft_norm": sqft, "norm_method": norm_method,
                })

        elif cls == "boundary":
            side = (_s(attrs.get("side")) or "").lower()
            if side in SIDES:
                adjacency = _s(attrs.get("adjacency")) or text
                measurement = _s(attrs.get("measurement"))
                access_kind, road_width = read_adjacency(adjacency)
                lot(li)["boundaries"][side] = {
                    "side": side,
                    "adjacency_raw": adjacency,
                    "access_kind": access_kind,
                    "road_width_ft": road_width,
                    "measurement_raw": measurement,
                    "measurement_ft": parse_length(measurement),
                    "is_length_valid": measurement is None or bool(parse_length(measurement)),
                }

        elif cls == "schedule":
            lot(li)["schedules"].append({
                "label": _s(attrs.get("label")),
                "type": _s(attrs.get("type")),
                "extent": _s(attrs.get("extent")),
                "description": text,
            })

        elif cls == "auction_terms":
            a = lot(li)["auction"]
            for src, dst, conv in (
                ("reserve_price_num", "reserve_price_num", parse_money),
                ("emd_num", "emd_num", parse_money),
                ("bid_increment_num", "bid_increment_num", parse_money),
                ("auto_extension_minutes", "auto_extension_minutes", _num),
                ("auction_start_dt", "auction_start_dt", _s),
                ("auction_end_dt", "auction_end_dt", _s),
                ("application_deadline_dt", "application_deadline_dt", _s),
                ("inspection_dt", "inspection_dt", _s),
                ("sarfaesi_stage", "sarfaesi_stage", _s),
            ):
                v = conv(attrs.get(src))
                if v is not None and dst not in a:
                    a[dst] = v

        elif cls == "outstanding":
            acct = _s(attrs.get("loan_account_no"))
            lot(li)["outstanding"].append({
                "account_no": acct,
                "amount_num": parse_money(attrs.get("amount_num")),
                "as_on": _s(attrs.get("as_on")),
                "demand_notice_date": _s(attrs.get("demand_notice_date")),
            })

        elif cls == "extras":
            key, val = _s(attrs.get("key")), _s(attrs.get("value")) or text
            if key and val:
                target = lot(li)["facts"] if attrs.get("lot_index") else notice["facts"]
                target.append({"key": key, "value": val})

    # finalize each lot: description, headline extent, derived road numbers
    out = []
    for li, rec in sorted(lots.items(), key=lambda kv: kv[0]):
        p = rec["props"]
        if rec["description_parts"]:
            p["full_description"] = "\n\n".join(rec["description_parts"])

        by_kind = {m["kind"]: m["sqft_norm"] for m in rec["measurements"]}
        rec["headline_kind"] = pick_headline(by_kind, p.get("property_type"))

        widths = [b["road_width_ft"] for b in rec["boundaries"].values()
                  if b["access_kind"] in {"road", "street"} and b["road_width_ft"]]
        if widths:
            p["road_width_ft"] = max(widths)
            # frontage = the parcel's own dimension along its widest road side
            best = max((b for b in rec["boundaries"].values()
                        if b["road_width_ft"] == max(widths)),
                       key=lambda b: b.get("measurement_ft") or 0, default=None)
            if best and best.get("measurement_ft"):
                p["frontage_ft"] = best["measurement_ft"]

        loc = rec["location"]
        for src, dst in (("latitude", "latitude"), ("longitude", "longitude")):
            v = _num(loc.get(src))
            if v is not None:
                p[dst] = v
        if loc.get("landmark"):
            p["landmark"] = loc["landmark"]

        rec["props"] = p
        out.append(rec)
    return notice, out


# ── Neo4j writes ─────────────────────────────────────────────────────────────

_FETCH = """
MATCH (d:Document)
WHERE d.extraction_json IS NOT NULL
  {filename_clause}
RETURN d.filename AS filename,
       d.extraction_json AS extraction_json,
       d.extraction_corrections_json AS corrections_json,
       d.extraction_review_status AS review_status
ORDER BY d.filename
{limit_clause}
"""

# One statement per document keeps the write set small enough to reason about
# and lets a single bad notice fail without rolling back the batch.
_WRITE_LOT = """
MATCH (d:Document {filename: $filename})
MERGE (l:Lot {lot_key: $lot_key})
SET l += $props,
    l.lot_index = $lot_index,
    l.promoted_at = datetime(),
    l.verified_at = CASE WHEN $review_status = 'verified'
                         THEN datetime() ELSE l.verified_at END
MERGE (d)-[:HAS_LOT]->(l)

// possession as a dimension node, plus the date on the edge
FOREACH (_ IN CASE WHEN $possession_type IS NULL THEN [] ELSE [1] END |
  MERGE (pt:PossessionType {name: $possession_type})
  MERGE (l)-[pr:POSSESSION_IS]->(pt)
  SET pr.taken_on = $possession_date)

// identifiers: the notice MENTIONS them; :Parcel later owns them
FOREACH (i IN $identifiers |
  MERGE (idn:Identifier {kind: i.kind, value_norm: i.value_norm})
  SET idn.value_raw = coalesce(idn.value_raw, i.value_raw)
  MERGE (l)-[mi:MENTIONS_IDENTIFIER]->(idn)
  SET mi.as_written = i.as_written)

// one Measurement per extent kind, with its unit
FOREACH (m IN $measurements |
  MERGE (ms:Measurement {lot_key: $lot_key, kind: m.kind})
  SET ms.raw = m.raw, ms.value = m.value, ms.unit = m.unit,
      ms.sqft_norm = m.sqft_norm, ms.norm_method = m.norm_method
  MERGE (l)-[he:HAS_EXTENT]->(ms)
  SET he.kind = m.kind,
      he.is_headline = (m.kind = $headline_kind)
  FOREACH (_ IN CASE WHEN m.unit IS NULL THEN [] ELSE [1] END |
    MERGE (u:Unit {name: m.unit})
    MERGE (ms)-[:IN_UNIT]->(u)))

// one Boundary per side, carrying road width and access kind
FOREACH (b IN $boundaries |
  MERGE (bd:Boundary {lot_key: $lot_key, side: b.side})
  SET bd += b
  MERGE (l)-[:HAS_BOUNDARY {side: b.side}]->(bd))

FOREACH (s IN $schedules |
  MERGE (sc:Schedule {lot_key: $lot_key, label: coalesce(s.label, 'A')})
  SET sc += s
  MERGE (l)-[:HAS_SCHEDULE]->(sc))

FOREACH (f IN $facts |
  MERGE (ft:Fact {key: f.key, value: f.value})
  MERGE (l)-[:HAS_FACT]->(ft))

// Borrower is the existing label; HAS_PARTY adds the role the old edge lost
FOREACH (pa IN $parties |
  MERGE (b:Borrower {name: pa.name})
  SET b.address = coalesce(b.address, pa.address)
  MERGE (l)-[hp:HAS_PARTY]->(b)
  SET hp.role = pa.role)

FOREACH (_ IN CASE WHEN $title_deed_holder IS NULL THEN [] ELSE [1] END |
  MERGE (th:Borrower {name: $title_deed_holder})
  MERGE (l)-[:TITLE_HELD_BY]->(th))

// the sale event
FOREACH (_ IN CASE WHEN $has_auction THEN [1] ELSE [] END |
  MERGE (au:Auction {lot_key: $lot_key})
  SET au += $auction
  MERGE (l)-[:OFFERED_IN]->(au))

// the debt
FOREACH (o IN $outstanding |
  FOREACH (_ IN CASE WHEN o.account_no IS NULL THEN [] ELSE [1] END |
    MERGE (la:LoanAccount {account_no: o.account_no})
    MERGE (l)-[se:SECURES]->(la)
    SET se.outstanding_num = o.amount_num,
        se.as_on = o.as_on,
        se.demand_notice_date = o.demand_notice_date))
RETURN l.lot_key AS lot_key
"""

_WRITE_NOTICE = """
MATCH (d:Document {filename: $filename})
SET d.sale_terms = coalesce($sale_terms, d.sale_terms),
    d.promoted_at = datetime()

FOREACH (_ IN CASE WHEN $legal_basis IS NULL THEN [] ELSE [1] END |
  MERGE (f:LegalFramework {name: $legal_basis})
  MERGE (d)-[:UNDER_FRAMEWORK]->(f))

FOREACH (_ IN CASE WHEN $court_reference IS NULL THEN [] ELSE [1] END |
  MERGE (c:CaseReference {ref: $court_reference})
  MERGE (d)-[:CASE_REF]->(c))

FOREACH (_ IN CASE WHEN $trust_name IS NULL THEN [] ELSE [1] END |
  MERGE (t:Trust {name: $trust_name})
  MERGE (d)-[:UNDER_TRUST]->(t))

FOREACH (_ IN CASE WHEN $officer_name IS NULL THEN [] ELSE [1] END |
  MERGE (o:Officer {name: $officer_name})
  MERGE (d)-[sb:SIGNED_BY]->(o)
  SET sb.role = $officer_role)

FOREACH (_ IN CASE WHEN $bank_name IS NULL THEN [] ELSE [1] END |
  MERGE (b:Bank {name: $bank_name})
  MERGE (d)-[:ISSUED_BY]->(b))

FOREACH (_ IN CASE WHEN $assignor_bank IS NULL THEN [] ELSE [1] END |
  MERGE (ab:Bank {name: $assignor_bank})
  MERGE (d)-[da:DEBT_ASSIGNED_FROM]->(ab)
  SET da.assignment_date = $assignment_date)

FOREACH (_ IN CASE WHEN $platform_name IS NULL THEN [] ELSE [1] END |
  MERGE (pl:Platform {name: $platform_name})
  SET pl.url = coalesce(pl.url, $platform_url)
  MERGE (d)-[:HOSTED_ON]->(pl))

FOREACH (_ IN CASE WHEN $terms_hash IS NULL THEN [] ELSE [1] END |
  MERGE (tt:TermsTemplate {hash: $terms_hash})
  SET tt.text = coalesce(tt.text, $terms_text)
  MERGE (d)-[:USES_TERMS]->(tt))

FOREACH (_ IN CASE WHEN $emd_account_no IS NULL OR $emd_ifsc IS NULL
                   THEN [] ELSE [1] END |
  MERGE (e:EMDAccount {account_no: $emd_account_no, ifsc: $emd_ifsc})
  SET e.account_name = coalesce(e.account_name, $emd_account_name),
      e.mode_of_payment = coalesce(e.mode_of_payment, $emd_mode)
  MERGE (d)-[:EMD_PAYABLE_TO]->(e))

FOREACH (c IN $contacts |
  MERGE (ct:Contact {phone: coalesce(c.phones, ''), email: coalesce(c.email, '')})
  MERGE (d)-[:HAS_CONTACT]->(ct))

FOREACH (f IN $facts |
  MERGE (ft:Fact {key: f.key, value: f.value})
  MERGE (d)-[:HAS_FACT]->(ft))
RETURN d.filename AS filename
"""


_WRITE_LOT_PLACE = """
UNWIND $rows AS row
MATCH (l:Lot {lot_key: row.lot_key})
SET l.place_status = row.status,
    l.place_source = row.source,
    l.place_conflict = row.conflict,
    l.village = row.village,
    l.taluk = row.taluk,
    l.district = row.district

// MATCH, never MERGE: every name here came out of the gazetteer, so a miss is
// a bug worth leaving visible. MERGE would invent a duplicate place node with
// no code hanging off the hierarchy — silently wrong instead of visibly absent.
WITH l, row
OPTIONAL MATCH (v:RevenueVillage {name: row.village})
               -[:IN_TALUK]->(t:Taluk {name: row.taluk})
               -[:IN_DISTRICT]->(d:District {name: row.district})
WITH l, collect({v: v, t: t, d: d}) AS hits
// 21 taluks hold two villages of one name. resolve_place already refuses
// those, but a second guard here costs nothing and a wrong parcel merge is
// far worse than a missing one.
WITH l, hits WHERE size(hits) = 1
UNWIND hits AS hit
WITH l, hit.v AS v, hit.t AS t, hit.d AS d WHERE v IS NOT NULL
MERGE (l)-[:IN_REVENUE_VILLAGE]->(v)
MERGE (l)-[:IN_TALUK]->(t)
MERGE (l)-[:IN_DISTRICT]->(d)
RETURN count(*) AS linked
"""

# Lots whose village did not resolve can still be placed at district level —
# useful on its own, and it costs one more statement rather than complicating
# the guard above. Taluk names are globally unique, districts likewise, so
# neither needs the size(hits) check.
_WRITE_LOT_DISTRICT = """
UNWIND $rows AS row
WITH row WHERE row.village IS NULL AND row.district IS NOT NULL
MATCH (l:Lot {lot_key: row.lot_key})
MATCH (d:District {name: row.district})
MERGE (l)-[:IN_DISTRICT]->(d)
WITH l, row
OPTIONAL MATCH (t:Taluk {name: row.taluk})
FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END |
  MERGE (l)-[:IN_TALUK]->(t))
RETURN count(*) AS linked
"""


def write_places(rows: list[dict]) -> None:
    """Link a document's lots to the canonical geography. Idempotent."""
    if not rows:
        return
    write(_WRITE_LOT_PLACE, {"rows": rows})
    write(_WRITE_LOT_DISTRICT, {"rows": rows})


def fetch_documents(limit: int | None, filename: str | None) -> list[dict]:
    q = _FETCH.format(
        filename_clause="AND d.filename = $fn" if filename else "",
        limit_clause=f"LIMIT {int(limit)}" if limit else "",
    )
    return run_read_query(q, {"fn": filename} if filename else None,
                          max_rows=20_000, timeout=120.0)


def promote_document(doc: dict, dry_run: bool) -> tuple[int, Counter]:
    filename = doc["filename"]
    entities = entities_with_corrections(doc["extraction_json"],
                                         doc.get("corrections_json"))
    notice, lots = build_lots(entities, filename)
    if dry_run:
        places = [lot_place(rec) for rec in lots]
        print(f"  [dry-run] {filename}: {len(lots)} lot(s), "
              f"{sum(len(l['identifiers']) for l in lots)} identifier(s), "
              f"{sum(len(l['measurements']) for l in lots)} extent(s), "
              f"{sum(1 for p in places if p['village'])}/{len(places)} placed "
              f"({Counter(p['status'] for p in places).most_common()})")
        return len(lots), Counter(p["status"] for p in places)

    officer = notice.get("authorised_officer") or notice.get("liquidator")
    emd = notice.get("emd_account") or {}
    platform_url = notice.get("platform_url")
    write(_WRITE_NOTICE, {
        "filename": filename,
        "sale_terms": notice.get("sale_terms"),
        "legal_basis": notice.get("legal_basis"),
        "court_reference": notice.get("court_reference"),
        "trust_name": notice.get("trust_name"),
        "officer_name": officer,
        "officer_role": "liquidator" if notice.get("liquidator") else "authorised_officer",
        "bank_name": notice.get("bank_name"),
        "assignor_bank": notice.get("assignor_bank"),
        "assignment_date": notice.get("assignment_date"),
        "platform_name": platform_name_of(platform_url),
        "platform_url": platform_url,
        "terms_hash": notice.get("terms_hash"),
        "terms_text": notice.get("terms_text"),
        "emd_account_no": emd.get("account_no"),
        "emd_ifsc": emd.get("ifsc"),
        "emd_account_name": emd.get("account_name"),
        "emd_mode": emd.get("mode_of_payment"),
        "contacts": [c for c in notice["contacts"] if c["phones"] or c["email"]],
        "facts": notice["facts"],
    })

    for rec in lots:
        props = dict(rec["props"])
        possession_type = props.pop("possession_type", None)
        possession_date = props.pop("possession_date", None)
        title_deed_holder = props.pop("title_deed_holder", None)
        auction = rec["auction"]
        write(_WRITE_LOT, {
            "filename": filename,
            "lot_key": rec["lot_key"],
            "lot_index": rec["lot_index"],
            "props": props,
            "review_status": doc.get("review_status"),
            "possession_type": possession_type,
            "possession_date": possession_date,
            "title_deed_holder": title_deed_holder,
            "identifiers": rec["identifiers"],
            "measurements": rec["measurements"],
            "headline_kind": rec["headline_kind"],
            "boundaries": list(rec["boundaries"].values()),
            "schedules": rec["schedules"],
            "facts": rec["facts"],
            "parties": rec["parties"],
            "has_auction": bool(auction),
            "auction": auction,
            "outstanding": [o for o in rec["outstanding"] if o["account_no"]],
        })
    places = [lot_place(rec) for rec in lots]
    write_places(places)
    return len(lots), Counter(p["status"] for p in places)


def place_document(doc: dict, dry_run: bool) -> tuple[int, Counter]:
    """Only the geography half of promote_document, for lots already loaded.

    Rebuilds the same lot records — they are pure functions of the extraction —
    but writes nothing except the gazetteer links, so re-linking the corpus is
    two statements per document instead of the full node write.
    """
    filename = doc["filename"]
    entities = entities_with_corrections(doc["extraction_json"],
                                         doc.get("corrections_json"))
    _, lots = build_lots(entities, filename)
    places = [lot_place(rec) for rec in lots]
    if dry_run:
        print(f"  [dry-run] {filename}: "
              f"{sum(1 for p in places if p['village'])}/{len(places)} placed "
              f"({Counter(p['status'] for p in places).most_common()})")
        return len(lots), Counter(p["status"] for p in places)
    write_places(places)
    return len(lots), Counter(p["status"] for p in places)


def platform_name_of(url: str | None) -> str | None:
    """Host-derived platform name, so the notice's URL and the scraper's
    `service_provider` land on the same :Platform node."""
    if not url:
        return None
    m = re.search(r"https?://(?:www\.)?([^/\s]+)", str(url))
    host = (m.group(1) if m else str(url)).strip().lower()
    host = re.sub(r"\.(com|in|co\.in|org|net)$", "", host)
    return host.split(".")[-1].upper() if host else None


# ── Phase C: parcels ─────────────────────────────────────────────────────────

# Two lots that cite the same identifier AND resolve to the same village are
# the same land. Village scoping matters: survey numbers repeat across the
# state, so an unscoped identifier match would merge unrelated parcels — and a
# bad merge is far harder to undo than a missed one.
_RESOLVE_PARCELS = """
MATCH (l1:Lot)-[:MENTIONS_IDENTIFIER]->(i:Identifier)<-[:MENTIONS_IDENTIFIER]-(l2:Lot)
WHERE elementId(l1) < elementId(l2)
  AND i.kind IN ['survey_old','survey_new','patta','cersai','property_id','sale_deed']
MATCH (l1)-[:IN_REVENUE_VILLAGE]->(v:RevenueVillage)<-[:IN_REVENUE_VILLAGE]-(l2)
WITH l1, l2, collect(DISTINCT i.kind + ':' + i.value_norm) AS shared
MERGE (l1)-[:IS_PARCEL]->(p:Parcel {parcel_id: 'auto-' + l1.lot_key})
MERGE (l2)-[r2:IS_PARCEL]->(p)
SET r2.confidence = 'high', r2.method = 'identifier',
    p.evidence = shared, p.last_seen = datetime()
RETURN count(DISTINCT p) AS parcels
"""

# Every lot that shares no identifier with another still gets its own parcel,
# so :Parcel is a complete spine rather than only covering duplicates.
_SINGLETON_PARCELS = """
MATCH (l:Lot) WHERE NOT (l)-[:IS_PARCEL]->()
MERGE (p:Parcel {parcel_id: 'lot-' + l.lot_key})
MERGE (l)-[r:IS_PARCEL]->(p)
SET r.confidence = 'single', r.method = 'singleton',
    p.last_seen = datetime()
RETURN count(p) AS parcels
"""

_LINK_LISTINGS = """
MATCH (l:Lot)-[:IS_PARCEL]->(p:Parcel)
MATCH (l)<-[:HAS_LOT]-(:Document)<-[:HAS_DOCUMENT]-(ap:AuctionProperty)
MERGE (ap)-[r:IS_PARCEL]->(p)
SET r.confidence = 'via_document', r.method = 'document'
RETURN count(DISTINCT ap) AS listings
"""

_ATTACH_IDENTIFIERS = """
MATCH (l:Lot)-[:IS_PARCEL]->(p:Parcel)
MATCH (l)-[:MENTIONS_IDENTIFIER]->(i:Identifier)
MERGE (p)-[:HAS_IDENTIFIER]->(i)
RETURN count(*) AS links
"""

_ATTEMPT_NO = """
MATCH (p:Parcel)<-[:IS_PARCEL]-(l:Lot)-[:OFFERED_IN]->(a:Auction)
WHERE a.auction_start_dt IS NOT NULL
WITH p, a ORDER BY a.auction_start_dt
WITH p, collect(a) AS auctions
UNWIND range(0, size(auctions) - 1) AS idx
WITH auctions[idx] AS a, idx, size(auctions) AS total
SET a.attempt_no = idx + 1,
    // an auction followed by a later one on the same parcel did not sell
    a.outcome = CASE WHEN idx < total - 1 THEN coalesce(a.outcome, 'unsold')
                     ELSE a.outcome END
RETURN count(a) AS auctions
"""

_PARCEL_STEPS: tuple[tuple[str, str], ...] = (
    ("merge by shared identifier", _RESOLVE_PARCELS),
    ("singleton parcels", _SINGLETON_PARCELS),
    ("link listings", _LINK_LISTINGS),
    ("attach identifiers", _ATTACH_IDENTIFIERS),
    ("number attempts", _ATTEMPT_NO),
)


def resolve_parcels(dry_run: bool) -> None:
    print("phase C — parcels")
    for name, cypher in _PARCEL_STEPS:
        if dry_run:
            print(f"  [dry-run] {name}")
            continue
        rows = run_query(cypher)
        n = list(rows[0].values())[0] if rows else 0
        print(f"  {name}: {n}")


# ── main ─────────────────────────────────────────────────────────────────────

def run(limit: int | None, filename: str | None,
        dry_run: bool, skip_parcels: bool, workers: int = DEFAULT_WORKERS,
        places_only: bool = False) -> int:
    docs = fetch_documents(limit, filename)
    workers = max(1, workers if not dry_run else 1)
    label = "places" if places_only else "promoting"
    print(f"phase B — {label} {len(docs)} document(s) "
          f"across {workers} worker(s)", flush=True)

    state = {"ok": 0, "fail": 0, "lots": 0, "done": 0, "status": Counter()}
    lock = threading.Lock()

    def one(doc: dict) -> None:
        try:
            n, statuses = (place_document(doc, dry_run) if places_only
                           else promote_document(doc, dry_run))
            with lock:
                state["ok"] += 1
                state["lots"] += n
                state["status"].update(statuses)
        except Exception as exc:      # one bad notice must not stop the batch
            with lock:
                state["fail"] += 1
            log.warning("promote failed for %s: %s", doc["filename"], exc)
            print(f"  [fail] {doc['filename']}: {exc}", flush=True)
        finally:
            with lock:
                state["done"] += 1
                done = state["done"]
            if done % 50 == 0 or done == len(docs):
                print(f"  {done}/{len(docs)} documents, {state['lots']} lot(s), "
                      f"{state['fail']} failed", flush=True)

    if workers == 1:
        for doc in docs:
            one(doc)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, doc) for doc in docs]
            for f in as_completed(futures):
                f.result()            # one() already swallows per-document errors

    print(f"phase B done — {state['ok']} document(s), {state['lots']} lot(s), "
          f"{state['fail']} failed", flush=True)
    if state["status"]:
        print("  place resolution: "
              + ", ".join(f"{k}={v}" for k, v in state["status"].most_common()),
              flush=True)

    if not skip_parcels:
        resolve_parcels(dry_run)
    return 1 if state["fail"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--filename", help="promote only this Document.filename")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-parcels", action="store_true",
                    help="run phase B only; parcels need the full corpus")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"concurrent documents in phase B "
                         f"(default {DEFAULT_WORKERS}; phase C is always serial)")
    ap.add_argument("--places-only", action="store_true",
                    help="skip the phase-B node writes and only (re)link "
                         "already-promoted lots to the gazetteer, then run "
                         "phase C — the cheap path when the lots are already "
                         "loaded and only their geography changed")
    args = ap.parse_args()
    return run(args.limit, args.filename, args.dry_run, args.skip_parcels,
               args.workers, args.places_only)


if __name__ == "__main__":
    raise SystemExit(main())
