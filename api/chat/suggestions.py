"""
api/chat/suggestions.py
-----------------------
Data-driven starter chips for the chat landing. The web UI used to ship four
hardcoded suggestion strings ("flats in Chennai under 50 lakhs", …) that a
human wrote once; they drift as the loader ingests and expires auctions, so a
chip can end up pointing at zero live matches — the same not-found trap the
`/modes` example ids were rewritten to avoid.

This builds the chips from the live graph instead. The router feeds in
single-dimension distributions (value -> auction_count buckets) from
`search_auctions(group_by=...)`, which is future-only by default, so a chip's
count matches what a click actually returns and every chip is guaranteed
non-empty. Everything here is pure (no I/O) so it's dependency-free
unit-testable; the router owns the Neo4j calls and the TTL cache.
"""
from __future__ import annotations

from typing import Any

# Only surface a bucket that actually has live matches — the whole point is
# chips that can't dead-end.
_MIN_COUNT = 1
# The landing shows four chips; fill up to this many and stop.
_MAX_CHIPS = 4

# Slots to fill, in priority order. Repeated dimensions mean "take another
# distinct value from that dimension" (a second city, a second property type),
# so a graph with rich data yields a varied mix — place, type, category — while
# a thin one degrades gracefully by falling through to whatever buckets exist.
# Every dimension here is one `search_auctions(group_by=...)` distribution.
_PICK_ORDER: tuple[str, ...] = (
    "city",
    "property_type",
    "asset_category",
    "property_type",
    "city",
    "area",
)


def _category_adjective(value: str) -> str:
    """AssetCategory enum -> a word that reads right before "properties".
    Only "Industrials" is stored plural; the rest already fit."""
    return "Industrial" if value == "Industrials" else value


def _chip_for(dim: str, value: str, count: int) -> dict[str, Any]:
    """One chip: `label` is what the pill shows, `q` is the chat question a
    click sends (the agent expands phrasing / is case-insensitive, so plain
    natural language is enough), `count` is the live match count for the UI."""
    if dim == "city":
        return {"label": f"Auctions in {value}", "q": f"auctions in {value}", "count": count}
    if dim == "area":
        return {"label": f"Properties in {value}", "q": f"properties in {value}", "count": count}
    if dim == "property_type":
        return {"label": f"{value} listings", "q": f"{value.lower()} listings", "count": count}
    if dim == "asset_category":
        adj = _category_adjective(value)
        return {"label": f"{adj} properties", "q": f"{adj.lower()} properties", "count": count}
    if dim == "bank":
        return {"label": f"{value} auctions", "q": f"{value} auctions", "count": count}
    # Unknown dimension: fall back to the bare value so we never emit an empty chip.
    return {"label": value, "q": value, "count": count}


def build_suggestions(
    distributions: dict[str, list[dict]],
    max_chips: int = _MAX_CHIPS,
) -> list[dict[str, Any]]:
    """Assemble up to `max_chips` chips from per-dimension distributions.

    `distributions` maps a group_by dimension ("city", "property_type", …) to
    its buckets `[{"value": str, "auction_count": int}, ...]`, ordered by count
    descending (as `search_auctions` returns them). Walks `_PICK_ORDER`, taking
    the next unused, positive-count bucket from each named dimension; skips
    empty/zero buckets and de-duplicates by question text. Returns [] when
    nothing usable is present, which the frontend reads as "keep the hardcoded
    fallback chips".
    """
    cursors: dict[str, int] = {}
    seen_q: set[str] = set()
    chips: list[dict[str, Any]] = []
    for dim in _PICK_ORDER:
        if len(chips) >= max_chips:
            break
        dist = distributions.get(dim) or []
        i = cursors.get(dim, 0)
        while i < len(dist):
            bucket = dist[i]
            i += 1
            value = str(bucket.get("value") or "").strip()
            count = bucket.get("auction_count") or 0
            if not value or not isinstance(count, int) or count < _MIN_COUNT:
                continue
            chip = _chip_for(dim, value, count)
            key = chip["q"].lower()
            if key in seen_q:
                continue
            seen_q.add(key)
            chips.append(chip)
            break
        cursors[dim] = i
    return chips
