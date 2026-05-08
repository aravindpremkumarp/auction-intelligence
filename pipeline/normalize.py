"""
pipeline/normalize.py
---------------------
Stage 3: Entity normalization and deduplication (PRD 5.8).

Reads lexical.jsonl and applies lookup-table + fuzzy matching normalization
to standardize bank names, locations, property types, and other entities.

Run standalone:  python -m pipeline.normalize
"""

import json
import re
import unicodedata
from pipeline.config import OUTPUT_DIR, LOOKUPS_DIR

LEXICAL_JSONL    = OUTPUT_DIR / "lexical.jsonl"
NORMALIZED_JSONL = OUTPUT_DIR / "normalized.jsonl"
REPORT_FILE      = OUTPUT_DIR / "normalization_report.txt"


# ── Load lookup tables ───────────────────────────────────────────────────────

def load_json(path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


BANK_LOOKUPS     = load_json(LOOKUPS_DIR / "bank_names.json")
LOCATION_LOOKUPS = load_json(LOOKUPS_DIR / "locations.json")
PROPTYPE_LOOKUPS = load_json(LOOKUPS_DIR / "property_types.json")


# ── Text cleaning utilities ──────────────────────────────────────────────────

def clean_text(text: str) -> str:
    """Basic text cleaning: normalize unicode, strip, collapse spaces."""
    if not text:
        return ""
    # Normalize unicode
    text = unicodedata.normalize("NFKC", text)
    # Strip trailing punctuation (dots, commas) and whitespace
    text = text.strip().rstrip(".,;:").strip()
    # Collapse multiple spaces
    text = re.sub(r"\s+", " ", text)
    return text


def title_case_name(text: str) -> str:
    """Title-case a name, preserving known abbreviations."""
    if not text:
        return ""
    # Don't title-case if already mixed case (e.g., "ARCIL", "HDFC")
    if text.isupper() and len(text) <= 6:
        return text
    return text.strip()


# ── Normalization functions ──────────────────────────────────────────────────

def normalize_bank_name(name: str) -> tuple[str, bool]:
    """Normalize bank name. Returns (normalized_name, was_changed)."""
    if not name:
        return name, False

    cleaned = clean_text(name)
    aliases = BANK_LOOKUPS.get("aliases", {})

    # Exact lookup
    if cleaned in aliases:
        return aliases[cleaned], True

    # Case-insensitive lookup
    lower_map = {k.lower(): v for k, v in aliases.items()}
    if cleaned.lower() in lower_map:
        return lower_map[cleaned.lower()], True

    # Fuzzy match
    match = fuzzy_match(cleaned, list(aliases.keys()))
    if match:
        return aliases[match], True

    return cleaned, False


def normalize_area(area: str) -> tuple[str, bool]:
    """Normalize area name. Returns (normalized_name, was_changed)."""
    if not area:
        return area, False

    cleaned = clean_text(area)
    aliases = LOCATION_LOOKUPS.get("areas", {})

    # Exact lookup
    if cleaned in aliases:
        return aliases[cleaned], True

    # Case-insensitive lookup
    lower_map = {k.lower(): v for k, v in aliases.items()}
    if cleaned.lower() in lower_map:
        return lower_map[cleaned.lower()], True

    # Remove "Taluk" suffix for matching, then check
    stripped = re.sub(r"\s+Taluk\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    if stripped.lower() in lower_map:
        return lower_map[stripped.lower()], True

    # Title case as default cleanup
    if cleaned != cleaned.title() and not cleaned.isupper():
        return cleaned.title(), True

    return cleaned, False


def normalize_city(city: str) -> tuple[str, bool]:
    """Normalize city name. Returns (normalized_name, was_changed)."""
    if not city:
        return city, False

    cleaned = clean_text(city)
    aliases = LOCATION_LOOKUPS.get("cities", {})

    if cleaned in aliases:
        return aliases[cleaned], True

    lower_map = {k.lower(): v for k, v in aliases.items()}
    if cleaned.lower() in lower_map:
        return lower_map[cleaned.lower()], True

    return cleaned, False


def normalize_property_type(ptype: str) -> tuple[str, bool]:
    """Normalize property type. Returns (normalized_name, was_changed)."""
    if not ptype:
        return ptype, False

    cleaned = clean_text(ptype)
    aliases = PROPTYPE_LOOKUPS.get("aliases", {})

    if cleaned in aliases:
        return aliases[cleaned], True

    lower_map = {k.lower(): v for k, v in aliases.items()}
    if cleaned.lower() in lower_map:
        return lower_map[cleaned.lower()], True

    return cleaned, False


def normalize_district(district: str) -> tuple[str, bool]:
    """Normalize district name."""
    if not district:
        return district, False

    cleaned = clean_text(district)
    aliases = LOCATION_LOOKUPS.get("districts", {})

    if cleaned in aliases:
        return aliases[cleaned], True

    lower_map = {k.lower(): v for k, v in aliases.items()}
    if cleaned.lower() in lower_map:
        return lower_map[cleaned.lower()], True

    # Remove "District" suffix
    stripped = re.sub(r"\s+District\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    if stripped != cleaned:
        return stripped, True

    return cleaned, False


def normalize_survey_number(sn: dict) -> dict:
    """Normalize survey number: strip spaces around /, clean up S.No. prefix."""
    if not sn or not isinstance(sn, dict):
        return sn

    result = dict(sn)
    survey_no = (result.get("survey_no") or "").strip()
    subdivision = (result.get("subdivision") or "").strip() if result.get("subdivision") else None

    # Strip spaces around /
    survey_no = re.sub(r"\s*/\s*", "/", survey_no)
    if subdivision:
        subdivision = re.sub(r"\s*/\s*", "/", subdivision)

    # Remove S.No. prefix variations
    survey_no = re.sub(r"^(?:S\.?\s*No\.?\s*|Survey\s*No\.?\s*)", "", survey_no, flags=re.IGNORECASE).strip()

    result["survey_no"] = survey_no
    result["subdivision"] = subdivision if subdivision else None
    return result


def normalize_village(village: str) -> tuple[str, bool]:
    """Normalize village name: remove 'Village' suffix, title case."""
    if not village:
        return village, False

    cleaned = clean_text(village)
    stripped = re.sub(r"\s+Village\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    if stripped != cleaned:
        return stripped.title(), True

    if cleaned != cleaned.title():
        return cleaned.title(), True

    return cleaned, False


def normalize_taluk(taluk: str) -> tuple[str, bool]:
    """Normalize taluk name: remove 'Taluk' suffix, title case."""
    if not taluk:
        return taluk, False

    cleaned = clean_text(taluk)
    stripped = re.sub(r"\s+Taluk\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    if stripped != cleaned:
        return stripped.title(), True

    if cleaned != cleaned.title():
        return cleaned.title(), True

    return cleaned, False


# ── Fuzzy matching ───────────────────────────────────────────────────────────

def fuzzy_match(query: str, candidates: list[str], threshold: int = 85) -> str | None:
    """Find best fuzzy match above threshold. Returns matched candidate or None."""
    try:
        from rapidfuzz import process, fuzz
        result = process.extractOne(query, candidates, scorer=fuzz.ratio, score_cutoff=threshold)
        if result:
            return result[0]
    except ImportError:
        pass
    return None


# ── Main normalization pipeline ──────────────────────────────────────────────

def normalize_record(record: dict) -> tuple[dict, list[str]]:
    """Normalize all entities in a lexical graph record.
    Returns (normalized_record, list_of_changes).
    """
    changes = []
    enriched = record.get("enriched_fields", {})

    # Normalize location fields in enriched_fields
    for field, normalizer in [
        ("village", normalize_village),
        ("taluk", normalize_taluk),
        ("district", normalize_district),
        ("registration_district", lambda x: normalize_district(x)),
        ("registration_sub_district", lambda x: normalize_district(x)),
    ]:
        if field in enriched:
            new_val, changed = normalizer(enriched[field])
            if changed:
                changes.append(f"{field}: '{enriched[field]}' -> '{new_val}'")
                enriched[field] = new_val

    # Normalize mentions
    for mention in record.get("mentions", []):
        if mention["entity_type"] in ("Village",):
            new_val, changed = normalize_village(mention["value"])
            if changed:
                changes.append(f"mention {mention['entity_type']}: '{mention['value']}' -> '{new_val}'")
                mention["value"] = new_val

        elif mention["entity_type"] in ("Taluk",):
            new_val, changed = normalize_taluk(mention["value"])
            if changed:
                changes.append(f"mention {mention['entity_type']}: '{mention['value']}' -> '{new_val}'")
                mention["value"] = new_val

        elif mention["entity_type"] in ("District", "RegistrationDistrict", "RegistrationSubDistrict"):
            new_val, changed = normalize_district(mention["value"])
            if changed:
                changes.append(f"mention {mention['entity_type']}: '{mention['value']}' -> '{new_val}'")
                mention["value"] = new_val

    record["enriched_fields"] = enriched
    return record, changes


def normalize_entities():
    """Run normalization on all lexical graph records."""
    if not LEXICAL_JSONL.exists():
        print("No lexical.jsonl found. Run lexical_graph first.")
        return

    count = 0
    total_changes = 0
    unmatched = []

    with open(LEXICAL_JSONL, "r", encoding="utf-8") as f_in, \
         open(NORMALIZED_JSONL, "w", encoding="utf-8") as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            normalized, changes = normalize_record(record)
            f_out.write(json.dumps(normalized, ensure_ascii=False) + "\n")
            count += 1
            total_changes += len(changes)
            if changes:
                unmatched.extend(
                    f"  [{record.get('auction_id', '?')}] {c}" for c in changes
                )

    # Write report
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(f"Normalization Report\n{'='*50}\n")
        f.write(f"Records processed: {count}\n")
        f.write(f"Total changes: {total_changes}\n\n")
        if unmatched:
            f.write("Changes:\n")
            for line in unmatched:
                f.write(line + "\n")

    print(f"Normalization: {count} records, {total_changes} changes")
    print(f"  Output: {NORMALIZED_JSONL}")
    print(f"  Report: {REPORT_FILE}")


if __name__ == "__main__":
    normalize_entities()
