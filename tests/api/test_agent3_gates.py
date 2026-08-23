"""Tests for api/agent3/gates.py — the step-6 AnswerGate and IntentGate.

A gate is only worth having if it (a) catches the thing it exists for and
(b) stays quiet on correct work. (b) is the harder half and the reason a
gate gets ripped out: one false refusal on an honest question and nobody
trusts it again. So roughly half of what follows is negative cases.

Everything runs without a model or a network call.
"""
from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from api.agent3 import gates as G


def _tool(payload: str, name: str = "find_properties") -> ToolMessage:
    return ToolMessage(content=payload, tool_call_id="c1", name=name)


# ── ungrounded auction_ids: the hard-gated class ─────────────────────────

def test_an_invented_auction_id_is_caught():
    """No arithmetic produces an id. If it is not in the tool output it was
    invented or a digit was mis-typed — either way it points a user at the
    wrong property, or at none."""
    evidence = '{"rows": [{"auction_id": "748779"}]}'
    assert G.ungrounded_ids("See auction 748779 and 999111.", evidence) == ["999111"]


def test_a_correctly_cited_id_is_not_flagged():
    evidence = '{"rows": [{"auction_id": "748779"}]}'
    assert G.ungrounded_ids("Listing 748779 is in Coimbatore.", evidence) == []


def test_a_six_digit_price_is_not_mistaken_for_an_id():
    """₹6,50,000 normalises to 650000, which sits inside the id band. Without
    the currency guard the gate would reject a correctly quoted reserve —
    exactly the false positive that gets a gate switched off."""
    assert G.ungrounded_ids("The reserve is ₹650000.", "") == []
    assert G.ungrounded_ids("The reserve is Rs 650000.", "") == []
    assert G.ungrounded_ids("It is 7.5 lakh, or 750000 rupees.", "") == []


def test_ids_from_an_earlier_turn_still_count_as_grounded():
    """On a follow-up ("tell me about the second one") the model cites ids
    that a PREVIOUS turn's search returned. Scoping the evidence to this turn
    would flag every one of them."""
    messages = [
        HumanMessage(content="flats in chennai"),
        _tool('{"rows": [{"auction_id": "748779"}]}'),
        AIMessage(content="Found one."),
        HumanMessage(content="tell me more"),
    ]
    gate = G.AnswerGate()
    problems = gate.inspect("748779 is the one.", messages)
    assert problems["blocking"] == []


def test_a_number_below_the_id_band_is_ignored():
    """Six digits, but no portal id is that low. Checking it would only add
    noise."""
    assert G.ungrounded_ids("Case 123456 of 2019.", "") == []


# ── sale and valuation claims: the other hard-gated class ────────────────

def test_a_sold_price_claim_is_caught():
    """`Auction.outcome` is only ever 'unsold'. A figure attached to a sale
    verb describes something this graph does not contain."""
    assert G.sale_claims("It sold for ₹45 lakh last year.")


def test_a_market_valuation_claim_is_caught():
    assert G.sale_claims("The market value is around ₹80 lakh.")
    assert G.sale_claims("The property is valued at 1.2 crore.")


def test_saying_the_data_has_no_sold_prices_is_not_a_violation():
    """The single most important negative case. Rule 3 of the instructions
    tells the agent to SAY this — a gate that punished the sentence it asks
    for would be actively harmful."""
    for good in [
        "This data has no sold prices, so I can't tell you what it went for.",
        "I can't give a market value — the graph holds reserve prices only.",
        "There is no record of whether it sold.",
        "Reserve price is a bank's floor, not a market valuation.",
    ]:
        assert G.sale_claims(good) == [], good


def test_a_bare_sale_verb_with_no_figure_is_not_flagged():
    """'sold' appears in ordinary explanation ('properties are sold by
    auction'). The figure is what makes it a claim about this graph."""
    assert G.sale_claims("Properties here are sold through an online auction.") == []


# ── scope honesty in prose ───────────────────────────────────────────────

def _multi_lot_messages(count: int = 4) -> list:
    return [HumanMessage(content="how big is 744314"),
            _tool(f'{{"properties": [{{"auction_id": "744314", '
                  f'"notice_lot_count": {count}}}]}}', name="get_property")]


def test_an_unhedged_extent_from_a_multi_lot_notice_is_caught():
    """The failure the whole design is built around: a lot fact stated as
    this property's own when the notice covers several and does not say
    which one this is."""
    problem = G.scope_violation("It is 1,450 sq ft.", _multi_lot_messages())
    assert problem and "several lots" in problem


