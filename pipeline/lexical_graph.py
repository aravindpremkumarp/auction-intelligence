"""
pipeline/lexical_graph.py
-------------------------
Stage 2: Build intermediate mention graph from extracted data (PRD 5.7).

Reads extracted.jsonl and produces lexical.jsonl with mention triples
and enrichment metadata per auction record.

Run standalone:  python -m pipeline.lexical_graph
"""

import json
from pipeline.config import OUTPUT_DIR

EXTRACTED_JSONL = OUTPUT_DIR / "extracted.jsonl"
LEXICAL_JSONL   = OUTPUT_DIR / "lexical.jsonl"


def build_mentions(extracted: dict) -> list[dict]:
    """Convert extracted fields into mention triples."""
    mentions = []

    # Undivided share
    if extracted.get("undivided_share"):
        mentions.append({
            "entity_type": "UndividedShare",
            "value": extracted["undivided_share"],
            "source": "image",
        })

    # Total area
    if extracted.get("total_area"):
        mentions.append({
            "entity_type": "TotalArea",
            "value": extracted["total_area"],
            "source": "image",
        })

    # Location entities
    for field in ("village", "taluk", "district", "registration_district", "registration_sub_district"):
        if extracted.get(field):
            mentions.append({
                "entity_type": field.replace("_", " ").title().replace(" ", ""),
                "value": extracted[field],
                "source": "image",
            })

    # Boundaries
    boundaries = extracted.get("boundaries") or {}
    for direction in ("north", "south", "east", "west"):
        if boundaries.get(direction):
            mentions.append({
                "entity_type": "Boundary",
                "direction": direction,
                "value": boundaries[direction],
                "source": "image",
            })

    # Door numbers
    door_numbers = extracted.get("door_numbers") or {}
    for num_type in ("old", "new"):
        for num in (door_numbers.get(num_type) or []):
            if num:
                mentions.append({
                    "entity_type": "DoorNumber",
                    "number_type": num_type,
                    "value": num,
                    "source": "image",
                })

    return mentions


def build_enriched_fields(extracted: dict) -> dict:
    """Extract key enrichment fields for easy access."""
    fields = {}
    for key in ("undivided_share", "total_area",
                "village", "taluk", "district",
                "registration_district", "registration_sub_district"):
        if extracted.get(key):
            fields[key] = extracted[key]

    # Flatten boundaries
    boundaries = extracted.get("boundaries") or {}
    for direction in ("north", "south", "east", "west"):
        if boundaries.get(direction):
            fields[f"boundary_{direction}"] = boundaries[direction]

    # Flatten door numbers
    door_numbers = extracted.get("door_numbers") or {}
    old_nums = door_numbers.get("old") or []
    new_nums = door_numbers.get("new") or []
    if old_nums:
        fields["door_numbers_old"] = old_nums
    if new_nums:
        fields["door_numbers_new"] = new_nums

    return fields


def build_lexical_graph():
    """Read extracted.jsonl and produce lexical.jsonl."""
    if not EXTRACTED_JSONL.exists():
        print("No extracted.jsonl found. Run ocr_extract first.")
        return

    count = 0
    with open(EXTRACTED_JSONL, "r", encoding="utf-8") as f_in, \
         open(LEXICAL_JSONL, "w", encoding="utf-8") as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            extracted = record.get("extracted", {})
            mentions = build_mentions(extracted)
            enriched = build_enriched_fields(extracted)

            output = {
                "auction_id": record["auction_id"],
                "url": record.get("url"),
                "mentions": mentions,
                "enriched_fields": enriched,
                "cross_reference": record.get("cross_reference", {}),
            }

            f_out.write(json.dumps(output, ensure_ascii=False) + "\n")
            count += 1

    print(f"Lexical graph: {count} records written to {LEXICAL_JSONL}")


if __name__ == "__main__":
    build_lexical_graph()
