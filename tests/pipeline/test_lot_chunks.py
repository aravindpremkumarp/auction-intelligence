"""Chunked extraction for long multi-lot notices (pipeline/lot_chunks.py)."""
from __future__ import annotations

import re

import pytest

from pipeline import lot_chunks as LC
from pipeline.lot_chunks import extract_chunked, find_lots, plan_chunks


# ── fixtures: the three layouts ──────────────────────────────────────────────

def _price_notice(n: int) -> str:
    lots = [f"No.{i} Borrower B{i}. Property at Sy No {i}/1.\n"
            f"Reserve price: Rs.{i},00,000/- | EMD: Rs.{i}0,000/-\n"
            for i in range(1, n + 1)]
    return ("# SALE NOTICE\nCanara Bank\n\n" + "\n".join(lots)
            + "\nAuction date 24.06.2026. Bids not below the reserve price.\n")


def _serial_notice(order) -> str:
    """Tata-style rows: serial row, EMD row, possession row, description."""
    rows = "".join(
        f'<tr>\n<td rowspan="3">{i}</td>\n<td>MR. B{i}</td>\n'
        f'<td>Rs. {i},00,000/-</td>\n</tr>\n<tr>\n<td>Earnest Money Deposit '
        f'(EMD): - Rs.{i}0,000/-</td>\n</tr>\n<tr>\n<td>Type of possession: - '
        f'Physical</td>\n</tr>\n<tr>\n<td colspan="3">Description: Sy No {i}/1, '
        f'village V{i}.</td>\n</tr>\n'
        for i in order)
    return ("# TATA CAPITAL\n\n<table>\n<tr><th>Sr. No</th><th>Borrower</th>"
            "<th>Reserve Price</th></tr>\n" + rows + "</table>\n\n"
            "The E-auction will take place on 11-08-2026.\n")


def _numbered_notice(n: int, clauses: int = 0) -> str:
    lots = [f"{i}. Name and Details of the Borrower : Mr B{i}\n\n"
            f"Details of the property : Plot {i}, Sy No {i}/2.\n\n"
            f"Amount due Rs.{i},00,000 as on 05.06.2026.\n"
            for i in range(1, n + 1)]
    terms = "".join(f"{i}. The bidder shall comply with clause {i}.\n"
                    for i in range(1, clauses + 1))
    return "# DCB Bank\n\nSale notice.\n\n" + "\n".join(lots) + "\nTerms:\n" + terms


# ── finding the lots ─────────────────────────────────────────────────────────

def test_price_layout_cuts_after_every_k_price_lines():
    md = _price_notice(12)
    plan = plan_chunks(md, 12, lots_per_chunk=5)
    assert plan.strategy == "price"
    assert [c.lots for c in plan.chunks] == [5, 5, 2]
    assert plan.chunks[0].start == 0
    for a, b in zip(plan.chunks, plan.chunks[1:]):
        assert a.end == b.start
    assert plan.chunks[-1].end == plan.tail_start
    assert md[plan.tail_start:].lstrip().startswith("Auction date")


def test_price_lot_ends_at_its_table_row_when_the_table_is_one_line():
    # HTML schedules often sit on one line: the line end would swallow every
    # later lot and collapse the rest into empty lots.
    cells = "".join(f"<tr><td>Lot {i} Sy No {i}/1</td></tr><tr><td>Reserve "
                    f"Price: Rs.{i},00,000/-</td><td>EMD Rs.{i}0,000</td></tr>"
                    for i in range(1, 8))
    md = "Header\n<table>" + cells + "</table>\nTerms\n"
    strategy, lots, _, _ = find_lots(md, 7)
    assert strategy == "price" and len(lots) == 7
    assert all(md[lot.start:lot.end].endswith("</tr>") for lot in lots)
    assert all(lot.end > lot.start for lot in lots)


def test_prose_mentions_of_reserve_price_are_not_lots():
    # "not below the reserve price" in the tail has no amount → not a lot
    assert plan_chunks(_price_notice(12), 12) is not None


