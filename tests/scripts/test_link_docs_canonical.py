"""
tests/scripts/test_link_docs_canonical.py
------------------------------------------
Covers storage-key resolution in scripts/link_docs_to_neo4j.

Two failures this guards against, both seen in the live graph:

* The linker assumed a notice lives at ``notices/<this aid>/<file>``. A batch
  notice covering N properties is stored once, so N-1 of them failed the R2
  existence gate and were silently dropped -- 901 of 2,753 pairs, every one of
  them a shared notice.
* Resolving to a different copy than the one an extracted Document already
  points at makes MERGE create a second, empty Document beside the one holding
  the extraction output -- 389 of those were created before this was fixed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(scope="module")
def link():
    pytest.importorskip("neo4j")
    for var, val in [("NEO4J_URI", "bolt://x"), ("NEO4J_USERNAME", "u"),
                     ("NEO4J_PASSWORD", "p"), ("NEO4J_DATABASE", "neo4j"),
                     ("R2_PUBLIC_BASE_URL", "https://r2.example")]:
        os.environ.setdefault(var, val)
    import scripts.link_docs_to_neo4j as mod
    return mod


def _row(aid: str, fname: str) -> dict:
    return {
        "auction_id": aid,
        "filename": fname,
        "storage_key": f"notices/{aid}/{fname}",
        "public_url": f"https://r2.example/notices/{aid}/{fname}",
        "file_type": "pdf",
    }


def test_shared_notice_is_remapped_not_dropped(link):
    """The 901-pair regression: siblings must survive the R2 gate."""
    rows = [_row("100", "batch.pdf"), _row("101", "batch.pdf"), _row("102", "batch.pdf")]
    r2 = {"notices/100/batch.pdf"}                      # stored once

    resolved, dropped, remapped = link.resolve_storage_keys(rows, r2, {})

    assert dropped == 0
    assert remapped == 2
    assert {r["storage_key"] for r in resolved} == {"notices/100/batch.pdf"}
    assert all(r["public_url"].endswith("notices/100/batch.pdf") for r in resolved)


def test_prefers_the_key_an_extracted_document_already_uses(link):
    """Choosing the other copy would MERGE an empty Document beside the real one."""
    rows = [_row("777", "n.pdf")]
    r2 = {"notices/100/n.pdf", "notices/900/n.pdf"}
    existing = {"n.pdf": "notices/900/n.pdf"}           # extraction output lives here

    resolved, _, _ = link.resolve_storage_keys(rows, r2, existing)

    assert resolved[0]["storage_key"] == "notices/900/n.pdf"


def test_falls_back_to_lowest_key_when_no_document_exists(link):
    rows = [_row("777", "n.pdf")]
    r2 = {"notices/900/n.pdf", "notices/100/n.pdf"}

    resolved, _, _ = link.resolve_storage_keys(rows, r2, {})

    assert resolved[0]["storage_key"] == "notices/100/n.pdf"


def test_choice_does_not_depend_on_set_iteration_order(link):
    keys = ["notices/300/n.pdf", "notices/100/n.pdf", "notices/200/n.pdf"]
    a = link.pick_canonical_keys(set(keys), {})
    b = link.pick_canonical_keys(set(reversed(keys)), {})
    assert a == b == {"n.pdf": "notices/100/n.pdf"}


def test_row_whose_own_key_exists_is_left_alone(link):
    rows = [_row("100", "solo.pdf")]
    resolved, dropped, remapped = link.resolve_storage_keys(
        rows, {"notices/100/solo.pdf"}, {})
    assert (dropped, remapped) == (0, 0)
    assert resolved[0]["storage_key"] == "notices/100/solo.pdf"


def test_file_absent_from_r2_is_still_dropped(link):
    """The gate exists to stop Documents whose public_url 404s. Keep it."""
    rows = [_row("100", "never-uploaded.pdf")]
    resolved, dropped, remapped = link.resolve_storage_keys(rows, set(), {})
    assert resolved == [] and dropped == 1 and remapped == 0
