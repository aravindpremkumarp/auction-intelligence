"""
pipeline/verify_and_enrich.py
-----------------------------
Stage 1.5: Verify scraped fields against PDF/image extractions, and merge
enrichment + extras into a consolidated record per auction.

Reads per-file vision-LLM extractions cached at
  pipeline/cache/ocr_results/{auction_id}__{filename}.json
(produced by pipeline/ocr_extract.py) and the scraped records from
tn_auction_data.jsonl. Writes one consolidated record per auction to
  pipeline/output/verified_enriched.jsonl
and an extras-key frequency sidecar at
  pipeline/output/extras_key_frequency.json

Policy:
  - PDF is source of truth for `verifiable` fields.
  - On conflict: PDF value overwrites; the scraped original is preserved as
    `<field>_scraped` and the field name is added to `field_conflicts`.
  - `enrichment.*` (named open-ended but schema-stable) is unioned/merged.
  - `extras` is open-ended; shallow-merged across files (last non-null wins).

Run standalone: python -m pipeline.verify_and_enrich [--pilot] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.config import (
    INPUT_JSONL, DOWNLOADS_DIR, CACHE_DIR, OUTPUT_DIR,
    OPENROUTER_MODEL, PILOT_SIZE,
)
from pipeline.normalize import (
    normalize_bank_name, normalize_city, normalize_area, normalize_property_type,
    clean_text,
)

VERIFIED_JSONL   = OUTPUT_DIR / "verified_enriched.jsonl"
EXTRAS_FREQ_JSON = OUTPUT_DIR / "extras_key_frequency.json"

# Verifiable fields the LLM is asked to return (must stay in sync with the prompt).
VERIFIABLE_NUMERIC = {"reserve_price_num", "emd_num"}
VERIFIABLE_DATES   = {"auction_start_dt", "auction_end_dt", "application_deadline_dt"}
VERIFIABLE_STRINGS = {
    "bank_name", "branch_name", "borrower_name",
    "city", "state", "area",
    "asset_category", "property_type", "auction_type",
}
VERIFIABLE_FIELDS = VERIFIABLE_NUMERIC | VERIFIABLE_DATES | VERIFIABLE_STRINGS

# Per-field string normalizers used to avoid spurious conflicts from casing/aliases.
STRING_NORMALIZERS = {
    "bank_name":     lambda s: normalize_bank_name(s)[0],
    "city":          lambda s: normalize_city(s)[0],
    "area":          lambda s: normalize_area(s)[0],
    "property_type": lambda s: normalize_property_type(s)[0],
}

PRICE_TOLERANCE = 0.01  # 1% for numeric comparisons


# ── I/O ──────────────────────────────────────────────────────────────────────

def load_scraped_records() -> list[dict]:
    records = []
    with open(INPUT_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def load_extractions_for(auction_id: str) -> list[tuple[str, dict]]:
    """Return list of (filename, extraction) for every cached file of this auction."""
    out: list[tuple[str, dict]] = []
    prefix = f"{auction_id}__"
    for path in CACHE_DIR.glob(f"{prefix}*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        filename = path.stem[len(prefix):]
        out.append((filename, data))
    return out


# ── Merging ──────────────────────────────────────────────────────────────────

def _get_nested(d: dict, key: str) -> Any:
    """Tolerate both new nested schema ({'verifiable': {...}}) and legacy flat schema."""
    if not isinstance(d, dict):
        return None
    if key in d:
        return d[key]
    return None


def consolidate(per_file: list[tuple[str, dict]]) -> dict:
    """Merge per-file extractions into one PDF-view dict.

    Returns { "verifiable": {..., <field>_src: filename},
              "enrichment": {...},
              "extras":     {...},
              "enriched_description": "...",
              "provenance": {<field>: filename, ...} }
    """
    verifiable: dict[str, Any]  = {}
    enrichment: dict[str, Any]  = {}
    extras:     dict[str, Any]  = {}
    descs: list[str] = []
    provenance: dict[str, str]  = {}

    # Enrichment list/dict fields we union instead of overwriting.
    list_fields_enr: set[str] = set()
    dict_fields_enr = {"boundaries", "door_numbers"}

    for filename, ext in per_file:
        v = _get_nested(ext, "verifiable") or {}
        e = _get_nested(ext, "enrichment") or {}
        x = _get_nested(ext, "extras") or {}
        desc = ext.get("enriched_description")

        # verifiable: first non-null wins; record provenance
        for k, val in v.items():
            if val in (None, "", []):
                continue
            if k not in verifiable:
                verifiable[k] = val
                provenance[f"verifiable.{k}"] = filename

        # enrichment: union lists/dicts, else first non-null
        for k, val in e.items():
            if val in (None, "", [], {}):
                continue
            if k in list_fields_enr:
                merged = enrichment.get(k, [])
                seen = {json.dumps(s, sort_keys=True) for s in merged}
                for item in (val or []):
                    key = json.dumps(item, sort_keys=True)
                    if key not in seen:
                        merged.append(item)
                        seen.add(key)
                enrichment[k] = merged
            elif k in dict_fields_enr:
                merged = enrichment.get(k, {}) or {}
                for sub_k, sub_v in (val or {}).items():
                    if sub_v and not merged.get(sub_k):
                        merged[sub_k] = sub_v
                enrichment[k] = merged
            else:
                if k not in enrichment:
                    enrichment[k] = val
                    provenance[f"enrichment.{k}"] = filename

        # extras: shallow merge, last non-null wins, record all sources per key
        for k, val in (x or {}).items():
            if val in (None, "", [], {}):
                continue
            extras[k] = val
            prov_key = f"extras.{k}"
            if prov_key in provenance:
                if filename not in provenance[prov_key].split("|"):
                    provenance[prov_key] += f"|{filename}"
            else:
                provenance[prov_key] = filename

        if desc:
            descs.append(f"[{filename}] {desc.strip()}")

    return {
        "verifiable": verifiable,
        "enrichment": enrichment,
        "extras":     extras,
        "enriched_description": "\n".join(descs) if descs else None,
        "provenance": provenance,
    }


# ── Comparison ───────────────────────────────────────────────────────────────

def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = re.sub(r"[^\d.]", "", v)
        try:
            return float(s) if s else None
        except ValueError:
            return None
    return None


def _norm_date(v: Any) -> str | None:
    """Reduce a date/datetime-ish value to YYYY-MM-DD for comparison."""
    if not v:
        return None
    if isinstance(v, str):
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", v)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        # dd/mm/yyyy or dd-mm-yyyy
        m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", v)
        if m:
            return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return str(v).strip().lower() or None


def _norm_str(field: str, v: Any) -> str | None:
    if v is None:
        return None
    s = clean_text(str(v))
    if not s:
        return None
    normalizer = STRING_NORMALIZERS.get(field)
    if normalizer:
        try:
            s = normalizer(s)
        except Exception:
            pass
    return s.lower().strip() if s else None


def fields_agree(field: str, scraped: Any, pdf: Any) -> bool:
    """Return True if scraped and PDF values are effectively the same."""
    if scraped in (None, "", []) or pdf in (None, "", []):
        # Can't be a conflict if one side is missing
        return True
    if field in VERIFIABLE_NUMERIC:
        a, b = _to_float(scraped), _to_float(pdf)
        if a is None or b is None:
            return True
        if a == 0 and b == 0:
            return True
        return abs(a - b) / max(abs(a), abs(b)) <= PRICE_TOLERANCE
    if field in VERIFIABLE_DATES:
        return _norm_date(scraped) == _norm_date(pdf)
    # strings
    ns, np = _norm_str(field, scraped), _norm_str(field, pdf)
    if ns is None or np is None:
        return True
    return ns == np or ns in np or np in ns


def compare_and_resolve(scraped: dict, pdf_view: dict) -> tuple[dict, dict, list[str]]:
    """Return (verified_fields, scraped_originals, field_conflicts).

    verified_fields  — PDF value when present, else scraped value (for every verifiable field
                       where *any* side has a value).
    scraped_originals — {<field>_scraped: scraped_value} only for fields we overwrote due to
                        conflict or PDF-only enrichment of a scraped field.
    field_conflicts   — list of field names where both sides had values and they disagreed.
    """
    verifiable = pdf_view.get("verifiable") or {}
    verified_fields: dict[str, Any]   = {}
    scraped_originals: dict[str, Any] = {}
    field_conflicts: list[str] = []

    for field in VERIFIABLE_FIELDS:
        scraped_val = scraped.get(field)
        pdf_val     = verifiable.get(field)

        if pdf_val not in (None, "", []):
            verified_fields[field] = pdf_val
            if scraped_val not in (None, "", []) and not fields_agree(field, scraped_val, pdf_val):
                field_conflicts.append(field)
                scraped_originals[f"{field}_scraped"] = scraped_val
        elif scraped_val not in (None, "", []):
            verified_fields[field] = scraped_val

    return verified_fields, scraped_originals, field_conflicts


# ── Enrichment flattening for Neo4j ──────────────────────────────────────────

def flatten_enrichment(enr: dict) -> dict:
    """Flatten enrichment block into scalar-friendly properties for Neo4j."""
    if not enr:
        return {}
    boundaries = enr.get("boundaries") or {}
    doors      = enr.get("door_numbers") or {}
    out = {
        "undivided_share":           enr.get("undivided_share"),
        "total_area":                enr.get("total_area"),
        "village":                   enr.get("village"),
        "taluk":                     enr.get("taluk"),
        "district":                  enr.get("district"),
        "registration_district":     enr.get("registration_district"),
        "registration_sub_district": enr.get("registration_sub_district"),
        "boundary_north":            boundaries.get("north"),
        "boundary_south":            boundaries.get("south"),
        "boundary_east":             boundaries.get("east"),
        "boundary_west":             boundaries.get("west"),
        "door_numbers_old":          doors.get("old") or None,
        "door_numbers_new":          doors.get("new") or None,
    }
    return {k: v for k, v in out.items() if v is not None}


# ── Main ─────────────────────────────────────────────────────────────────────

def process_record(scraped: dict) -> dict | None:
    auction_id = scraped.get("auction_id")
    if not auction_id:
        return None

    per_file = load_extractions_for(auction_id)
    if not per_file:
        # Build a minimal pass-through record so Neo4j can still mark it as un-verified
        return {
            "auction_id": auction_id,
            "verified_fields": {k: scraped.get(k) for k in VERIFIABLE_FIELDS
                                 if scraped.get(k) not in (None, "", [])},
            "enrichment_flat": {},
            "extras_json": "{}",
            "enriched_description": None,
            "scraped_originals": {},
            "field_conflicts": [],
            "verification_status": "no_pdf_data",
            "provenance": {},
            "documents": [],
        }

    pdf_view = consolidate(per_file)
    verified_fields, scraped_originals, conflicts = compare_and_resolve(scraped, pdf_view)

    if pdf_view.get("verifiable"):
        status = "verified" if not conflicts else "partial"
    else:
        status = "no_pdf_data"

    documents = []
    now = datetime.now(timezone.utc).isoformat()
    for filename, ext in per_file:
        ext_path = DOWNLOADS_DIR / filename
        resolved = str(ext_path) if ext_path.exists() else filename
        doc_type = "pdf" if Path(filename).suffix.lower() == ".pdf" else "image"
        documents.append({
            "filename":              filename,
            "file_path":             resolved,
            "doc_type":              doc_type,
            "extracted_fields_json": json.dumps(ext, ensure_ascii=False),
            "extracted_at":          now,
            "model":                 OPENROUTER_MODEL,
        })

    return {
        "auction_id":           auction_id,
        "verified_fields":      verified_fields,
        "enrichment_flat":      flatten_enrichment(pdf_view.get("enrichment") or {}),
        "extras_json":          json.dumps(pdf_view.get("extras") or {}, ensure_ascii=False),
        "enriched_description": pdf_view.get("enriched_description"),
        "scraped_originals":    scraped_originals,
        "field_conflicts":      conflicts,
        "verification_status":  status,
        "provenance":           pdf_view.get("provenance") or {},
        "documents":            documents,
    }


def run(limit: int | None = None, pilot: bool = False) -> None:
    records = load_scraped_records()
    if pilot:
        records = records[:PILOT_SIZE]
    if limit:
        records = records[:limit]

    total = len(records)
    print(f"Verify + Enrich: {total} records")
    print(f"  Cache dir : {CACHE_DIR}")
    print(f"  Output    : {VERIFIED_JSONL}")

    status_counts = Counter()
    conflict_counts = Counter()
    extras_keys = Counter()
    written = 0

    VERIFIED_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(VERIFIED_JSONL, "w", encoding="utf-8") as out_f:
        for rec in records:
            result = process_record(rec)
            if result is None:
                continue
            out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
            written += 1
            status_counts[result["verification_status"]] += 1
            for field in result["field_conflicts"]:
                conflict_counts[field] += 1
            try:
                extras = json.loads(result["extras_json"])
                extras_keys.update(extras.keys())
            except json.JSONDecodeError:
                pass

    EXTRAS_FREQ_JSON.write_text(
        json.dumps({
            "total_records": written,
            "keys": dict(extras_keys.most_common()),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n  Written   : {written}")
    print(f"  Status    : {dict(status_counts)}")
    if conflict_counts:
        print(f"  Conflicts : {dict(conflict_counts.most_common())}")
    print(f"  Extras keys ({len(extras_keys)} unique): top -> {dict(extras_keys.most_common(10))}")


def main():
    parser = argparse.ArgumentParser(description="Verify scraped auction fields against PDF/image extractions")
    parser.add_argument("--pilot", action="store_true", help=f"Process first PILOT_SIZE ({PILOT_SIZE}) records only")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N records")
    args = parser.parse_args()
    run(limit=args.limit, pilot=args.pilot)


if __name__ == "__main__":
    main()
