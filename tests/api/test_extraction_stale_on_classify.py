"""Classification review must mark the extraction stale when it invalidates it.

The LangExtract prompt is built from ``Document.expected_lot_count``
(pipeline.langextract_examples.prompt_description_for) and the model is chosen
from ``notice_type`` (pipeline.extract_routing). So the moment review changes
either, the stored extraction was produced by a prompt and a model that no
longer apply — and nothing else in the system notices. Before this, 582 human
lot counts sat in the graph with only 4 documents queued for re-extraction: the
reviewer's work reached the database and stopped there.

These are source-level guards. The behaviour itself was verified against the
live graph (both writes exercised on a throwaway node across the change /
no-change / no-extraction / type-flip cases); what regresses silently is the
*shape* of the query, so that is what is pinned here.
"""
from __future__ import annotations

import inspect
import re

from api.review import queries as Q

_VERIFY = inspect.getsource(Q.verify_classification)
_BULK = inspect.getsource(Q.auto_confirm_classifications)


def _cypher(src: str) -> str:
    """Strip Cypher line comments so a rule can't be 'satisfied' by prose."""
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.splitlines())


def _stale_case(src: str) -> str:
    """The whole `d.extraction_stale_at = CASE ... END` expression.

    Not a naive split on the property name: it appears twice (the assignment
    target and the ELSE that preserves it), so splitting would cut the ELSE off
    and quietly pass a test that meant to check for it.
    """
    body = _cypher(src)
    m = re.search(r"d\.extraction_stale_at\s*=\s*CASE.*?END", body, re.S)
    assert m, "no `d.extraction_stale_at = CASE ... END` found"
    return m.group(0)


# ── verify_classification ───────────────────────────────────────────────────

def test_verify_stamps_the_stale_marker():
    assert "d.extraction_stale_at" in _cypher(_VERIFY)


def test_verify_reads_prior_values_before_the_set():
    """The marker keys off what changed, so the old values must be captured in
    a WITH ahead of the SET — reading them afterwards would compare a value
    against itself and never fire."""
    body = _cypher(_VERIFY)
    with_at = body.index("d.expected_lot_count AS prior_elc")
    set_at = body.index("SET d.notice_type")
    assert with_at < set_at, "prior_elc must be captured before the SET"
    assert "d.notice_type AS prior" in body


def test_verify_fires_on_either_lot_count_or_type_change():
    body = _cypher(_VERIFY)
    assert "prior <> $nt" in body
    assert "prior_elc" in _stale_case(_VERIFY)


def test_verify_only_marks_documents_that_have_an_extraction():
    """Nothing to redo when there is no extraction; queuing one would just
    inflate the stale count."""
    assert "d.extraction_json IS NOT NULL" in _stale_case(_VERIFY)


def test_verify_preserves_the_marker_when_nothing_changed():
    """The CASE must have an ELSE that keeps any existing value — re-confirming
    an unchanged notice must not clear a marker set earlier."""
    assert "ELSE d.extraction_stale_at" in _stale_case(_VERIFY)


# ── auto_confirm_classifications (bulk) ─────────────────────────────────────

def test_bulk_stamps_the_stale_marker():
    assert "d.extraction_stale_at" in _cypher(_BULK)


def test_bulk_reads_the_prior_count_before_the_set():
    """Bulk-confirm back-fills expected_lot_count=1 for singles in the same SET
    clause. Reading the property afterwards would race that assignment, so the
    prior value is captured in a WITH first."""
    body = _cypher(_BULK)
    assert "d.expected_lot_count AS prior_elc" in body
    assert body.index("prior_elc") < body.index("SET d.notice_type_verified_at")


def test_bulk_only_marks_rows_it_actually_gave_a_count():
    """A single that already had a count is unchanged by this write and must
    not be queued for a pointless re-extraction."""
    case = _stale_case(_BULK)
    assert "prior_elc IS NULL" in case
    assert "d.extraction_json IS NOT NULL" in case


def test_bulk_preserves_the_marker_when_nothing_changed():
    assert "ELSE d.extraction_stale_at" in _stale_case(_BULK)


# ── the consumer ────────────────────────────────────────────────────────────

def test_the_marker_is_what_the_reextract_script_selects_on():
    """The whole point: this marker has a reader. If that selector is renamed,
    these writes become dead and the loop silently reopens."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2]
           / "scripts" / "reset_langextract_and_extract.py").read_text(encoding="utf-8")
    assert "d.extraction_stale_at IS NOT NULL" in src
