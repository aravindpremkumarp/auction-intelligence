"""Forward migrations for stored extraction entities across schema versions.

Every ``Document.extraction_json`` written by the reader is stamped with
``extraction_schema_version``. When a field's meaning or name changes,
SCHEMA_VERSION bumps and a migration is added here, so old blobs are read
forward instead of re-extracted. Migrations are additive (they never delete a
value) and idempotent (running one twice is harmless), which is what lets an
old reader keep working on a new blob and a new reader on an old one.

Version 0 is the LangExtract-era blob: entities with no evidence attrs at all.
Migration 1 gives them the defaults the v2 consumers read, without claiming
any evidence the old reader did not have.
"""
from __future__ import annotations

from typing import Callable

from pipeline.reader.schema import SCHEMA_VERSION

Migration = Callable[[list[dict]], list[dict]]


def _to_1(ents: list[dict]) -> list[dict]:
    out = []
    for e in ents:
        if not isinstance(e, dict):
            continue
        attrs = dict(e.get("attrs") or {})
        attrs.setdefault("reader", "langextract")
        # An old entity's evidence is exactly its span: EXPLICIT when grounded,
        # and nothing claimed when it was not.
        if "evidence" not in attrs:
            attrs["evidence"] = "EXPLICIT" if e.get("start") is not None else "UNGROUNDED"
        out.append({**e, "attrs": attrs})
    return out


MIGRATIONS: dict[int, Migration] = {1: _to_1}


def migrate(entities: list[dict], from_version: int | None) -> list[dict]:
    """``entities`` brought forward from ``from_version`` (None / 0 = the
    LangExtract era) to SCHEMA_VERSION. Unknown future versions pass through."""
    v = int(from_version or 0)
    if v >= SCHEMA_VERSION:
        return entities
    out = entities
    for step in range(v + 1, SCHEMA_VERSION + 1):
        fn = MIGRATIONS.get(step)
        if fn is not None:
            out = fn(out)
    return out
