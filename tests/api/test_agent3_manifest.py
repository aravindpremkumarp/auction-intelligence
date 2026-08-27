"""api/agent3/manifest.py + manifest_store.py — the per-turn card record.

The design is `docs/designs/turn-owned-property-cards.md` (#404). What these
pin is the part of it that is easy to get subtly wrong and impossible to
notice from the UI: the turn ordinal, which ids are eligible for a card, and
which sentence ends up quoted on which card. A mis-attributed quote looks
exactly like a working feature.

No Neo4j and no model — the manifest builder is pure apart from one by-id
fetch, and the store's queries are asserted against a fake runner.
"""
from __future__ import annotations

import asyncio

from api.agent3.common import ToolSink
from api.agent3.manifest import (
    CARD_FIELDS,
    TurnManifest,
    annotate,
    build_manifest,
    project_card,
    split_units,
    turn_index_of,
)


class _Msg:
    """A message duck-typed the way the loop reads them."""

    def __init__(self, type_: str, content: str = "", tool_calls=None):
        self.type = type_
        self.content = content
        self.tool_calls = tool_calls or []


class _Result:
    """A TurnResult stand-in — only the fields the manifest reads."""

    def __init__(self, *, answer="", panel_rows=None, total=None,
                 query_echo=None, breakdown=None, searched=False,
                 web_sources=None):
        self.answer = answer
        self.panel_rows = panel_rows or []
        self.total = total
        self.query_echo = query_echo
        self.breakdown = breakdown
        self.searched = searched
        self.web_sources = web_sources or []


def _row(auction_id: str, **extra) -> dict:
    row = {"auction_id": auction_id, "title": "A flat", "city": "Chennai",
           "reserve_price": 4250000, "bank": "Indian Bank",
           "notice_lot_count": 1}
    row.update(extra)
    return row


# ── turn ordinals ────────────────────────────────────────────────────────

def test_the_ordinal_counts_answers_not_ai_messages():
    """A tool-using turn checkpoints several AIMessages. Counting all of them
    runs the ordinal ahead of the conversation, and every later manifest then
    joins to the wrong message."""
    messages = [
        _Msg("human", "q1"),
        _Msg("ai", "", tool_calls=[{"name": "find_properties"}]),
        _Msg("tool", "rows"),
        _Msg("ai", "the answer"),
    ]
    assert turn_index_of(messages) == 1


def test_the_ordinal_is_one_based_and_includes_the_current_answer():
    """At write time the answer is already checkpointed, so the first turn is
    index 1 — the frontend counts the same way, so the two cannot disagree by
    one."""
    messages = [_Msg("human", "q1"), _Msg("ai", "a1"),
                _Msg("human", "q2"), _Msg("ai", "a2")]
    assert turn_index_of(messages) == 2


def test_a_gate_repair_does_not_advance_the_ordinal():
    """The repair deletes the draft and injects a HumanMessage. Neither is an
    answer the user received."""
    messages = [_Msg("human", "q1"), _Msg("human", "repair note"),
                _Msg("ai", "the corrected answer")]
    assert turn_index_of(messages) == 1


# ── sentence units ───────────────────────────────────────────────────────

def test_two_sentences_split_so_a_price_is_not_inherited():
    """The digit in the lookahead is the point: without it "₹1.5 Cr. 812440
    is next" is one unit and 812440's card quotes the other property's
    price."""
    units = split_units("Priced at ₹1.5 Cr. 812440 is next.")
    assert units == ["Priced at ₹1.5 Cr.", "812440 is next."]


def test_a_markdown_list_item_is_its_own_unit():
    """It has no sentence-ending mark, so without the block rule one bullet
    swallows the whole list."""
    units = split_units("Two options:\n- 748779: symbolic possession\n"
                        "- 812440: physical")
    assert "- 748779: symbolic possession" in units
    assert "- 812440: physical" in units


def test_the_documented_abbreviation_clip_is_a_clip_not_a_mix_up():
    """`Rs.` splits early, so the quote is truncated. #404 accepts that: a
    clipped quote is cosmetic, a quote attached to the wrong property is
    not. This pins which of the two we get."""
    units = split_units("748779 costs Rs. 50 lakh.")
    assert units[0] == "748779 costs Rs."
    assert "748779" not in units[1]


# ── attribution ──────────────────────────────────────────────────────────

def test_each_card_quotes_the_sentence_that_named_it():
    notes = annotate("748779 looks strong. 812440 is riskier.",
                     {"748779", "812440"})
    assert notes["748779"] == ["748779 looks strong."]
    assert notes["812440"] == ["812440 is riskier."]


