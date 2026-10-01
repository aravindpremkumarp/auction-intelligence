"""The model's form -> the stored entity contract, every value proved.

``to_entities`` turns a NoticeRead and a list of (segment, LotRead) into the
``Document.extraction_json`` shape the rest of the pipeline reads
({id, cls, text, start, end, attrs}) with the same classes and attrs the
LangExtract guide declared, plus the evidence attrs (page, block_id, source,
method, evidence, anchor, inherited_from). Nothing is stored that
pipeline/reader/ground.py could not locate inside the lot's own window; a
quote it could not locate is returned in ``dropped``. Numbers come from
pipeline/reader/normalize.py; a mangled figure leaves the number out and
marks the entity ILLEGIBLE.

Long blocks (full_description, full_terms, schedules) are cut by code from
the model's anchors, then widened so every located identifier / extent /
boundary of the lot lies inside — the deterministic fix for the validator's
``full_description_incomplete``. ``text`` is always ``md[start:end]``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pipeline.lot_chunks import _PRICE_LINE
from pipeline.reader import normalize as N
from pipeline.reader.ground import Located, Window, contains, locate
from pipeline.reader.provenance import BlockMap
from pipeline.reader.schema import (SCHEMA_VERSION, Anchors, LotRead, NoticeRead,
                                    SharedTerms)
from pipeline.reader.segment import Segment
from pipeline.reader.tables import Table, cell_for
from pipeline.validators import normalize_identifier_kind

READER = "v2"
_NOTICE_CLASSES = ("secured_creditor", "contact", "emd_account", "full_terms")
_DATE_ATTR = {"auction_start": "auction_start_dt", "auction_end": "auction_end_dt",
              "application_deadline": "application_deadline_dt", "inspection": "inspection_dt"}
_EXTENT_ATTR = {"total_area": "total_area", "built_up_area": "built_up_area",
                "super_built_up_area": "super_built_up_area", "carpet_area": "carpet_area",
                "undivided_share": "undivided_share", "uds_parent_extent": "uds_parent_extent"}
_PLACE_PARTS = ("village", "taluk", "district", "registration_district",
                "registration_sub_district", "hobli", "panchayat", "municipality_corporation",
                "city", "area", "state", "ward_no", "landmark")
MIN_DESCRIPTION_CHARS = 60


@dataclass
class Converted:
    entities: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    not_stated: list[tuple[str, str]] = field(default_factory=list)   # (lot_index, key)
    illegible: list[tuple[str, str]] = field(default_factory=list)


class _Ctx:
    def __init__(self, md: str, blocks, tables: list[Table] | None, prompt_hash: str,
                 model: str) -> None:
        self.md = md
        self.bm = BlockMap(md, blocks)
        self.tables = tables or []
        self.prompt_hash = prompt_hash
        self.model = model
        self.out = Converted()

    # ── one entity ────────────────────────────────────────────────────────
    def emit(self, cls: str, quote: str | None, attrs: dict, win: Window, *,
             lot: str | None, cursor: int = 0, source: str = "prose",
             method: str = "structured_reader", evidence: str = "EXPLICIT",
             span: tuple[int, int] | None = None) -> dict | None:
        if span is not None:
            loc = Located(span[0], span[1], "exact")
        else:
            loc = locate(win, quote, cursor)
        if loc is None:
            self.out.dropped.append({"cls": cls, "quote": quote, "lot_index": lot,
                                     "attrs": {k: v for k, v in attrs.items() if v not in (None, "", [])}})
            return None
        page, block = self.bm.page_and_block(loc.start)
        a = {k: v for k, v in attrs.items() if v not in (None, "", [])}
        if lot is not None:
            a["lot_index"] = lot
        a.update({"evidence": "FUZZY_GROUNDED" if loc.anchor == "fuzzy" and evidence == "EXPLICIT" else evidence,
                  "anchor": loc.anchor, "source": source, "method": method,
                  "reader": READER, "schema_version": SCHEMA_VERSION})
        if page is not None:
            a["page"] = page
        if block is not None:
            a["block_id"] = block
        if self.prompt_hash:
            a["prompt_hash"] = self.prompt_hash
        e = {"id": None, "cls": cls, "text": self.md[loc.start:loc.end],
             "start": loc.start, "end": loc.end, "attrs": a}
        self.out.entities.append(e)
        return e

    def block_span(self, anchors: Anchors | None, win: Window, *, cursor: int = 0,
                   must_cover: list[tuple[int, int]] = (), fallback_end: int | None = None,
                   fallback_start: int | None = None) -> tuple[int, int] | None:
        """The span between two anchors inside ``win``, widened over
        ``must_cover`` spans, or built from the fallbacks."""
        s = e = None
        if anchors is not None:
            a = locate(win, anchors.first_words, cursor)
            if a is not None:
                s = a.start
                b = locate(win.sub(a.start, win.hi), anchors.last_words, a.start)
                if b is not None and b.end > s:
                    e = b.end
        if s is None and fallback_start is not None:
            s = fallback_start
        if s is None and must_cover:
            s = min(x for x, _ in must_cover)
            s = self.md.rfind("\n", win.lo, s) + 1 if self.md.rfind("\n", win.lo, s) >= win.lo else win.lo
        if s is None:
            return None
        if e is None:
            if fallback_end is not None and fallback_end > s:
                e = fallback_end
            elif must_cover and max(y for _, y in must_cover) > s:
                e = max(y for _, y in must_cover)
            else:
                e = win.hi
        for x, y in must_cover:
            if win.lo <= x and y <= win.hi:
                s, e = min(s, x), max(e, y)
        if e - s < MIN_DESCRIPTION_CHARS and not must_cover:
            return None
        return s, e

    # ── one lot ───────────────────────────────────────────────────────────
    def lot(self, seg: Segment, lot: LotRead, win: Window, lot_index: str,
            shared: SharedTerms | None, tables: list[Table]) -> None:
        md = self.md
        out = self.out
        cursor = win.lo
        row_tbl = row = None
        if seg.row is not None and seg.row[0] < len(tables):
            row_tbl = tables[seg.row[0]]
            row = next((r for r in row_tbl.rows if r.index == seg.row[1]), None)

        def cell_window(role: str) -> tuple[Window, str, str | None]:
            if row_tbl is not None and row is not None:
                c = cell_for(row_tbl, row, role)
                if c is not None:
                    col = row_tbl.column(role)
                    return (Window(md, c.start, c.end), f"table:r{row.index}:c{col}",
                            row_tbl.columns[col][1])
                return Window(md, row.start, row.end), f"table:r{row.index}", None
            return win, "prose", None

        # borrowers
        bw, bsrc, _ = cell_window("borrower")
        for b in lot.borrowers:
            e = self.emit("borrower", b.name_quote, {"role": b.role, "address": b.address_quote},
                          bw, lot=lot_index, cursor=cursor, source=bsrc)
            if e is None and bw is not win:
                self.emit("borrower", b.name_quote, {"role": b.role, "address": b.address_quote},
                          win, lot=lot_index, cursor=cursor)

        # granular spans first, so the description can be widened over them
        located: list[tuple[int, int]] = []
        for ident in lot.identifiers:
            kind, _ = normalize_identifier_kind(ident.kind)
            e = self.emit("identifier", ident.quote, {"kind": kind, "value": ident.value},
                          win, lot=lot_index, cursor=cursor)
            if e:
                located.append((e["start"], e["end"]))
        for x in lot.extents:
            attr = _EXTENT_ATTR.get(x.role, "total_area")
            attrs: dict = {attr: x.quote}
            if attr in ("total_area", "built_up_area", "super_built_up_area", "carpet_area"):
                ar = N.area(x.quote)
                if ar.state == "ok" and ar.value.sqft:
                    attrs["extent_sqft"] = str(round(ar.value.sqft, 2))
            ew, src, _ = cell_window("extent")
            e = self.emit("extent", x.quote, attrs, ew if row is not None else win,
                          lot=lot_index, cursor=cursor, source=src)
            if e is None and row is not None:
                e = self.emit("extent", x.quote, attrs, win, lot=lot_index, cursor=cursor)
            if e:
                located.append((e["start"], e["end"]))
        for bd in lot.boundaries:
            q = bd.adjacency_quote or bd.measurement_quote
            e = self.emit("boundary", q, {"side": bd.side, "adjacency": bd.adjacency_quote,
                                          "measurement": bd.measurement_quote},
                          win, lot=lot_index, cursor=cursor)
            if e:
                located.append((e["start"], e["end"]))
        if lot.location is not None:
            parts = {k: getattr(lot.location, k) for k in _PLACE_PARTS}
            kept = {k: v for k, v in parts.items() if v and contains(win, v)}
            dropped_parts = sorted(k for k, v in parts.items() if v and k not in kept)
            e = self.emit("location", lot.location.quote, kept, win, lot=lot_index, cursor=cursor)
            if e and dropped_parts:
                e["attrs"]["inferred_dropped"] = ",".join(dropped_parts)
            if e:
                located.append((e["start"], e["end"]))
        for sc in lot.schedules:
            sp = self.block_span(sc.anchors, win, cursor=cursor)
            q = None if sp else sc.extent_quote
            e = self.emit("schedule", q, {"label": sc.label, "type": sc.type,
                                          "extent": sc.extent_quote}, win,
                          lot=lot_index, cursor=cursor, span=sp)
            if e:
                located.append((e["start"], e["end"]))

        # full_description from anchors, widened over the granular spans
        price = _PRICE_LINE.search(md, win.lo, win.hi)
        fd = self.block_span(lot.description, win, cursor=cursor, must_cover=located,
                             fallback_end=(price.start() if price else None))
        if fd:
            self.emit("full_description", None, {}, win, lot=lot_index, span=fd)
        else:
            out.dropped.append({"cls": "full_description", "quote": None, "lot_index": lot_index})

        # property
        pattrs: dict = {"property_type": lot.property_type.quote if lot.property_type.status == "found" else None,
                        "property_type_norm": lot.property_type_norm, "asset_category": "immovable",
                        "construction_type": lot.construction_type,
                        "occupancy_status": lot.occupancy_status, "branch_of_lot": lot.branch_of_lot,
                        "title_deed_holder": lot.title_deed_holder_quote,
                        "encumbrance": lot.encumbrance_quote, "portal_aid": lot.portal_aid,
                        "lot_label": lot.lot_label}
        inherited: list[str] = []
        poss = N.possession(lot.possession.quote) if lot.possession.status == "found" else N.NONE
        if poss.state == "ok":
            pattrs["possession_type"] = poss.value
        elif (lot.possession.status == "found" and lot.possession_type
              and lot.possession_type in (lot.possession.quote or "").lower().replace(" ", "")):
            # "PhysicalPossession" (OCR dropped the space): the quote names one
            # kind and the model read it; the word is there, so it is stated.
            pattrs["possession_type"] = lot.possession_type
        elif shared is not None and shared.possession is not None:
            sp = N.possession(shared.possession.quote)
            if sp.state == "ok":
                pattrs["possession_type"] = sp.value
                pattrs["from_notice_header"] = "true"
                inherited.append("possession_type")
        for d in lot.dates:
            if d.event == "possession_date" and d.status == "found":
                nd = N.date(d.quote, d.iso)
                if nd.state == "ok":
                    pattrs["possession_date"] = nd.value
        states: dict[str, str] = {}
        for key, fact in (("property_type", lot.property_type), ("possession_type", lot.possession)):
            if fact.status == "not_stated" and not pattrs.get(key) and key not in inherited:
                states[key] = "not_stated"
                out.not_stated.append((lot_index, key))
            elif fact.status == "illegible":
                states[key] = "illegible"
                out.illegible.append((lot_index, key))
        # The property entity's own span: the type phrase when there is one,
        # else the head of the description block (so the coverage rule that
        # every descriptive span lies inside full_description holds), else
        # the possession sentence.
        pq = lot.property_type.quote if lot.property_type.status == "found" else None
        pspan = None
        if not pq:
            if fd:
                pspan = (fd[0], min(fd[1], fd[0] + 80))
            elif lot.possession.status == "found":
                pq = lot.possession.quote
        if pq or pspan:
            if inherited:
                pattrs["inherited"] = ",".join(inherited)
                pattrs["inherited_from"] = "notice.possession"
            for k, v in states.items():
                pattrs[f"{k}_state"] = v
            self.emit("property", pq, pattrs, win, lot=lot_index, cursor=cursor, span=pspan,
                      evidence="ILLEGIBLE" if "illegible" in states.values() else "EXPLICIT")

        # auction_terms: money + dates
        tattrs: dict = {}
        tstates: dict[str, str] = {}
        money_quote = None
        money_win, money_src, money_unit = cell_window("reserve_price")
        for key, fact, attr in (("reserve_price", lot.reserve_price, "reserve_price_num"),
                                ("emd", lot.emd, "emd_num"),
                                ("bid_increment", lot.bid_increment, "bid_increment_num")):
            if fact is None:
                continue
            # The model's own "illegible" is a hint, not a verdict: a figure it
            # could not read may still be one the narrow OCR repairs recover
            # (gold 750348, "35.15,000"). Code decides, and marks the repair.
            if fact.status in ("found", "illegible") and fact.quote and N.money(
                    fact.quote, fact.unit).state == "ok" or (fact.status == "found" and fact.quote):
                unit = fact.unit or (money_unit if key in ("reserve_price", "emd") else None)
                m = N.money(fact.quote, unit)
                if m.state == "ok":
                    tattrs[attr] = str(m.value)
                    if N.grouping_repaired(fact.quote):
                        tattrs[f"{key}_ocr_repaired"] = "true"
                    money_quote = money_quote or fact.quote
                else:
                    tstates[key] = m.state if m.state == "illegible" else "not_stated"
            elif fact.status == "not_stated":
                tstates[key] = "not_stated"
            elif fact.status == "illegible":
                tstates[key] = "illegible"
                money_quote = money_quote or fact.quote
        lot_events = set()
        for d in lot.dates:
            if d.status != "found" or d.event not in _DATE_ATTR:
                continue
            nd = N.date(d.quote, d.iso)
            if nd.state == "ok":
                tattrs[_DATE_ATTR[d.event]] = nd.value
                lot_events.add(d.event)
                out.timeline.append({"event": d.event, "date": nd.value, "quote": d.quote,
                                     "lot_index": lot_index, "evidence": "EXPLICIT",
                                     "source": "lot"})
        tinherited = []
        if shared is not None:
            for d in shared.dates:
                if d.status != "found" or d.event not in _DATE_ATTR or d.event in lot_events:
                    continue
                nd = N.date(d.quote, d.iso)
                if nd.state == "ok":
                    tattrs[_DATE_ATTR[d.event]] = nd.value
                    tinherited.append(_DATE_ATTR[d.event])
                    out.timeline.append({"event": d.event, "date": nd.value, "quote": d.quote,
                                         "lot_index": lot_index, "evidence": "INHERITED",
                                         "source": "notice"})
        if "reserve_price" in tstates:
            if tstates["reserve_price"] == "not_stated":
                out.not_stated.append((lot_index, "reserve_price"))
            else:
                out.illegible.append((lot_index, "reserve_price"))
        if "auction_start_dt" not in tattrs:
            out.not_stated.append((lot_index, "auction_date"))
        for k, v in tstates.items():
            tattrs[f"{k}_state"] = v
        if tinherited:
            tattrs["inherited"] = ",".join(tinherited)
            tattrs["inherited_from"] = "notice.shared"
        if tattrs:
            q = money_quote
            first_date = next((d.quote for d in lot.dates if d.status == "found"), None)
            ev = "ILLEGIBLE" if "illegible" in tstates.values() else "EXPLICIT"
            e = None
            if q:
                e = self.emit("auction_terms", q, tattrs, money_win, lot=lot_index, cursor=cursor,
                              source=money_src, evidence=ev)
                if e is None and money_win is not win:
                    e = self.emit("auction_terms", q, tattrs, win, lot=lot_index, cursor=cursor,
                                  evidence=ev)
            if e is None and first_date:
                e = self.emit("auction_terms", first_date, tattrs, win, lot=lot_index, cursor=cursor,
                              evidence=ev)
            if e is None and fd:
                e = self.emit("auction_terms", None, tattrs, win, lot=lot_index,
                              span=(fd[0], min(fd[1], fd[0] + 40)), evidence="INHERITED"
                              if tinherited and not tattrs.get("reserve_price_num") else ev)

        # outstanding
        if lot.outstanding is not None and lot.outstanding.status == "found" and lot.outstanding.quote:
            oattrs: dict = {}
            m = N.money(lot.outstanding.quote, lot.outstanding.unit)
            if m.state == "ok":
                oattrs["amount_num"] = str(m.value)
            for d in lot.dates:
                if d.status == "found" and d.event in ("outstanding_as_on", "demand_notice_date"):
                    nd = N.date(d.quote, d.iso)
                    if nd.state == "ok":
                        oattrs["as_on" if d.event == "outstanding_as_on" else "demand_notice_date"] = nd.value
            if lot.loan_account_nos:
                oattrs["loan_account_no"] = lot.loan_account_nos[0]
            ow, osrc, _ = cell_window("outstanding")
            e = self.emit("outstanding", lot.outstanding.quote, oattrs, ow if row is not None else win,
                          lot=lot_index, cursor=cursor, source=osrc,
                          evidence="ILLEGIBLE" if m.state == "illegible" else "EXPLICIT")
            if e is None and row is not None:
                self.emit("outstanding", lot.outstanding.quote, oattrs, win, lot=lot_index, cursor=cursor)

        for x in lot.extras[:5]:
            self.emit("extras", x.value_quote, {"key": x.key, "value": x.value_quote}, win,
                      lot=lot_index, cursor=cursor)

    # ── notice level ──────────────────────────────────────────────────────
    def notice(self, n: NoticeRead, head: Window, tail: Window) -> None:
        whole = Window(self.md)
        sattrs = {"legal_basis": n.legal_basis, "bank_name": n.bank_name_quote, "branch": n.branch,
                  "authorised_officer": n.authorised_officer, "assignor_bank": n.assignor_bank,
                  "trust_name": n.trust_name, "liquidator": n.liquidator,
                  "court_reference": n.court_reference, "predecessor_entity": n.predecessor_entity,
                  "sale_terms": n.sale_terms_quote, "auction_platform_url": n.auction_platform_url}
        for d in n.shared.dates:
            if d.status == "found" and d.event in ("assignment_date", "notice_date"):
                nd = N.date(d.quote, d.iso)
                if nd.state == "ok":
                    sattrs[d.event] = nd.value
                    self.out.timeline.append({"event": d.event, "date": nd.value, "quote": d.quote,
                                              "lot_index": None, "evidence": "EXPLICIT",
                                              "source": "notice"})
        q = n.bank_name_quote or n.authorised_officer or n.sale_terms_quote
        if q:
            e = self.emit("secured_creditor", q, sattrs, head, lot=None, source="header")
            if e is None:
                self.emit("secured_creditor", q, sattrs, whole, lot=None, source="header")
        for c in n.contacts:
            e = self.emit("contact", c.quote, {"phones": ", ".join(c.phones) or None, "email": c.email},
                          tail, lot=None, source="tail")
            if e is None:
                self.emit("contact", c.quote, {"phones": ", ".join(c.phones) or None, "email": c.email},
                          whole, lot=None, source="tail")
        if n.emd_account is not None:
            a = n.emd_account
            attrs = {"account_name": a.account_name, "account_no": a.account_no, "ifsc": a.ifsc,
                     "bank": a.bank, "mode_of_payment": a.mode_of_payment}
            e = self.emit("emd_account", a.quote, attrs, tail, lot=None, source="tail")
            if e is None:
                self.emit("emd_account", a.quote, attrs, whole, lot=None, source="tail")
        if n.terms is not None:
            sp = self.block_span(n.terms, tail) or self.block_span(n.terms, whole)
            if sp:
                self.emit("full_terms", None, {}, whole, lot=None, span=sp, source="tail")
            else:
                self.out.dropped.append({"cls": "full_terms", "quote": n.terms.first_words,
                                         "lot_index": None})


def _lot_windows(md: str, lot_reads: list[tuple[Segment, LotRead]]) -> list[Window]:
    """One window per lot. Real segments are their own windows; the lots of
    a whole read are fenced by where their descriptions were found, so a
    quote cannot settle in a neighbour even then."""
    segs = [s for s, _ in lot_reads]
    distinct = len({(s.start, s.end) for s in segs}) == len(segs)
    if distinct:
        return [Window(md, s.start, s.end) for s in segs]
    whole = Window(md)
    starts: list[int | None] = []
    cursor = 0
    for _, lot in lot_reads:
        hit = locate(whole, lot.description.first_words, cursor) if lot.description else None
        if hit is not None and hit.start >= cursor:
            starts.append(hit.start)
            cursor = hit.end
        else:
            starts.append(None)
    if any(s is None for s in starts) or len(starts) < 2:
        return [whole for _ in lot_reads]
    bounds = []
    for i, s in enumerate(starts):
        lo = 0 if i == 0 else max(s - 1500, starts[i - 1])
        hi = len(md) if i == len(starts) - 1 else starts[i + 1]
        bounds.append(Window(md, lo, hi))
    return bounds


def to_entities(notice: NoticeRead | None, lot_reads: list[tuple[Segment, LotRead]], md: str, *,
                blocks: list[dict] | None = None, tables: list[Table] | None = None,
                prompt_hash: str = "", model: str = "", head_end: int | None = None,
                tail_start: int | None = None) -> Converted:
    ctx = _Ctx(md, blocks, tables, prompt_hash, model)
    shared = notice.shared if notice is not None else None
    windows = _lot_windows(md, lot_reads)
    for i, ((seg, lot), win) in enumerate(zip(lot_reads, windows)):
        ctx.lot(seg, lot, win, str(i + 1), shared, ctx.tables)
    if notice is not None:
        head = Window(md, 0, head_end if head_end else min(len(md), 6000))
        tail = Window(md, tail_start if tail_start is not None else max(0, len(md) - 8000), len(md))
        ctx.notice(notice, head, tail)
    # notice-level entities first, then lots in order — the shape promote expects
    ents = ctx.out.entities
    ctx.out.entities = ([e for e in ents if e["cls"] in _NOTICE_CLASSES]
                        + [e for e in ents if e["cls"] not in _NOTICE_CLASSES])
    return ctx.out


__all__ = ["to_entities", "Converted", "READER", "MIN_DESCRIPTION_CHARS"]
