"""One id pattern for every reader: bare six-digit eauctionsindia ids keep
their band and currency guards; bn- / be- ids (BAANKNET, bankeauctions) are
carried by their prefix. The gate, the artifact fallback and the chat panel
all read it, so a prefixed id cited from a tool result is grounded and an
invented one is caught.
"""
from __future__ import annotations

from api import ids as I
from api.agent3 import artifacts as A
from api.agent3 import gates as G
from api.chat import panel as P
from api.chat.v2.middleware.answer_gate import check_answer


def test_both_shapes_are_extracted_in_order():
    assert I.guarded_ids("Compare bn-358394 with 841207, then be-235813.") == ["bn-358394", "841207", "be-235813"]
    assert I.guarded_ids("BN-359826 again: bn-359826.") == ["bn-359826"]        # case folds, deduped


def test_bare_ids_keep_their_guards_and_prefixed_ids_need_none():
    assert I.guarded_ids("The reserve is ₹650000.") == []
    assert I.guarded_ids("It is 7.5 lakh, or 750000 rupees.") == []
    assert I.guarded_ids("Case 123456 of 2019.") == []                          # below the band
    assert I.guarded_ids("Listing bn-1234 has a reserve of Rs 650000.") == ["bn-1234"]


def test_a_prefix_glued_to_other_text_is_not_an_id():
    assert I.guarded_ids("abn-359826 bn-359826x bn-359826-1 bn-12") == []
    assert I.guarded_ids("(bn-359826), \"be-235813\".") == ["bn-359826", "be-235813"]


def test_is_portal_id_is_a_whole_string_test():
    assert I.is_portal_id("841207") and I.is_portal_id("bn-359826") and I.is_portal_id(" BE-235813 ")
    assert not I.is_portal_id("123456") and not I.is_portal_id("bn-") and not I.is_portal_id("") and not I.is_portal_id(None)


def test_all_ids_is_the_loose_variant():
    assert I.all_ids("₹650000 and 123456 and bn-1") == ["650000", "123456"]     # bn-1: too short
    assert A.cited_ids("bn-359826, then 841207 for ₹650000") == ["bn-359826", "841207", "650000"]


def test_gate_grounds_a_prefixed_id_from_tool_output_and_catches_an_invented_one():
    evidence = '{"rows": [{"auction_id": "bn-359826"}, {"auction_id": "841207"}]}'
    assert G.ungrounded_ids("bn-359826 and 841207 are both listed.", evidence) == []
    assert G.ungrounded_ids("bn-359826 and bn-999999 are both listed.", evidence) == ["bn-999999"]
    assert G.ungrounded_ids("BN-359826 is the one.", evidence.lower()) == []


def test_v2_answer_gate_reads_prefixed_ids():
    results = [{"auction_id": "bn-359826", "reserve_price_num": 4626500.0}]
    assert check_answer("bn-359826 is the one.", results).ok
    verdict = check_answer("be-111111 is the one.", results)
    assert not verdict.ok and verdict.unsupported_ids == ["be-111111"]
    assert check_answer("bn-359826 is the one.", [], extra_ids=["BN-359826"]).ok


def test_panel_cites_prefixed_ids_the_conversation_surfaced():
    known = P.known_auction_ids([("find_properties", {"results": [{"auction_id": "bn-359826"}, {"auction_id": "841207"}]})])
    assert known == {"bn-359826", "841207"}
    assert P.cited_ids("Look at BN-359826, 841207 and bn-777777.", known) == ["bn-359826", "841207"]


def test_gates_still_reexport_the_shared_pattern():
    assert G.ID_LIKE is I.ID_LIKE and G.ID_BAND == I.ID_BAND and G.guarded_ids is I.guarded_ids