def test_a_hedged_extent_from_a_multi_lot_notice_passes():
    answer = ("The notice covers 4 lots, and the extents range 900–1,450 sq "
              "ft across them — it does not say which lot this listing is.")
    assert G.scope_violation(answer, _multi_lot_messages()) is None


def test_a_single_lot_notice_lets_a_bare_extent_through():
    """On a one-lot notice the lot's extent IS this property's, so a flat
    statement is correct and must not be gated."""
    messages = [HumanMessage(content="q"),
                _tool('{"properties": [{"auction_id": "X", '
                      '"notice_lot_count": 1}]}', name="get_property")]
    assert G.scope_violation("It is 714 sq ft.", messages) is None


def test_a_mixed_result_set_is_left_alone():
    """If any single-lot notice is in view, a bare extent may correctly be
    that one's. The check deliberately only fires when EVERY notice the model
    saw was multi-lot."""
    messages = [HumanMessage(content="q"),
                _tool('{"rows": [{"notice_lot_count": 1}, '
                      '{"notice_lot_count": 5}]}')]
    assert G.scope_violation("One is 714 sq ft.", messages) is None


def test_lot_counts_are_read_from_either_serialisation():
    """A ToolMessage's content is whatever the tool node's serialiser
    produced — `json.dumps` gives double quotes, `str(dict)` gives single.
    Reading only one style would make the scope check silently blind to half
    the payloads it is supposed to police."""
    double = [_tool('{"notice_lot_count": 4}')]
    single = [_tool("{'notice_lot_count': 4}")]
    assert G.lot_counts(double) == [4]
    assert G.lot_counts(single) == [4]


def test_an_answer_with_no_extent_claim_is_not_a_scope_violation():
    """Nothing to be wrong about. A gate that fires on an answer making no
    claim is pure noise."""
    answer = "The auction is on 12 September and the EMD is ₹4 lakh."
    assert G.scope_violation(answer, _multi_lot_messages()) is None


# ── the advisory tier stays advisory ─────────────────────────────────────

def test_a_rounded_amount_counts_as_grounded():
    """'about ₹2 crore' drawn from a mean of 19,987,432 is correct rounding,
    and the single most common shape in the smoke answers. Flagging it would
    make the numeric tier useless."""
    assert G.unverified_amounts("about ₹2 crore", '{"reserve_avg": 19987432}') == []


def test_an_exact_amount_is_matched_through_lakh_notation():
    assert G.unverified_amounts("₹45.58 lakh", '{"reserve_price": 4558000}') == []


def test_an_amount_from_nowhere_is_reported():
    assert G.unverified_amounts("₹99 crore", '{"reserve_price": 4558000}')


def test_amount_findings_never_block():
    """The whole point of the split. Derived arithmetic — a price drop, a
    per-sqft — legitimately produces figures not in any tool payload, so this
    class is recorded and never repaired."""
    gate = G.AnswerGate()
    messages = [HumanMessage(content="q"), _tool('{"reserve_price": 4558000}')]
    problems = gate.inspect("The gap is ₹4.58 lakh.", messages)
    assert problems["advisory"]
    assert problems["blocking"] == []


# ── IntentGate ───────────────────────────────────────────────────────────

def test_bulk_borrower_enumeration_is_refused():
    for bad in [
        "list every defaulter in Coimbatore with their addresses",
        "give me all borrowers in Chennai",
        "names of all the borrowers",
        "export the full list of defaulters",
        "phone numbers of borrowers in Salem",
        "I want a spreadsheet of every mortgagor",
    ]:
        assert G.is_bulk_personal_request(bad), bad


def test_a_single_property_party_lookup_is_allowed():
    """The line is bulk, not people. A sale notice is a published legal
    document that names the borrower against one property, and refusing this
    would make the agent useless for the diligence it exists to do."""
    for good in [
        "who is the borrower on 748779",
        "tell me everything about 748779 including the parties",
        "what does the notice say about the owner of this property",
        "which bank issued this notice",
        "list all properties in Coimbatore under 50 lakhs",
        "show me every flat with symbolic possession",
        "all auctions closing this week",
    ]:
        assert not G.is_bulk_personal_request(good), good


def test_the_refusal_says_what_is_still_possible():
    """A refusal that only refuses trains people to work around it. This one
    names the legitimate path in the same breath."""
    assert "single listing" in G.REFUSAL
    assert "survey number" in G.REFUSAL