def test_serial_layout_keeps_continuation_rows_with_their_lot():
    md = _serial_notice(range(1, 9))
    plan = plan_chunks(md, 8, lots_per_chunk=5)
    assert plan.strategy == "serial"
    assert [lot.label for lot in plan.lots] == [str(i) for i in range(1, 9)]
    lot3 = md[plan.lots[2].start:plan.lots[2].end]
    assert "Rs.30,000" in lot3 and "Sy No 3/1" in lot3 and "Sy No 4/1" not in lot3
    assert plan.head_end == plan.lots[0].start


def test_serial_layout_accepts_pages_joined_out_of_order():
    # tata-5: the second page was stitched ahead of the first
    md = _serial_notice(list(range(5, 9)) + list(range(1, 5)))
    strategy, lots, _, _ = find_lots(md, 8)
    assert strategy == "serial"
    assert [lot.label for lot in lots] == ["5", "6", "7", "8", "1", "2", "3", "4"]


def test_numbered_layout_ignores_the_numbered_terms():
    md = _numbered_notice(7, clauses=7)
    strategy, lots, _, _ = find_lots(md, 7)
    assert strategy == "numbered"
    assert all("Name and Details" in md[lot.start:lot.end] for lot in lots)


def test_a_numbered_run_without_money_is_not_lots():
    md = "Intro\n" + "".join(f"{i}. The bidder shall comply with clause {i}.\n"
                             for i in range(1, 8))
    assert find_lots(md, 7) is None


@pytest.mark.parametrize("expected", [11, 13, None])
def test_no_cut_unless_the_count_matches_exactly(expected):
    assert plan_chunks(_price_notice(12), expected) is None


def test_short_notices_are_read_whole():
    assert plan_chunks(_price_notice(4), 4, lots_per_chunk=5) is None


def test_chunks_are_capped_in_characters(monkeypatch):
    monkeypatch.setattr(LC, "CHUNK_CHAR_CAP", 120)
    plan = plan_chunks(_price_notice(12), 12, lots_per_chunk=5)
    assert all(c.lots < 5 for c in plan.chunks)
    assert sum(c.lots for c in plan.chunks) == 12


# ── a fake model ─────────────────────────────────────────────────────────────

_BLOCK = re.compile(r"(?:No\.|<td rowspan=\"3\">)(\d+)")


def _reader(drop=None, fail=None, log=None, partial=None, no_reserve=None):
    """Stands in for the model. Tags each lot block it sees with a LOCAL index
    1..k (as the prompt asks) — a borrower, a full_description and an
    auction_terms with its reserve — plus one notice-level date from the tail.
    ``drop(call_no, real_lot)`` returns True to leave a lot out of that read;
    ``partial(call_no, real_lot)`` returns True to read it without its
    full_description; ``no_reserve(call_no, real_lot)`` returns True to read it
    without its reserve price; ``fail(call_no)`` returns True to raise."""
    calls = {"n": 0}

    def read(text, lots, extra=None, strong=False):
        calls["n"] += 1
        n = calls["n"]
        if log is not None:
            log.append({"lots": lots, "extra": extra, "strong": strong})
        if fail and fail(n):
            raise RuntimeError("provider error")
        out, local = [], 0
        for m in _BLOCK.finditer(text):
            real = int(m.group(1))
            if drop and drop(n, real):
                continue
            local += 1
            out.append({"id": "x", "cls": "borrower", "text": m.group(0),
                        "start": m.start(), "end": m.end(),
                        "attrs": {"lot_index": str(local), "real": str(real),
                                  "call": n}})
            if not (partial and partial(n, real)):
                out.append({"id": "x", "cls": "full_description",
                            "text": m.group(0), "start": m.start(),
                            "end": m.end(),
                            "attrs": {"lot_index": str(local), "real": str(real),
                                      "call": n}})
            if not (no_reserve and no_reserve(n, real)):
                out.append({"id": "x", "cls": "auction_terms",
                            "text": m.group(0), "start": m.start(),
                            "end": m.end(),
                            "attrs": {"lot_index": str(local), "real": str(real),
                                      "call": n,
                                      "reserve_price_num": f"{real}00000"}})
        d = text.find("24.06.2026")
        if d >= 0:
            out.append({"id": "x", "cls": "auction_date", "text": "24.06.2026",
                        "start": d, "end": d + 10, "attrs": {}})
        return out
    return read


# ── stitching ────────────────────────────────────────────────────────────────