def test_a_sentence_naming_two_ids_attaches_to_both():
    notes = annotate("748779 and 812440 share a bank.", {"748779", "812440"})
    assert notes["748779"] == notes["812440"] == ["748779 and 812440 share a bank."]


def test_an_ungrounded_id_gets_no_annotation():
    """An id in the prose that no tool output grounds is a gate escape.
    Rendering a card for it would be the display layer laundering a
    hallucination."""
    assert annotate("999111 is a great buy.", {"748779"}) == {}


def test_a_six_digit_price_is_not_mistaken_for_a_property():
    """₹6,50,000 normalises into the id band. Without the currency guard
    every correctly-quoted reserve becomes a card."""
    assert annotate("The reserve is ₹650000 flat.", {"650000"}) == {}


# ── the manifest ─────────────────────────────────────────────────────────

def _build(result, messages, thread_id="t1") -> TurnManifest:
    return asyncio.run(build_manifest(result, messages, thread_id=thread_id))


def test_a_search_turn_reports_the_true_total_not_the_row_count():
    """The rows stop at PANEL_ROW_CAP; the count is exact over every match.
    Reporting len(rows) is why an 812-match search has always displayed
    "500 matches"."""
    rows = [_row("748779"), _row("812440")]
    m = _build(_Result(answer="748779 and 812440.", panel_rows=rows,
                       total=812, searched=True),
               [_Msg("tool", "748779 812440"), _Msg("ai", "748779 and 812440.")])
    assert m.kind == "search"
    assert m.counts == {"total": 812, "shown": 2}


def test_card_rows_are_the_card_fields_only():
    """A snapshot, not the graph row — bounded on purpose, because it is
    duplicated per turn and kept for the thread's lifetime."""
    rows = [_row("748779", secret_internal_field="do not store",
                 area_sqft_scope="notice")]
    m = _build(_Result(answer="748779.", panel_rows=rows, total=1,
                       searched=True),
               [_Msg("tool", "748779"), _Msg("ai", "748779.")])
    assert "secret_internal_field" not in m.card_rows[0]
    assert m.card_rows[0]["auction_id"] == "748779"
    # The scope badge is a correctness feature, so its fields must survive.
    assert m.card_rows[0]["area_sqft_scope"] == "notice"
    assert set(m.card_rows[0]) <= set(CARD_FIELDS)


def test_a_search_that_matched_nothing_is_empty_not_none():
    """"asked and got nothing" is a state worth rendering; "never asked" is
    not. They used to be indistinguishable because the zero path skipped the
    sink."""
    m = _build(_Result(answer="Nothing matched.", searched=True, total=0,
                       query_echo={"filters": ["city = Salem"]}),
               [_Msg("ai", "Nothing matched.")])
    assert m.kind == "empty"
    assert m.query_echo == {"filters": ["city = Salem"]}


def test_a_group_by_turn_is_a_distribution_not_a_zero_match():
    """It matched plenty and grouped it. Rendering "0 matches" would be
    false."""
    m = _build(_Result(answer="Chennai leads.", searched=True, total=200,
                       breakdown=[{"value": "Chennai", "listings": 120}]),
               [_Msg("ai", "Chennai leads.")])
    assert m.kind == "distribution"
    assert m.breakdown == [{"value": "Chennai", "listings": 120}]


def test_a_greeting_gets_a_manifest_with_no_cards():
    """Every final answer gets one, without exception — otherwise ordinals
    and manifests disagree about how many turns the thread has."""
    m = _build(_Result(answer="Hello!"), [_Msg("ai", "Hello!")])
    assert m.kind == "none"
    assert m.card_rows == [] and m.discussed_ids == []
    assert m.turn_index == 1


def test_grounding_is_thread_wide_so_a_follow_up_keeps_its_card():
    """On "tell me more about the second one" the agent names an id an
    earlier turn's search returned. Scoping evidence to this turn would strip
    the card off every one of them."""
    messages = [
        _Msg("tool", '{"auction_id": "748779"}'),   # turn 1's search
        _Msg("ai", "Two matches."),
        _Msg("human", "tell me about the first"),
        _Msg("ai", "748779 has physical possession."),
    ]
    m = _build(_Result(answer="748779 has physical possession."), messages)
    assert m.discussed_ids == ["748779"]
    assert m.annotations["748779"] == ["748779 has physical possession."]