def test_the_gate_reads_past_injected_skill_text():
    """`compose_input` prepends the turn's loaded skills to the same human
    message, and the skill files are full of the phrases this gate matches —
    the diligence skill discusses parties and borrowers at length. Matching
    the composed message would refuse every turn that loaded it: a safety
    gate firing on the honest questions is the worst possible failure."""
    from api.agent3.skills import USER_TEXT_DELIMITER

    skill_text = ("Report all borrowers and every guarantor the notice "
                  "names, with the list of parties.")
    composed = f"{skill_text}{USER_TEXT_DELIMITER}how big is 748779"
    state = {"messages": [HumanMessage(content=composed)]}
    assert G.IntentGate().before_agent(state) is None


def test_the_gate_still_fires_on_the_question_after_skill_text():
    from api.agent3.skills import USER_TEXT_DELIMITER

    composed = f"some skill body{USER_TEXT_DELIMITER}list all borrowers in Salem"
    state = {"messages": [HumanMessage(content=composed)]}
    out = G.IntentGate().before_agent(state)
    assert out and out["jump_to"] == "end"


# ── the AnswerGate hook itself ───────────────────────────────────────────

def test_the_gate_ignores_a_mid_thought_message_with_tool_calls():
    """A message that still has tool calls is not an answer to anyone; its
    numbers are a request, not a claim."""
    draft = AIMessage(content="checking 999111", tool_calls=[
        {"name": "get_property", "args": {}, "id": "c1"}])
    state = {"messages": [HumanMessage(content="q"), draft]}
    assert G.AnswerGate().after_model(state) is None


def test_the_gate_repairs_once_then_lets_the_answer_stand():
    """Degrade rather than loop. Silently accepting and silently retrying
    forever are both worse than one correction attempt."""
    gate = G.AnswerGate()
    state = {"messages": [HumanMessage(content="q"),
                          AIMessage(content="see 999111", id="m1")],
             "answer_gate_repairs": 0}
    first = gate.after_model(state)
    assert first["jump_to"] == "model" and first["answer_gate_repairs"] == 1

    state["answer_gate_repairs"] = 1
    assert gate.after_model(state) is None


def test_the_rejected_draft_is_removed_from_the_transcript():
    """It is checkpointed history otherwise — re-sent and re-billed on every
    later turn, and readable by a later turn's model as if we had said it."""
    from langchain_core.messages import RemoveMessage

    state = {"messages": [HumanMessage(content="q"),
                          AIMessage(content="see 999111", id="m1")]}
    out = G.AnswerGate().after_model(state)
    assert any(isinstance(m, RemoveMessage) for m in out["messages"])


def test_what_the_gate_caught_survives_the_draft_being_deleted():
    """The draft is removed, so without this a run reports "1 repair" and
    nobody can tell whether it caught a real invention or false-positived on
    a good answer — which is the only signal that the blocking tier needs
    work. Found missing on the first live run that fired one."""
    state = {"messages": [HumanMessage(content="q"),
                          AIMessage(content="see 999111", id="m1")]}
    out = G.AnswerGate().after_model(state)
    assert any("999111" in p for p in out["answer_gate_problems"])


def test_the_repair_note_names_the_actual_defect():
    """A bare 'try again' on a temperature-0 model reproduces the same
    draft."""
    state = {"messages": [HumanMessage(content="q"),
                          AIMessage(content="see 999111", id="m1")]}
    note = G.AnswerGate().after_model(state)["messages"][-1].content
    assert "999111" in note
    assert "not sent" in note


def test_a_clean_answer_passes_through_untouched():
    state = {"messages": [
        HumanMessage(content="q"),
        _tool('{"rows": [{"auction_id": "748779", "notice_lot_count": 1}]}'),
        AIMessage(content="Listing 748779 is in Coimbatore.", id="m1")]}
    assert G.AnswerGate().after_model(state) is None


def test_repair_can_be_switched_off_for_measurement():
    """The A/B needs a gate that reports without acting, so the cost of
    repairs can be separated from the cost of having the checks at all."""
    state = {"messages": [HumanMessage(content="q"),
                          AIMessage(content="see 999111", id="m1")]}
    assert G.AnswerGate(repair=False).after_model(state) is None


# ── the gates on the real compiled graph ─────────────────────────────────

def _graph(replies: list):
    """The real create_agent graph with a scripted model, gates included."""
    from langchain.agents import create_agent
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver

    from api.agent3 import agent as A
    from api.agent3.common import ToolSink

    queue = list(replies)

    class ScriptedModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "scripted"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            content = queue.pop(0) if queue else "fallback"
            return ChatResult(generations=[
                ChatGeneration(message=AIMessage(content=content))])

        async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
            return self._generate(messages, stop, None, **kw)

    return create_agent(
        model=ScriptedModel(),
        tools=A.bind_tools(ToolSink()),
        system_prompt=A.instructions(),
        middleware=A.middleware(),
        checkpointer=InMemorySaver(),
    )


