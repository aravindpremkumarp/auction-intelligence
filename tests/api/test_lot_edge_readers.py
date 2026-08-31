"""Phase 2: readers resolve a listing through the edge, not the string.

`resolved_lot_key` is "<filename>#<lot_index>", and lot_index is the
extraction model's own numbering. Re-extract a notice and the lots renumber,
so a stale key still RESOLVES — to a different property. Reading the edge
makes that impossible, because it names the node instead of naming a way to
find one.

Source inspection rather than a live database: these are Cypher strings, and
the thing under test is which clause they use.
"""
from __future__ import annotations

import inspect

import pytest

from api.agent3 import (
    find_properties, get_property, identifiers, reauction_history,
    search_notices,
)
import api.review.queries as q

_READERS = (get_property, identifiers, find_properties, reauction_history,
            search_notices)

_EDGE_READ = "[(a)-[:IS_LOT]->(_lot:Lot) | _lot.lot_key][0] AS resolved_lot_key"


@pytest.mark.parametrize("mod", _READERS, ids=lambda m: m.__name__.split(".")[-1])
def test_the_lot_comes_from_the_edge(mod):
    src = inspect.getsource(mod)
    assert _EDGE_READ in src, f"{mod.__name__} still reads the string"


@pytest.mark.parametrize("mod", _READERS, ids=lambda m: m.__name__.split(".")[-1])
def test_no_reader_still_selects_the_string(mod):
    """The two agree today, so a leftover would pass every test and quietly
    keep the old failure mode alive."""
    assert "a.resolved_lot_key AS resolved_lot_key" not in inspect.getsource(mod)


def test_the_field_name_is_unchanged():
    """Phase 2 swaps the SOURCE, not the shape.

    Every caller still reads `resolved_lot_key`; renaming it here would turn
    a contained migration into an API change.
    """
    assert _EDGE_READ.endswith("AS resolved_lot_key")


# ── the review app ───────────────────────────────────────────────────────────

def test_the_price_queue_joins_the_lot_through_the_edge():
    src = inspect.getsource(q._price_checks)
    assert "(p)-[:IS_LOT]->(keyed:Lot)" in src
    assert "(keyed:Lot {lot_key: p.resolved_lot_key})" not in src


def test_the_price_queue_does_not_chain_lot_to_auction():
    """A lot with no :Auction still names the lot.

    Chaining them blanked lot_key on 12 of the 65 rows that have one — the
    string it replaced was read straight off the listing and never had that
    dependency.
    """
    src = inspect.getsource(q._price_checks)
    assert "OPTIONAL MATCH (p)-[:IS_LOT]->(keyed:Lot)\n" in src
    assert "OPTIONAL MATCH (keyed)-[:OFFERED_IN]->(ka:Auction)" in src


def test_the_price_queue_carries_keyed_past_the_aggregation():
    """An aggregating WITH drops anything it does not list.

    Leaving `keyed` out made the whole query a 400 at runtime, which no
    source-shape assertion above would have caught.
    """
    src = inspect.getsource(q._price_checks)
    agg = "WITH p, d, ka, keyed, count(DISTINCT any) AS lot_count"
    assert agg in src
    assert src.index(agg) < src.index("keyed.lot_key AS lot_key")


def test_unresolved_means_no_edge():
    """A key whose :Lot does not exist used to read as resolved and keep the
    listing OFF this queue — the one place a broken pointer must not hide."""
    src = inspect.getsource(q._lot_match_candidates)
    assert "NOT (p)-[:IS_LOT]->(:Lot)" in src
    assert "p.resolved_lot_key IS NULL" not in src


# ── keeping the edge in step with the key ────────────────────────────────────

def test_apply_extractions_links_after_writing_keys():
    """run_pipeline calls apply_extractions and never calls promote.

    Leaving the edge to a manual promote run would make a resolution
    invisible to every Phase 2 reader until someone remembered.
    """
    from pipeline import apply_extractions as AX
    src = inspect.getsource(AX.run)
    assert "link_lots(dry_run=False)" in src
    assert src.index("write_lot_matches(") < src.index("link_lots(dry_run=False)")


def test_the_link_import_is_local_to_avoid_a_cycle():
    """promote_extractions imports from apply_extractions, so the reverse
    import cannot sit at module scope."""
    from pipeline import apply_extractions as AX
    assert "from pipeline.promote_extractions import link_lots" in \
        inspect.getsource(AX.run)
    assert "from pipeline.promote_extractions import" not in \
        inspect.getsource(AX).split("def ")[0]