def test_a_failed_build_still_returns_a_manifest_with_its_ordinal():
    """Best-effort by contract: losing the cards must never cost the user an
    answer they already paid for."""

    class _Explodes:
        answer = "748779 is good."

        def __getattr__(self, name):  # every field read blows up
            raise RuntimeError("boom")

    m = _build(_Explodes(), [_Msg("ai", "748779 is good.")])
    assert m.turn_index == 1
    assert m.kind == "none" and m.card_rows == []


def test_project_card_drops_nulls_rather_than_storing_them():
    assert project_card({"auction_id": "1", "emd": None}) == {"auction_id": "1"}


# ── the sink's new fields ────────────────────────────────────────────────

def test_absorb_still_works_with_rows_alone():
    """Keyword-only extras: the existing callers and tests pass rows and
    nothing else."""
    sink = ToolSink()
    sink.absorb([_row("748779")])
    assert sink.total == 1 and sink.searched is True
    assert sink.auction_ids == ["748779"]


def test_absorb_keeps_the_true_total_apart_from_the_row_count():
    sink = ToolSink()
    sink.absorb([_row("748779")], total=812, query_args={"filters": ["x"]})
    assert sink.total == 812
    assert len(sink.panel_rows) == 1
    assert sink.query_args == {"filters": ["x"]}


def test_absorb_empty_marks_a_search_that_found_nothing():
    sink = ToolSink()
    sink.absorb_empty(query_args={"filters": ["city = Salem"]})
    assert sink.searched is True and sink.total == 0
    assert sink.panel_rows == [] and sink.breakdown is None


def test_absorb_breakdown_keeps_the_match_count():
    sink = ToolSink()
    sink.absorb_breakdown([{"value": "Chennai", "listings": 5}], total=200)
    assert sink.total == 200
    assert sink.breakdown == [{"value": "Chennai", "listings": 5}]
    assert sink.panel_rows == []


def test_a_second_search_replaces_the_first_and_clears_its_breakdown():
    """`absorb` replaces — the manifest adopts that rule explicitly, so a
    turn that groups and then searches must not keep the stale table."""
    sink = ToolSink()
    sink.absorb_breakdown([{"value": "Chennai", "listings": 5}], total=200)
    sink.absorb([_row("748779")], total=1)
    assert sink.breakdown is None and sink.total == 1


# ── the store ────────────────────────────────────────────────────────────