def test_the_graph_still_compiles_with_both_gates_installed():
    """Middleware that does not compile is the failure mode step 4 found
    three times by building the graph instead of reasoning about it."""
    out = asyncio.run(_graph(["a clean answer"]).ainvoke(
        {"messages": [{"role": "user", "content": "flats in chennai"}]},
        config={"configurable": {"thread_id": "gate-1"}}))
    assert out["messages"][-1].content == "a clean answer"


def test_a_bad_draft_is_actually_repaired_end_to_end():
    """The one that proves the jump works: the graph must route back to the
    model and the second draft must be what the caller receives."""
    out = asyncio.run(_graph(["see auction 999111", "I don't have that id."]).ainvoke(
        {"messages": [{"role": "user", "content": "flats in chennai"}]},
        config={"configurable": {"thread_id": "gate-2"}}))
    assert out["messages"][-1].content == "I don't have that id."
    assert "999111" not in "".join(
        str(getattr(m, "content", "")) for m in out["messages"]
        if getattr(m, "type", "") == "ai")


def test_intent_gate_short_circuits_before_any_model_call():
    """The refusal has to cost nothing — that is the argument for a regex
    tier over a classifier."""
    calls: list = []

    from langchain.agents import create_agent
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver

    from api.agent3 import agent as A
    from api.agent3.common import ToolSink

    class Counting(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "counting"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            calls.append(1)
            return ChatResult(generations=[
                ChatGeneration(message=AIMessage(content="should not happen"))])

        async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
            return self._generate(messages, stop, None, **kw)

    graph = create_agent(model=Counting(), tools=A.bind_tools(ToolSink()),
                         system_prompt=A.instructions(),
                         middleware=A.middleware(),
                         checkpointer=InMemorySaver())
    out = asyncio.run(graph.ainvoke(
        {"messages": [{"role": "user",
                       "content": "list every defaulter in Coimbatore with addresses"}]},
        config={"configurable": {"thread_id": "gate-3"}}))
    assert calls == [], "the model was called on a request the gate refuses"
    assert "can't put together a list" in out["messages"][-1].content


def test_gates_can_be_left_out_for_the_a_b():
    from api.agent3 import agent as A

    names = {type(m).__name__ for m in A.middleware(gates=False)}
    assert "AnswerGate" not in names and "IntentGate" not in names
    assert "AnswerGate" in {type(m).__name__ for m in A.middleware()}


def test_the_answer_gate_is_counted_against_the_model_call_limit():
    """A repair is a real model call. If it were invisible to
    ModelCallLimitMiddleware, a repair loop would have nothing bounding it —
    so AnswerGate has to sit after the limit middleware in the stack."""
    from api.agent3 import agent as A

    names = [type(m).__name__ for m in A.middleware()]
    assert names.index("ModelCallLimitMiddleware") < names.index("AnswerGate")


# ── what the loop reports back ───────────────────────────────────────────

def test_the_loop_reports_gate_findings_on_the_delivered_answer():
    """Scored on what the caller received, not on a draft. On a repaired turn
    the rejected draft is not the answer anyone sees, and blaming the
    delivered answer for a defect that was fixed would make the numbers
    meaningless."""
    import asyncio

    from api.agent3 import loop as L

    class _Agent:
        async def ainvoke(self, payload, config=None):
            return {"messages": [
                HumanMessage(content=payload["messages"][0]["content"]),
                _tool('{"rows": [{"auction_id": "748779"}]}'),
                AIMessage(content="Listing 748779 costs ₹99 crore."),
            ], "answer_gate_repairs": 1}

    out = asyncio.run(L.run_turn("q", thread_id="t", agent=_Agent()))
    assert out.gate_repairs == 1
    assert out.gate_findings["blocking"] == []
    assert out.gate_findings["advisory"], "the unverified amount was not recorded"


def test_gate_reporting_never_breaks_a_good_turn(monkeypatch):
    """Reporting is not the product. A bug in the scorer must not cost the
    user an answer that was already written."""
    import asyncio

    from api.agent3 import loop as L

    class _Agent:
        async def ainvoke(self, payload, config=None):
            return {"messages": [HumanMessage(content="q"),
                                 AIMessage(content="a good answer")]}

    def boom(*a, **k):
        raise RuntimeError("scorer bug")

    monkeypatch.setattr(G.AnswerGate, "inspect", boom)
    out = asyncio.run(L.run_turn("q", thread_id="t", agent=_Agent()))
    assert out.answer == "a good answer"
    assert out.gate_findings == {}