def test_lots_are_numbered_by_position_not_by_the_models_index():
    md = _price_notice(12)
    ents = extract_chunked(md, plan_chunks(md, 12, lots_per_chunk=5), _reader())
    lots = [e for e in ents if e["cls"] == "borrower"]
    assert [e["attrs"]["lot_index"] for e in lots] == [str(i) for i in range(1, 13)]
    assert all(e["attrs"]["lot_index"] == e["attrs"]["real"] for e in lots)
    assert all(md[e["start"]:e["end"]] == e["text"] for e in ents)


def test_a_model_that_misnumbers_is_corrected_by_position():
    md = _price_notice(10)

    def read(text, lots, extra=None, strong=False):
        ents = _reader()(text, lots)
        for e in ents:                       # every lot claims to be lot 1
            if e["attrs"].get("lot_index"):
                e["attrs"]["lot_index"] = "1"
        return ents
    ents = extract_chunked(md, plan_chunks(md, 10, lots_per_chunk=5), read)
    lots = [e for e in ents if e["cls"] == "borrower"]
    assert [e["attrs"]["lot_index"] for e in lots] == [str(i) for i in range(1, 11)]


def test_notice_level_entities_are_kept_once():
    md = _price_notice(12)
    ents = extract_chunked(md, plan_chunks(md, 12, lots_per_chunk=5), _reader())
    dates = [e for e in ents if e["cls"] == "auction_date"]
    assert len(dates) == 1
    assert md[dates[0]["start"]:dates[0]["end"]] == "24.06.2026"
    assert [e["id"] for e in ents] == [str(i) for i in range(len(ents))]


def test_an_ungrounded_lot_entity_follows_its_lots_grounded_ones():
    md = _price_notice(10)

    def read(text, lots, extra=None, strong=False):
        ents = _reader()(text, lots)
        ents.append({"id": "x", "cls": "outstanding", "text": "?", "start": None,
                     "end": None, "attrs": {"lot_index": "2"}})
        return ents
    ents = extract_chunked(md, plan_chunks(md, 10, lots_per_chunk=5), read)
    ungrounded = [e["attrs"]["lot_index"] for e in ents if e["cls"] == "outstanding"]
    assert ungrounded == ["2", "7"]     # local lot 2 of each chunk


def test_serial_chunks_repeat_the_table_header_and_name_the_serials():
    md = _serial_notice(range(1, 9))
    log = []
    extract_chunked(md, plan_chunks(md, 8, lots_per_chunk=5), _reader(log=log))
    assert log[1]["lots"] == 3
    assert "numbered 6–8 in the notice" in log[1]["extra"]
    assert "lots 6–8" in log[1]["extra"]


def test_serial_chunk_text_carries_the_table_header():
    md = _serial_notice(range(1, 9))
    plan = plan_chunks(md, 8, lots_per_chunk=5)
    t = LC._compose(md, plan, list(range(5, 8)))
    assert "<th>Sr. No</th>" in t.text and "The E-auction" in t.text


# ── the retry ladder ─────────────────────────────────────────────────────────

def test_a_missed_lot_is_reread_on_its_own():
    md = _price_notice(10)
    log = []
    read = _reader(drop=lambda n, real: n == 1 and real in (2, 4), log=log)
    ents = extract_chunked(md, plan_chunks(md, 10, lots_per_chunk=5), read)
    lots = sorted({e["attrs"]["lot_index"] for e in ents if e["cls"] == "borrower"},
                  key=int)
    assert lots == [str(i) for i in range(1, 11)]
    assert log[1]["lots"] == 2 and not log[1]["strong"]   # focused, lots 2 and 4
    assert len(log) == 3                                   # chunk1, retry, chunk2


def test_the_ladder_escalates_then_stops():
    md = _price_notice(10)
    log = []
    # lot 3 is never read, whatever the attempt
    read = _reader(drop=lambda n, real: real == 3, log=log)
    ents = extract_chunked(md, plan_chunks(md, 10, lots_per_chunk=5), read)
    assert "3" not in {e["attrs"]["lot_index"] for e in ents if e["cls"] == "borrower"}
    first_chunk = log[:4]
    assert [r["strong"] for r in first_chunk] == [False, False, True, True]
    assert first_chunk[1]["lots"] == 1 and first_chunk[3]["lots"] == 5
    assert "an earlier read found only 4" in first_chunk[3]["extra"]


