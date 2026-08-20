"""
tests/api/test_chat_v2_tools.py
-------------------------------
Two claims worth pinning about the v2 tool surface:

  * tool NAMES match v1 exactly, so the golden catalogue scores v2 with the
    same tool-trajectory assertions and no alias map (the spike needed one,
    and it hid a real routing difference);
  * the enum values the planner reads are RENDERED from the same constants
    the tools validate against, so a new enum cannot drift out of the prompt.
    The first narrowing run failed precisely because the planner put a
    category value on `property_type` and ran a whole conversation on zero
    rows.
"""
from __future__ import annotations

from evals.cases import KNOWN_TOOLS

from api.chat.v2 import tools
from api.tools.cypher_tools import _AGG_FIELDS, _AGG_FUNCS, _ORDER_BY_CLAUSES


def test_tool_names_match_the_eval_catalogue():
    assert set(tools.ALL_TOOLS) == KNOWN_TOOLS


def test_planner_cannot_reach_cypher_directly():
    """The planner has not seen the schema, so it asks for tier 3 by
    signalling rather than emitting run_cypher blind — the same on-demand
    shape as v1's deferred `cypher` capability."""
    assert "run_cypher" not in tools.PLANNER_TOOLS
    assert "describe_schema" not in tools.PLANNER_TOOLS
    assert set(tools.CYPHER_TOOLS) == {"run_cypher", "describe_schema"}


def test_catalogue_carries_the_real_order_by_values():
    catalogue = tools.render_catalogue()
    for value in _ORDER_BY_CLAUSES:
        assert value in catalogue


def test_catalogue_carries_the_real_aggregation_values():
    catalogue = tools.render_catalogue()
    for value in {*_AGG_FIELDS, *_AGG_FUNCS}:
        assert value in catalogue


def test_catalogue_separates_category_from_type():
    """The exact confusion that produced a zero-row conversation."""
    # Normalized: the docstring is hard-wrapped, and line breaks must not
    # decide whether a content assertion passes.
    catalogue = " ".join(tools.render_catalogue().split())
    assert "asset_category is the broad class" in catalogue
    assert "Putting a category value on property_type returns zero rows" in catalogue
    for value in tools.ASSET_CATEGORIES:
        assert value in catalogue


def test_catalogue_lists_every_parameter():
    catalogue = tools.render_catalogue()
    for param in ("min_emd", "max_emd", "auction_type", "branch_name",
                  "service_provider", "deadline_within_days"):
        assert param in catalogue, f"{param} missing — the spike dropped these five"


def test_model_visible_errors_returns_data():
    @tools.model_visible_errors
    def boom():
        raise ValueError("aggregate_field must be one of [...]")

    assert boom() == {"error": "aggregate_field must be one of [...]"}


def test_model_visible_errors_lets_real_bugs_raise():
    """Only ValueError/TypeError are the model getting an argument wrong. A
    driver failure is a bug and must not be laundered into a tool result."""
    @tools.model_visible_errors
    def boom():
        raise RuntimeError("driver went away")

    try:
        boom()
    except RuntimeError:
        return
    raise AssertionError("RuntimeError should propagate")


def test_iso_string_coercion():
    from datetime import datetime, timezone
    assert tools._dt(None) is None
    assert tools._dt("2026-08-20T00:00:00Z") == datetime(2026, 8, 20, tzinfo=timezone.utc)
    already = datetime(2026, 8, 20, tzinfo=timezone.utc)
    assert tools._dt(already) is already


def test_tools_do_not_use_the_pydantic_ai_splitter():
    """`split_ui_overflow` returns a pydantic-ai ToolReturn, which is
    meaningless outside v1's agent. v2 splits the UI rows in the executor
    instead, so the model-visible result and the panel rows are separated
    exactly once."""
    import inspect

    source = inspect.getsource(tools)
    assert "split_ui_overflow(" not in source.replace("split_ui_overflow`", "")


def test_detail_batch_truncation_is_reported_not_silent(monkeypatch):
    """Observed live: the model asked for 15 ids, got 10 back, and wrote "this
    applies to all 15 properties". Silent truncation reads as full coverage."""
    monkeypatch.setattr("api.tools.cypher_tools.get_auction_details",
                        lambda ids: {"results": [{"auction_id": i} for i in ids],
                                     "returned": len(ids)})
    ids = [str(800000 + i) for i in range(15)]
    out = tools.get_auction_detail(ids)

    assert out["returned"] == tools.DETAIL_BATCH_CAP
    assert out["not_fetched_ids"] == ids[tools.DETAIL_BATCH_CAP:]
    assert "not_checked" in out["_note"] or "not checked" in out["_note"]


def test_no_note_when_nothing_was_dropped(monkeypatch):
    monkeypatch.setattr("api.tools.cypher_tools.get_auction_details",
                        lambda ids: {"results": [], "returned": 0})
    out = tools.get_auction_detail(["837057", "831476"])
    assert "not_fetched_ids" not in out
    assert "_note" not in out
