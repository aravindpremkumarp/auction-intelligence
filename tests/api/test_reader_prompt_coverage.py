"""The v2 prompt is rendered from the schema: every field reaches the model,
the rules that matter are stated, and the hash changes with the prompt."""
from __future__ import annotations

import re

from pipeline.reader import prompt as P
from pipeline.reader import schema as S


def test_every_schema_field_is_in_the_segment_and_notice_prompts():
    seg, notice = P.system_prompt("segment"), P.system_prompt("notice")
    for path in S.all_field_paths():
        fld = path.split(".")[1]
        assert f"  - {fld} (" in seg and f"  - {fld} (" in notice, path


def test_the_rules_that_define_the_reader_are_stated():
    seg = P.system_prompt("segment")
    for must in ("not_stated", "Never infer a district", "ILLEGIBLE IS A STATUS",
                 "NO ARITHMETIC", "ONE LOT, ONE SET OF FACTS", "LONG BLOCKS ARE ANCHORS",
                 "REGISTRATION DISTRICTS", "uds_parent_extent", "POSSESSION", "SHARED FACTS"):
        assert must in seg, must
    assert "SINGLE JSON object" not in seg and "MinerU" not in seg
    assert "extraction_text" not in seg          # no v1 vocabulary leaks in


def test_prompt_hash_is_stable_and_tracks_the_schema_version():
    assert re.fullmatch(rf"v2-s{S.SCHEMA_VERSION}-[0-9a-f]{{10}}", P.PROMPT_HASH)
    assert P._hash() == P.PROMPT_HASH


def test_roster_block_never_licenses_copying_values():
    rb = P.roster_block([{"aid": "796269", "reserve": 5000000, "village": "Kelambakkam"}])
    assert "listing 796269:" in rb and "NEVER copy a value" in rb and "portal_aid" in rb
    assert P.roster_block([]) == "" and P.roster_block(None) == ""


def test_lot_count_hints():
    assert "EXACTLY ONE lot" in P.lot_count_hint(1)
    assert "EXACTLY 6 lots" in P.lot_count_hint(6)
    assert "holds 3 lot(s)" in P.lot_count_hint(6, in_chunk=3)
    assert P.lot_count_hint(None) == ""
    assert "exactly 2 entries" in P.user_keyfacts("x", 2)
    assert "Fields needed: extents, location" in P.user_reread("x", ["extents", "location"])


def test_every_kind_has_a_prompt_and_unknown_kinds_fail():
    for k in ("segment", "notice", "boundaries", "keyfacts", "reread"):
        assert P.system_prompt(k).startswith("You ")
    import pytest
    with pytest.raises(ValueError):
        P.system_prompt("nope")