def test_the_store_json_encodes_the_map_valued_fields(monkeypatch):
    """Neo4j properties cannot hold lists of maps, so these go as JSON
    strings. Sending them raw fails at the driver, not here."""
    from api.agent3 import manifest_store

    captured: dict = {}

    async def _fake(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return []

    monkeypatch.setattr(manifest_store, "run_query_async", _fake)
    asyncio.run(manifest_store.save(TurnManifest(
        thread_id="t1", turn_index=2, kind="search",
        card_rows=[{"auction_id": "748779"}],
        annotations={"748779": ["748779 looks strong."]},
        counts={"total": 812, "shown": 1})))

    assert isinstance(captured["params"]["card_rows"], str)
    assert isinstance(captured["params"]["annotations"], str)
    assert captured["params"]["idx"] == 2
    # MERGE, not CREATE: a retried turn overwrites its own record rather than
    # leaving two rows claiming the same ordinal.
    assert "MERGE (m:TurnManifest" in captured["cypher"]


def test_an_oversized_manifest_stores_without_rows_rather_than_failing(monkeypatch):
    """Losing the cards degrades the turn; failing the write loses the whole
    record, ordinal included."""
    from api.agent3 import manifest_store

    captured: dict = {}

    async def _fake(cypher, params=None):
        captured["params"] = params
        return []

    monkeypatch.setattr(manifest_store, "run_query_async", _fake)
    monkeypatch.setattr(manifest_store, "MAX_ROWS_BYTES", 50)
    asyncio.run(manifest_store.save(TurnManifest(
        thread_id="t1", turn_index=1,
        card_rows=[_row(str(600000 + i)) for i in range(20)])))
    assert captured["params"]["card_rows"] == "[]"


def test_loading_a_thread_decodes_back_to_the_shape_the_ui_reads(monkeypatch):
    from api.agent3 import manifest_store

    async def _fake(cypher, params=None):
        return [{"turn_index": 1, "kind": "search",
                 "card_rows": '[{"auction_id": "748779"}]',
                 "discussed_ids": '["748779"]',
                 "annotations": '{"748779": ["748779 looks strong."]}',
                 "query_echo": '{"filters": ["city = Chennai"]}',
                 "counts": '{"total": 812, "shown": 1}',
                 "breakdown": "null", "web_sources": "[]",
                 "produced_at": "2026-08-27T00:00:00Z"}]

    monkeypatch.setattr(manifest_store, "run_read_query_async", _fake)
    out = asyncio.run(manifest_store.load_thread("t1"))
    assert out[0]["card_rows"] == [{"auction_id": "748779"}]
    assert out[0]["annotations"] == {"748779": ["748779 looks strong."]}
    assert out[0]["counts"]["total"] == 812
    assert out[0]["breakdown"] is None


def test_corrupt_json_falls_back_instead_of_breaking_the_thread(monkeypatch):
    """One bad row must not make a whole conversation unopenable."""
    from api.agent3 import manifest_store

    async def _fake(cypher, params=None):
        return [{"turn_index": 1, "kind": "search", "card_rows": "{not json",
                 "discussed_ids": None, "annotations": None,
                 "query_echo": None, "counts": None, "breakdown": None,
                 "web_sources": None, "produced_at": None}]

    monkeypatch.setattr(manifest_store, "run_read_query_async", _fake)
    out = asyncio.run(manifest_store.load_thread("t1"))
    assert out[0]["card_rows"] == []
    assert out[0]["counts"] == {"total": 0, "shown": 0}


# ── the reload history ───────────────────────────────────────────────────

_D = "\n\n---\n\n"   # skills.USER_TEXT_DELIMITER


def test_history_returns_the_users_words_not_the_composed_message():
    """`compose_input` prepends the date line and any loaded skills. Showing
    that back to the user would show them a message they did not write."""
    from api.agent3.manifest import history_from_messages

    out = history_from_messages([
        _Msg("human", f"Today is 2026-08-27.{_D}flats in chennai"),
        _Msg("ai", "Two matches."),
    ])
    assert out[0] == {"role": "user", "text": "flats in chennai"}


def test_history_drops_the_gate_repair_note():
    """The repair is injected as a HumanMessage with no delimiter. It is the
    gate talking to the model, not the user talking to anyone."""
    from api.agent3.manifest import history_from_messages

    out = history_from_messages([
        _Msg("human", f"Today is 2026-08-27.{_D}flats in chennai"),
        _Msg("human", "Your draft cited an id no tool returned. Rewrite it."),
        _Msg("ai", "Two matches."),
    ])
    assert [m["role"] for m in out] == ["user", "assistant"]


def test_history_ordinals_match_the_manifest_ordinals():
    """This is the join. If the two ever count differently, every turn after
    the divergence shows another turn's properties."""
    from api.agent3.manifest import history_from_messages

    messages = [
        _Msg("human", f"d{_D}q1"),
        _Msg("ai", "", tool_calls=[{"name": "find_properties"}]),
        _Msg("tool", "rows"),
        _Msg("ai", "a1"),
        _Msg("human", f"d{_D}q2"),
        _Msg("ai", "a2"),
    ]
    out = history_from_messages(messages)
    answers = [m for m in out if m["role"] == "assistant"]
    assert [m["turn_index"] for m in answers] == [1, 2]
    assert answers[-1]["turn_index"] == turn_index_of(messages)


def test_history_of_an_empty_thread_is_empty_not_an_error():
    from api.agent3.manifest import history_from_messages

    assert history_from_messages([]) == []


# ── the artifact's count ─────────────────────────────────────────────────
#
# `build_artifacts` is exercised in test_agent3_router.py, but that module
# imports api.main through conftest. These two live here so the count
# contract — the one number the panel and the inline chip must agree on —
# is covered by a module that collects without the web stack.

def test_the_artifact_reports_the_search_total_not_the_rows_it_carries():
    """`len(rows)` stops at PANEL_ROW_CAP, so an 812-match search reported
    500: the cap, presented as the answer. The panel already knows how to say
    "showing 500 · sorted by deadline" once the total exceeds what it holds."""
    from api.agent3.artifacts import build_artifacts

    rows = [_row(str(700000 + i)) for i in range(500)]
    arts = asyncio.run(build_artifacts(
        _Result(answer="Lots.", panel_rows=rows, total=812, searched=True)))
    assert arts[0]["result"]["total_count"] == 812
    assert len(arts[0]["ui_rows"]) == 500


def test_the_artifact_falls_back_to_the_row_count_without_a_total():
    """Every caller predating the sink carrying a total, including the
    existing router tests."""
    from api.agent3.artifacts import build_artifacts

    class _Old:
        answer = "Two matches."
        panel_rows = [_row("748779"), _row("744314")]
        web_sources: list = []

    arts = asyncio.run(build_artifacts(_Old()))
    assert arts[0]["result"]["total_count"] == 2
