"""Unit tests for the pure helpers in scripts/copy_aura_to_community.py.

The end-to-end copy was verified against two real Neo4j 5.26 containers;
these cover the string/schema logic that would silently corrupt a copy.
"""

from scripts.copy_aura_to_community import q, schema_statements


def test_quote_escapes_backticks():
    assert q("AuctionProperty") == "`AuctionProperty`"
    assert q("we`ird") == "`we``ird`"


def test_schema_statements_skips_lookup_and_constraint_owned_indexes():
    constraints = [{"name": "auction_id", "createStatement": "CREATE CONSTRAINT `auction_id` ..."}]
    indexes = [
        {"name": "index_1", "type": "LOOKUP", "owningConstraint": None, "createStatement": "CREATE LOOKUP INDEX ..."},
        {"name": "auction_id", "type": "RANGE", "owningConstraint": "auction_id", "createStatement": "x"},
        {"name": "lot_description_ft", "type": "FULLTEXT", "owningConstraint": None, "createStatement": "CREATE FULLTEXT INDEX ..."},
        {"name": "property_desc_idx", "type": "VECTOR", "owningConstraint": None, "createStatement": "CREATE VECTOR INDEX ..."},
    ]
    names = [n for n, _ in schema_statements(constraints, indexes)]
    # constraints first, then standalone indexes; LOOKUP + owned index dropped
    assert names == ["auction_id", "lot_description_ft", "property_desc_idx"]
