"""
api/agent3/find_by_identifier.py
---------------------------------
Survey / patta / door / plot / flat number -> every listing whose notice
mentions it.

"Is survey number 331/1 in any auction notice" is a question the old tool
surface cannot answer at all — there is no field to filter on, because the
number lives in a fulltext-indexed `Identifier` node, not a listing property.
This is a direct lookup, not a filtered search, which is why it is its own
tool rather than another `find_properties` parameter (that parameter still
exists, for combining an identifier with other filters — see
`api/agent3/identifiers.py`, which both share).

Returns which identifier matched (kind + the notice's own spelling), which
lot it was found on, and whether other listings share the same underlying
notice — a survey number match is worth nothing if the agent cannot say
which property it belongs to and whether that claim is safe to make.
"""
from __future__ import annotations

from api.agent3.common import ToolInputError, clamp_limit, scope_note, scope_of, tool
from api.agent3.identifiers import resolve_identifier_detail

#: A blank or single-character query is almost certainly a typo in progress,
#: not a real identifier — the fulltext index would otherwise return noise.
_MIN_QUERY_CHARS = 2


@tool
def find_by_identifier(value: str, identifier_kind: str | None = None,
                       limit: int = 20) -> dict:
    """Look up a survey, patta, door, plot, flat or CERSAI number.

    Returns every listing whose sale notice mentions it, with the matched
    identifier's own spelling, which lot it sits on, and the notice's total
    lot count so you know whether to report the match as this property's own
    or as something the shared notice mentions (see `scope`/`scope_note` on
    each row — same discipline as `get_property`).

    `identifier_kind` narrows to one identifier type when the same number
    could be a survey number on one lot and a door number on another — same
    parameter name and values as `find_properties`' `identifier_kind`:
      survey_old, survey_new, patta, plot, door_old, door_new, sale_deed,
      approved_layout, property_id, flat, assessment_old, assessment_new,
      block, cersai, floor, ward_no, chitta, khata

    A zero-result answer here means the number is not in this graph's
    notices — it does NOT mean the property doesn't exist; report it as a
    graph gap, not as "no such property".
    """
    raw = (value or "").strip()
    if len(raw) < _MIN_QUERY_CHARS:
        raise ToolInputError(
            f"value={value!r} is too short to search — give the full number.")
    limit = clamp_limit(limit, default=20)

    rows = resolve_identifier_detail(raw, identifier_kind, limit)
    if not rows:
        return {
            "query": raw, "matches": [],
            "hint": (f"No notice in this graph mentions {raw!r}"
                     + (f" as a {identifier_kind}" if identifier_kind else "") +
                     ". This means the graph has no record of it, not that "
                     "the property doesn't exist — say so plainly."),
        }

    # A matched Identifier can be reached from several listings when they
    # share one underlying notice (a bank sometimes lists the same multi-lot
    # notice as several portal rows) — group so the agent reports one finding
    # with several listing ids, not several unrelated-looking hits.
    by_value: dict[tuple[str, str], dict] = {}
    for r in rows:
        lot_count = r.get("lot_count") or 0
        scope = scope_of(lot_count)
        key = (r["matched_kind"], r["matched_value"])
        group = by_value.setdefault(key, {
            "identifier_kind": r["matched_kind"],
            "identifier_value": r["matched_value"],
            "listings": [],
        })
        listing = {
            "auction_id": r["auction_id"],
            "title": r.get("title"),
            "city": r.get("city"), "bank": r.get("bank"),
            "matched_on_lot": r.get("lot_key"),
            "lot_property_type": r.get("lot_property_type"),
            "notice_lot_count": lot_count,
            "scope": scope,
        }
        note = scope_note("this identifier's exact location on the property", lot_count)
        if note:
            listing["scope_note"] = note
        group["listings"].append({k: v for k, v in listing.items() if v is not None})

    matches = list(by_value.values())
    for m in matches:
        m["listing_count"] = len(m["listings"])

    return {
        "query": raw,
        "matches": matches,
        "total_listings": sum(m["listing_count"] for m in matches),
    }