def test_a_failed_retry_keeps_what_was_read():
    md = _price_notice(10)
    read = _reader(drop=lambda n, real: n == 1 and real == 2,
                   fail=lambda n: n in (2, 3, 4))
    ents = extract_chunked(md, plan_chunks(md, 10, lots_per_chunk=5), read)
    lots = {e["attrs"]["lot_index"] for e in ents if e["cls"] == "borrower"}
    assert lots == {str(i) for i in range(1, 11)} - {"2"}


def test_retries_zero_keeps_the_first_read():
    md = _price_notice(10)
    read = _reader(drop=lambda n, real: real != 1 and real != 6)
    ents = extract_chunked(md, plan_chunks(md, 10, lots_per_chunk=5), read,
                           retries=0)
    assert len([e for e in ents if e["cls"] == "borrower"]) == 2


def test_lots_read_counts_distinct_lots_with_evidence():
    ents = [{"cls": "borrower", "attrs": {"lot_index": "1"}},
            {"cls": "extent", "attrs": {"lot_index": "1"}},
            {"cls": "borrower", "attrs": {"lot_index": "2"}},
            {"cls": "contact", "attrs": {}}]
    assert LC.lots_read(ents) == 2


# ── fix: a half-read lot is retried, and only a full read replaces it ────────

def test_a_lot_without_its_full_description_is_retried_and_replaced():
    md = _price_notice(10)
    log = []
    read = _reader(partial=lambda n, real: n == 1 and real == 3, log=log)
    ents = extract_chunked(md, plan_chunks(md, 10, lots_per_chunk=5), read)
    lot3 = [e for e in ents if e["attrs"].get("lot_index") == "3"]
    assert {e["cls"] for e in lot3} == {"borrower", "full_description",
                                        "auction_terms"}
    assert {e["attrs"]["call"] for e in lot3} == {2}      # the retry's read, whole
    assert log[1]["lots"] == 1


def test_a_partial_retry_does_not_replace_a_partial_read():
    md = _price_notice(10)
    # lot 3 never comes back with its full_description
    read = _reader(partial=lambda n, real: real == 3)
    ents = extract_chunked(md, plan_chunks(md, 10, lots_per_chunk=5), read)
    lot3 = [e for e in ents if e["attrs"].get("lot_index") == "3"]
    assert [e["cls"] for e in lot3] == ["borrower", "auction_terms"]
    assert {e["attrs"]["call"] for e in lot3} == {1}      # the first read kept


def test_a_partial_retry_fills_a_lot_that_had_nothing():
    md = _price_notice(10)
    read = _reader(drop=lambda n, real: n == 1 and real == 3,
                   partial=lambda n, real: real == 3)
    ents = extract_chunked(md, plan_chunks(md, 10, lots_per_chunk=5), read)
    assert [e["cls"] for e in ents
            if e["attrs"].get("lot_index") == "3"] == ["borrower", "auction_terms"]


# ── fix: a continuation lot inherits the borrower before it ──────────────────

def _continuation_notice() -> str:
    lots = []
    for i in range(1, 9):
        who = ("" if i in (2, 5) else
               f"Name of the Borrower : Mr B{i}\n")
        lots.append(f"{who}No.{i} Property {i}: land at Sy No {i}/1.\n"
                    f"Reserve price: Rs.{i},00,000/-\n")
    return "# SALE NOTICE\n\n" + "\n".join(lots) + "\nTerms.\n"


def _borrowers_by_name(md):
    """A reader that reads the borrower's NAME when the lot states one."""
    def read(text, lots, extra=None, strong=False):
        out, local = [], 0
        for m in re.finditer(r"(Mr B\d+\n)?No\.(\d+) Property", text):
            local += 1
            span = (m.start(2), m.end(2))
            out.append({"cls": "full_description", "text": "x", "start": span[0],
                        "end": span[1], "attrs": {"lot_index": str(local)}})
            if m.group(1):
                out.append({"cls": "borrower", "text": m.group(1).strip(),
                            "start": m.start(1), "end": m.start(1) + 5,
                            "attrs": {"lot_index": str(local)}})
        return out
    return read


