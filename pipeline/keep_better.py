"""Only ever improve a stored extraction.

A re-extraction replaces a notice's whole entity list. A new read often gains
something (a reserve price the last read skipped) and loses something else (a
location it had), so saving it blindly makes the corpus drift instead of
improve. ``judge`` compares the stored read with the new one on what a notice
is worth, and says whether the new one may replace it:

1. **Lots.** With a reviewer lot count, the read whose lot count is closer to
   it wins outright — a notice split into the wrong number of lots is wrong
   whatever else it holds, and its lots cannot be compared one to one. Without
   a count, a read that finds fewer lots loses.
2. **Key facts per lot** (pipeline/key_entities: reserve price, auction date,
   property type, location, extent, full description, possession). A fact the
   stored read had for a lot and the new one lacks is a loss; the reverse is a
   gain. So is each part of a location that places it on the register — its
   village, taluk and sub-registrar office (key_entities.LOCATION_PARTS).
3. **Validator score** (pipeline/validators). Catches what the checklist does
   not: wrong values, ungrounded spans, over-split extras.

The new read is saved only with at least one gain and no loss. A read that is
merely as good is not saved either: saving resets the review status, and
churn with nothing to show for it is a cost.

When neither read is better as a whole — each has facts the other lacks —
``merge`` builds a third: one read with the other's missing key facts filled
in, lot by lot, and ``best`` picks it when it is better than the stored read.
Both reads point into the same notice text, so their spans combine; a run can
then only add facts, never trade one for another.

The caller decides when the comparison applies. It does not when the notice's
text changed since the stored read — the old spans point into a text that is
gone, so the new read wins by default.
"""
from __future__ import annotations

import copy
import re

from pipeline.key_entities import (
    KEY_ATTR, KEY_CLASS, KEY_ENTITIES, KEY_LABELS, LOCATION_PARTS, key_checklist,
    location_parts,
)
from pipeline.validators import validate_stored

_PART_LABELS = {"village": "village", "taluk": "taluk",
                "registration_sub_district": "sub-registrar office"}
#: Carried with a merged location part when the stored lot lacks them too.
_PART_PASSENGERS = ("district", "registration_district")


def lot_count(ents: list[dict]) -> int:
    return len({str((e.get("attrs") or {}).get("lot_index"))
                for e in ents
                if (e.get("attrs") or {}).get("lot_index") not in (None, "")})


def _filled(ents: list[dict]) -> dict[str, set[str]]:
    """lot_index -> the key facts the read carries for that lot."""
    out: dict[str, set[str]] = {}
    for lot in key_checklist(ents)["lots"]:
        if lot.get("extracted"):
            out[str(lot["lot_index"])] = {
                k for k, c in lot["cells"].items() if c["status"] == "filled"}
    return out


# ── is a reserve price really that lot's? ────────────────────────────────────
#
# A re-read that "found" a missing price was always a gain, so a model that
# copied lot 3's price line onto lot 4, or wrote a figure printed nowhere in
# the notice, beat the stored read and the wrong price went live. A price now
# counts only when the notice prints that amount somewhere, and no other lot's
# price stands on the very same span, unless the span itself names that many
# lots ("Plot No.16 & 17 - Rs.13,50,000").

_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(crores?|cr\b|lakhs?|lacs?)?", re.I)
#: Separators an OCR'd table puts inside one number: "1,82,00,<br/>000",
#: "19,74, 375".
_IN_NUMBER = re.compile(r"(?<=\d)(?:[,\s]|<br\s*/?>)+(?=\d)", re.I)
_LOT_LABEL = re.compile(
    r"\b(?:plot|lot|item|site|flat|unit|door|property|schedule|sl|sr|s)\.?\s*"
    r"(?:no|nos|number)?\.?\s*[:.\-]?\s*"
    r"(\d+[a-z]?(?:\s*(?:&|and|,|/)\s*\d+[a-z]?)*)", re.I)


def _amounts(text: str) -> set[int]:
    """Every amount ``text`` prints, as rupees: "17,80,000", "17.8 Lakh",
    "1.5 Cr." — and numbers broken across a table cell joined back up."""
    out = set()
    for src in (text or "", _IN_NUMBER.sub("", text or "")):
        for num, unit in _AMOUNT.findall(src):
            u = (unit or "").lower()
            scale = 1e7 if u.startswith("cr") else 1e5 if u.startswith(("lakh", "lac")) else 1
            # OCR swaps the separators: "1.94,000", "89,00.000", "31,50 Lakh"
            for reading in (num.replace(",", ""), num.replace(",", ".").rstrip("."),
                            num.replace(",", "").replace(".", "")):
                try:
                    v = float(reading) * scale
                except ValueError:
                    continue
                if v < 1e13:   # an account number joined up is no price
                    out.add(round(v))
    return out


