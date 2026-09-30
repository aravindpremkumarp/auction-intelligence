from __future__ import annotations

from pipeline.reader.migrate import migrate
from pipeline.reader.schema import SCHEMA_VERSION


def test_langextract_era_entities_gain_defaults_only():
    ents = [{"id": "0", "cls": "auction_terms", "text": "Rs.1", "start": 3, "end": 7, "attrs": {"lot_index": "1"}},
            {"id": "1", "cls": "location", "text": "x", "start": None, "end": None, "attrs": {}}]
    out = migrate(ents, None)
    assert out[0]["attrs"]["evidence"] == "EXPLICIT" and out[0]["attrs"]["reader"] == "langextract"
    assert out[1]["attrs"]["evidence"] == "UNGROUNDED"
    assert out[0]["attrs"]["lot_index"] == "1"           # nothing lost
    assert migrate(out, SCHEMA_VERSION) is out            # current version: untouched
    assert migrate(out, None)[0]["attrs"]["evidence"] == "EXPLICIT"   # idempotent


def test_future_version_passes_through():
    ents = [{"cls": "x", "attrs": {"evidence": "VERIFIED"}}]
    assert migrate(ents, SCHEMA_VERSION + 5) is ents