def test_a_lot_that_names_no_borrower_inherits_the_one_before():
    md = _continuation_notice()
    plan = plan_chunks(md, 8, lots_per_chunk=5)
    ents = extract_chunked(md, plan, _borrowers_by_name(md))
    by_lot = {}
    for e in ents:
        if e["cls"] == "borrower":
            by_lot.setdefault(e["attrs"]["lot_index"], []).append(e)
    assert set(by_lot) == {str(i) for i in range(1, 9)}
    assert by_lot["2"][0]["text"] == "Mr B1"
    assert by_lot["2"][0]["attrs"]["inherited_from_lot"] == "1"
    assert by_lot["5"][0]["text"] == "Mr B4"
    assert "inherited_from_lot" not in by_lot["3"][0]["attrs"]


def test_a_lot_that_names_a_borrower_the_model_missed_inherits_nothing():
    md = _continuation_notice()
    plan = plan_chunks(md, 8, lots_per_chunk=5)

    def read(text, lots, extra=None, strong=False):
        ents = _borrowers_by_name(md)(text, lots, extra, strong)
        return [e for e in ents if not (e["cls"] == "borrower"
                                        and e["text"] == "Mr B3")]
    ents = extract_chunked(md, plan, read, retries=0)
    lot3 = [e for e in ents if e["cls"] == "borrower"
            and e["attrs"]["lot_index"] == "3"]
    assert lot3 == []            # its text says "Borrower": a gap, not a continuation


# ── fix: a lot whose text quotes a price is not done without its reserve ─────

def test_a_lot_read_without_its_reserve_is_retried_and_replaced():
    md = _price_notice(10)
    log = []
    read = _reader(no_reserve=lambda n, real: n == 1 and real == 4, log=log)
    ents = extract_chunked(md, plan_chunks(md, 10, lots_per_chunk=5), read)
    lot4 = [e for e in ents if e["attrs"].get("lot_index") == "4"]
    assert {e["cls"] for e in lot4} == {"borrower", "full_description",
                                        "auction_terms"}
    assert {e["attrs"]["call"] for e in lot4} == {2}      # the retry's read, whole
    assert log[1]["lots"] == 1 and "reserve price" in log[1]["extra"]


def test_a_table_row_priced_only_beside_its_emd_needs_a_reserve():
    # Tata rows: the amount has no "reserve" label of its own, only the EMD
    md = _serial_notice(range(1, 9))
    log = []
    read = _reader(no_reserve=lambda n, real: n == 1 and real == 2, log=log)
    extract_chunked(md, plan_chunks(md, 8, lots_per_chunk=5), read)
    assert log[1]["lots"] == 1


def test_a_lot_whose_text_quotes_no_price_is_not_held_to_a_reserve():
    # prices listed apart from the lots: nothing in a lot's own text to find
    lots = [f"No.{i} Borrower B{i}. Property at Sy No {i}/1, Rs.{i},00,000/- due.\n"
            for i in range(1, 11)]
    md = "# SALE NOTICE\n\n" + "\n".join(lots) + "\nTerms.\n"
    log = []
    read = _reader(no_reserve=lambda n, real: True, log=log)
    plan = plan_chunks(md, 10, lots_per_chunk=5)
    assert plan is not None
    extract_chunked(md, plan, read)
    assert len(log) == 2                  # one read per chunk, no retries


def test_si_no_is_read_as_sl_no():
    # OCR reads "Sl.No." as "SI.No."; a lot whose outstanding amount and
    # possession follow its price must be cut at its number, not after its price.
    from pipeline.lot_chunks import find_lots
    body = ("SI.No.{n} : BO : Branch {n}, Mr. Borrower {n}. Description of the "
            "property {n}.\nRESERVE PRICE : Rs.{n}0,00,000/- EMD : Rs.{n},00,000/-\n"
            "Outstanding Amount : Rs.{n}5,00,000/- as on 30.06.2026 with further "
            "interest.\nPossession Status : Symbolic Date of Notice under Section "
            "13(2) : 03.08.2017\n\n")
    md = "PUNJAB NATIONAL BANK sale notice.\n\n" + "".join(
        body.format(n=n) for n in range(1, 8))
    strategy, lots, _, _ = find_lots(md, 7)
    assert strategy == "numbered"
    assert all(md[lot.start:lot.end].count("Possession Status") == 1 for lot in lots)
    assert "Symbolic" in md[lots[0].start:lots[0].end]