def _price_value(e: dict) -> int | None:
    try:
        return round(float(str((e.get("attrs") or {}).get("reserve_price_num")).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _offsets(e: dict, text: str) -> tuple[int, int] | None:
    s, t = e.get("start"), e.get("end")
    if isinstance(s, int) and isinstance(t, int) and 0 <= s < t <= len(text):
        return s, t
    return None


def _named_lots(span: str) -> int:
    return sum(len(re.findall(r"\d+", m)) for m in _LOT_LABEL.findall(span or ""))


def price_doubts(ents: list[dict], text: str) -> dict[str, str]:
    """lot_index -> why its reserve price is not evidence, for every lot whose
    price fails the check above. Lots whose price holds, or that have none, are
    not listed. Without notice text nothing can be checked, so nothing is."""
    if not text:
        return {}
    printed = _amounts(text)
    value: dict[str, int] = {}
    where: dict[str, tuple[int, int]] = {}
    for e in ents:
        v = _price_value(e) if e.get("cls") == "auction_terms" else None
        if v is None:
            continue
        li = _lot(e)
        value.setdefault(li, v)
        at = _offsets(e, text)
        if v == value[li] and at and li not in where:
            where[li] = at
    doubts = {li: "not printed anywhere in the notice"
              for li, v in value.items() if v not in printed}
    on_span: dict[tuple[int, int], set[str]] = {}
    for li, at in where.items():
        if li not in doubts:
            on_span.setdefault(at, set()).add(li)
    for (s, t), lots in on_span.items():
        if len(lots) > 1 and _named_lots(text[s:t]) < len(lots):
            names = ", ".join(sorted(lots, key=lambda x: (len(x), x)))
            for li in lots:
                doubts[li] = f"price line shared by lots {names}"
    return doubts


def _credited(ents: list[dict], text: str) -> dict[str, set[str]]:
    """``_filled``, less any reserve price that is not that lot's own."""
    out = {li: set(keys) for li, keys in _filled(ents).items()}
    for li in price_doubts(ents, text):
        out.get(li, set()).discard("reserve_price")
    return out


#: Half a fact per lot: a re-split that merges duplicate lots may shift the
#: average a little without being worse.
_COVERAGE_SLACK = 0.5


def coverage(ents: list[dict]) -> float:
    """Key facts per lot: what a read is worth when its spans cannot be read
    against the current notice text (see :func:`stale_losses`)."""
    filled = _filled(ents)
    return sum(len(v) for v in filled.values()) / len(filled) if filled else 0.0


def stale_losses(old: list[dict], new: list[dict],
                 expected_lot_count: int | None = None) -> list[str]:
    """Why ``new`` must not replace ``old`` when the notice text changed since
    ``old`` was read. The spans no longer line up, so :func:`judge` cannot
    run, but a read that finds fewer key facts per lot, or strays further
    from the reviewer's lot count, is still worse whatever text it read."""
    if not old:
        return []
    if not new:
        return ["new read is empty"]
    losses = []
    n_old, n_new = lot_count(old), lot_count(new)
    if expected_lot_count:
        exp = int(expected_lot_count)
        d_old, d_new = abs(n_old - exp), abs(n_new - exp)
        if d_new > d_old:
            return [f"lots {n_old} → {n_new} (expected {exp})"]
        if d_new < d_old:
            return []   # the right lot count wins outright, as in `judge`
    c_old, c_new = coverage(old), coverage(new)
    t_old = sum(len(v) for v in _filled(old).values())
    t_new = sum(len(v) for v in _filled(new).values())
    # A re-split that folds duplicate lots loses facts in total but not per
    # lot; a thinner read loses both.
    if c_new < c_old - _COVERAGE_SLACK or (c_new < c_old and t_new < t_old):
        losses.append(f"key facts {t_old} → {t_new} ({c_old:.1f} → {c_new:.1f} per lot)")
    return losses


def judge(old: list[dict], new: list[dict], text: str,
          expected_lot_count: int | None = None
          ) -> tuple[bool, list[str], list[str]]:
    """``(save, gains, losses)`` for replacing ``old`` with ``new``.

    ``gains`` and ``losses`` are short human-readable reasons ("lot 3:
    reserve price", "score 56 → 36"); the caller prints them.
    """
    if not old:
        return True, ["no stored extraction"], []
    if not new:
        return False, [], ["new read is empty"]

    gains: list[str] = []
    losses: list[str] = []

    # A price the new read puts on a lot that is not that lot's own — copied
    # from a sibling, or printed nowhere — is never a gain, and blocks the
    # read outright unless the stored read already held that same price.
    new_doubts = price_doubts(new, text)
    old_price = {_lot(e): _price_value(e) for e in reversed(old)
                 if e.get("cls") == "auction_terms" and _price_value(e) is not None}
    new_price = {_lot(e): _price_value(e) for e in reversed(new)
                 if e.get("cls") == "auction_terms" and _price_value(e) is not None}
    bad = [f"lot {li}: reserve price {new_price.get(li)} — {why}"
           for li, why in sorted(new_doubts.items(), key=lambda kv: (len(kv[0]), kv[0]))
           if old_price.get(li) != new_price.get(li)]
    if bad:
        return False, [], bad

    n_old, n_new = lot_count(old), lot_count(new)
    if n_new != n_old:
        tag = f"lots {n_old} → {n_new}"
        if expected_lot_count:
            exp = int(expected_lot_count)
            d_old, d_new = abs(n_old - exp), abs(n_new - exp)
            if d_new < d_old:
                return True, [f"{tag} (expected {exp})"], []
            if d_new > d_old:
                return False, [], [f"{tag} (expected {exp})"]
        elif n_new < n_old:
            return False, [], [tag]

    had, has = _credited(old, text), _credited(new, text)
    for li in sorted(set(had) | set(has), key=lambda s: (len(s), s)):
        before, after = had.get(li, set()), has.get(li, set())
        losses += [f"lot {li}: {KEY_LABELS[k].lower()}"
                   for k in sorted(before - after)]
        gains += [f"lot {li}: {KEY_LABELS[k].lower()}"
                  for k in sorted(after - before)]
    had_p, has_p = location_parts(old), location_parts(new)
    for li in sorted(set(had_p) | set(has_p), key=lambda s: (len(s), s)):
        before, after = had_p.get(li, set()), has_p.get(li, set())
        losses += [f"lot {li}: {_PART_LABELS[k]}" for k in sorted(before - after)]
        gains += [f"lot {li}: {_PART_LABELS[k]}" for k in sorted(after - before)]

    s_old = validate_stored(old, source_text=text)["score"]
    s_new = validate_stored(new, source_text=text)["score"]
    if s_new < s_old:
        losses.append(f"score {s_old} → {s_new}")
    elif s_new > s_old:
        gains.append(f"score {s_old} → {s_new}")

    return (bool(gains) and not losses), gains, losses


def _lot(e: dict) -> str:
    return str((e.get("attrs") or {}).get("lot_index") or "1")


def _has(v) -> bool:
    return v is not None and str(v).strip() not in ("", "None", "null")


def _fold(v) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


def _in_span(value, text) -> bool:
    """The value is written in the span that carries it — "Adhanur" in
    "Adhanur Village, Kundrathur Taluk" — not a spelling the model made up."""
    return bool(_fold(value)) and _fold(value) in _fold(text)


def _location_attrs(ents: list[dict], lot: str) -> set[str]:
    return {k for e in ents if e.get("cls") == "location" and _lot(e) == lot
            for k, v in (e.get("attrs") or {}).items() if _has(v)}


def merge(base: list[dict], donor: list[dict], text: str = "") -> list[dict]:
    """``base`` with every key fact it lacks, lot by lot, taken from ``donor``.

    Only lots both reads hold are filled, so lot numbering must agree — the
    caller merges only reads with the same lot count. A fact carried by an
    attribute (reserve price, auction date, property type, possession) is set
    on ``base``'s own entity of that class when it has one, so a lot never
    ends up with two auction_terms or two properties; otherwise the donor's
    entity is added. ``base`` is not modified.

    With ``text``, a donor reserve price that is not its lot's own
    (:func:`price_doubts`) is not carried over.
    """
    out = copy.deepcopy(base)
    had = _filled(base)
    gave = _credited(donor, text)
    carried_price: set[str] = set()
    used = {str(e.get("id")) for e in out if e.get("id") is not None}
    nxt = [0]

    def fresh_id() -> str:
        while str(nxt[0]) in used or f"m{nxt[0]}" in used:
            nxt[0] += 1
        used.add(f"m{nxt[0]}")
        return f"m{nxt[0]}"

    # which lots' prices already stand on each span of ``base``
    priced: dict[tuple[int, int], set[str]] = {}
    for e in base:
        at = _offsets(e, text) if text and e.get("cls") == "auction_terms" else None
        if at and _price_value(e) is not None:
            priced.setdefault(at, set()).add(_lot(e))

    for li in sorted(set(had) & set(gave)):
        for key in (k for k, *_ in KEY_ENTITIES):
            if key in had[li] or key not in gave[li]:
                continue
            cls, attr = KEY_CLASS[key], KEY_ATTR[key]
            source = next((e for e in donor if e.get("cls") == cls
                           and _lot(e) == li
                           and (_has((e.get("attrs") or {}).get(attr))
                                if attr else _has(e.get("text")))), None)
            if source is None:
                continue
            if key == "reserve_price" and text:
                # the donor's price line must not be one a sibling lot of
                # ``base`` already stands on (lot 3's price copied to lot 4)
                at = _offsets(source, text)
                if at:
                    others = priced.get(at, set()) - {li}
                    if others and _named_lots(text[at[0]:at[1]]) < len(others) + 1:
                        continue
                    priced.setdefault(at, set()).add(li)
            if key == "reserve_price":
                carried_price.add(li)
            target = next((e for e in out if e.get("cls") == cls
                           and _lot(e) == li), None) if attr else None
            if target is not None:
                attrs = target.setdefault("attrs", {})
                attrs[attr] = source["attrs"][attr]
                # which attributes came from the other read, for a reviewer
                attrs["merged_attrs"] = ",".join(sorted(
                    {*filter(None, str(attrs.get("merged_attrs") or "").split(",")),
                     attr}))
            else:
                e = copy.deepcopy(source)
                e["id"] = fresh_id()
                e.setdefault("attrs", {})["merged"] = "true"
                # an auction_terms copied for its date must not smuggle in a
                # price that was refused above
                if (cls == "auction_terms" and li not in carried_price
                        and "reserve_price" not in had[li]):
                    e["attrs"].pop("reserve_price_num", None)
                out.append(e)
        # A location both reads hold, but without the parts that place it:
        # the other read's span for them is added beside it, carrying only
        # what this lot lacks, so no value the stored read holds is replaced.
        if "location" not in had[li]:
            continue
        lacks = [k for k in LOCATION_PARTS if k not in _location_attrs(out, li)]
        for source in (e for e in donor if e.get("cls") == "location"
                       and _lot(e) == li and lacks):
            a, text = source.get("attrs") or {}, source.get("text")
            got = {k: a[k] for k in lacks if _has(a.get(k)) and _in_span(a[k], text)}
            if not got:
                continue
            have = _location_attrs(out, li)
            got.update({k: a[k] for k in _PART_PASSENGERS
                        if k not in have and _has(a.get(k)) and _in_span(a[k], text)})
            e = copy.deepcopy(source)
            e["id"] = fresh_id()
            e["attrs"] = {"lot_index": li, **got, "merged": "true",
                          "merged_attrs": ",".join(sorted(got)),
                          **({"gap_fill": a["gap_fill"]} if a.get("gap_fill") else {})}
            out.append(e)
            lacks = [k for k in lacks if k not in got]
    return out


def best(old: list[dict], new: list[dict], text: str,
         expected_lot_count: int | None = None
         ) -> tuple[list[dict] | None, str, list[str], list[str]]:
    """The read to store, if any beats ``old``: ``(entities, how, gains,
    losses)`` with ``how`` one of "new", "merged" or "" (keep ``old``).

    The new read as it stands is tried first. When it loses something, the two
    merges are tried — the stored read filled from the new one, and the new
    read filled from the stored one — and the higher-scoring one that beats
    the stored read is taken.
    """
    save, gains, losses = judge(old, new, text, expected_lot_count)
    if save:
        return new, "new", gains, losses
    if not old or not new or lot_count(old) != lot_count(new):
        return None, "", gains, losses
    candidates = []
    for merged in (merge(old, new, text), merge(new, old, text)):
        ok, g, l = judge(old, merged, text, expected_lot_count)
        if ok:
            score = validate_stored(merged, source_text=text)["score"]
            candidates.append((score, len(g), merged, g, l))
    if not candidates:
        return None, "", gains, losses
    _, _, merged, g, l = max(candidates, key=lambda c: (c[0], c[1]))
    return merged, "merged", g, l
