"""Offline shape test for the multi-turn conversation catalogue.

Validates `evals/conversations.py` structure without importing pydantic-ai /
Neo4j (the catalogue is stdlib-only). Two guards worth calling out:

- every `expected_tools` entry is a real tool (shared KNOWN_TOOLS); and
- `CARRY_FORWARD_FILTER_KEYS` (and every `expect_filters` key) matches the
  router's real `_CARRY_FORWARD_FILTER_KEYS`, so a filter added to the router
  without updating the eval — or a typo'd filter key in a conversation — fails
  here instead of silently never matching in the live run.

The live end-to-end conversation eval is `evals/run_conversations.py`
(nightly via `.github/workflows/golden-conversations.yml`).
"""
from __future__ import annotations

from evals.cases import KNOWN_TOOLS
from evals.conversations import (
    ANY,
    CARRY_FORWARD_FILTER_KEYS,
    GOLDEN_CONVERSATIONS,
    Turn,
)


def test_conversations_well_formed() -> None:
    assert len(GOLDEN_CONVERSATIONS) >= 4
    ids = [c.conv_id for c in GOLDEN_CONVERSATIONS]
    assert len(ids) == len(set(ids)), "conversation ids must be unique"

    for conv in GOLDEN_CONVERSATIONS:
        assert conv.conv_id.strip()
        assert conv.description.strip()
        assert len(conv.turns) >= 2, f"{conv.conv_id} needs multiple turns"
        for turn in conv.turns:
            assert isinstance(turn, Turn)
            assert turn.message.strip(), "turn message must be non-empty"
            for t in turn.expected_tools:
                assert t in KNOWN_TOOLS, f"unknown tool {t!r} in {conv.conv_id}"
            for key in turn.expect_filters:
                assert key in CARRY_FORWARD_FILTER_KEYS, (
                    f"expect_filters key {key!r} in {conv.conv_id} is not a "
                    "carry-forward filter"
                )
            for key in turn.forbid_tool_arg_values:
                assert key in CARRY_FORWARD_FILTER_KEYS, (
                    f"forbid_tool_arg_values key {key!r} in {conv.conv_id} is "
                    "not a carry-forward filter"
                )
            if turn.topic_switch:
                # A topic-switch turn should assert *something* about the pivot:
                # either the new scope or a dropped stale value.
                assert turn.expect_filters or turn.forbid_tool_arg_values or turn.expected_tools
            # expect_panel only takes the assertions PanelState implements.
            unknown = set(turn.expect_panel) - {"max_ids", "cited"}
            assert not unknown, (
                f"unknown expect_panel key(s) {unknown} in {conv.conv_id}"
            )
            if turn.references_panel:
                # A panel-reference turn needs a prior turn to have populated
                # the panel — it can't be the conversation opener.
                assert conv.turns.index(turn) > 0, (
                    f"references_panel on the first turn of {conv.conv_id}"
                )


def test_has_refinement_and_topic_switch_coverage() -> None:
    """The catalogue must actually exercise both target behaviors."""
    narrowing = [c for c in GOLDEN_CONVERSATIONS
                 if sum(1 for t in c.turns if t.narrows) >= 2]
    switching = [c for c in GOLDEN_CONVERSATIONS
                 if any(t.topic_switch for t in c.turns)]
    assert narrowing, "need at least one monotonic-narrowing conversation"
    assert switching, "need at least one topic-switch conversation"
    # And a scope-replacement pivot that forbids carrying a stale arg value.
    assert any(
        t.forbid_tool_arg_values
        for c in GOLDEN_CONVERSATIONS for t in c.turns
    ), "need a scope-replacement pivot asserting a dropped stale filter"
    # Panel coverage: a citation-driven panel assertion and a bare panel
    # reference ("compare these") must both be exercised.
    assert any(
        t.expect_panel for c in GOLDEN_CONVERSATIONS for t in c.turns
    ), "need at least one turn asserting panel state (expect_panel)"
    assert any(
        t.references_panel for c in GOLDEN_CONVERSATIONS for t in c.turns
    ), "need at least one panel-reference turn (references_panel)"


def test_carry_forward_keys_match_source() -> None:
    """The eval's mirrored key set must equal the real source of truth.

    That source is now `api/chat/scope_keys.py`, shared by the v1 router and
    the v2 tiered loop, so a filter added there without updating the eval
    mirror is caught offline.
    """
    from api.chat.scope_keys import CARRY_FORWARD_FILTER_KEYS as SOURCE

    assert CARRY_FORWARD_FILTER_KEYS == SOURCE, (
        "evals/conversations.py CARRY_FORWARD_FILTER_KEYS drifted from "
        "api/chat/scope_keys.py CARRY_FORWARD_FILTER_KEYS"
    )


def test_router_reexports_the_shared_key_set() -> None:
    """The router alias must stay bound to the shared definition — a local
    redefinition there would silently fork v1's scope from v2's."""
    from api.chat.router import _CARRY_FORWARD_FILTER_KEYS
    from api.chat.scope_keys import CARRY_FORWARD_FILTER_KEYS as SOURCE

    assert _CARRY_FORWARD_FILTER_KEYS is SOURCE


def test_any_sentinel_usable() -> None:
    """ANY is a plain sentinel value usable in expect_filters."""
    t = Turn("x", expect_filters={"deadline_within_days": ANY})
    assert t.expect_filters["deadline_within_days"] == ANY
