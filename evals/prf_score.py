"""Precision / recall / F1 scoring for LangExtract output — the "count the wrong
answers too" grader.

The legacy scorer (evals/langextract_eval.score_records) is RECALL-ONLY: for each
gold value it asks "did this appear somewhere in the extraction?" and stops there.
A model that emits the right value *plus five wrong ones* scores identically to a
model that emits only the right value. That hides the failure mode heavy prompts
actually produce: over-emission (spraying candidates) and slotting errors (right
value, wrong field).

This module adds the missing half — false positives — for the fields where the
gold is genuinely CLOSED-WORLD, so an extra value is unambiguously wrong:

  * single-valued scalars on SINGLE notices (legal_basis, bank_name, village,
    taluk, district, reg-district, possession_type, ...): a single-lot notice has
    exactly ONE correct value, so every *other* distinct value the model emits for
    that field is a false positive.
  * reserve_price / emd: single notice -> one value; MULTI notice -> the gold
    `lots` enumerate every lot in the markdown, so the union of gold reserves is
    closed-world and any extracted reserve outside it is a false positive.
  * EXPECT_NULL fields: the correct answer is "empty", so ANY emitted value is a
    false positive ("don't invent").

Identifiers are left RECALL-ONLY on purpose: a notice legitimately carries survey/
patta/flat ids the sparse gold never listed, so counting id false positives against
a partial gold would be dishonest. Precision here is scoped to where it is sound,
and that scope is stated in the report.

Matching reuses evals.langextract_eval._smatch so recall stays comparable with the
legacy number; an extracted value is a false positive when it does NOT _smatch the
gold value. "Chennai North" vs gold "Chennai South" therefore counts as an FP
(neither is a substring of the other), while the lenient substring match that lets
"Chennai" pass for "Chennai South" is unchanged — we are adding a signal, not
silently re-baselining recall.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field

from evals.langextract_gold import EXPECT_NULL
from evals.langextract_eval import _SC_KEYS, _LOC_KEYS, _smatch

# Scalar fields whose gold value is single-valued on a single-lot notice.
SCALAR_FIELDS = tuple(_SC_KEYS) + tuple(_LOC_KEYS) + ("possession_type",)


def _fnum(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def collect_scalars(records: list[dict]) -> dict[str, set]:
    """All distinct raw values emitted per scalar field (not collapsed)."""
    vals: dict[str, set] = {f: set() for f in SCALAR_FIELDS}
    for r in records:
        a = r.get("attrs") or {}
        c = r.get("cls")
        if c == "secured_creditor":
            for k in _SC_KEYS:
                if a.get(k):
                    vals[k].add(str(a[k]))
        elif c == "location":
            for k in _LOC_KEYS:
                if a.get(k):
                    vals[k].add(str(a[k]))
        elif c == "property" and a.get("possession_type"):
            vals["possession_type"].add(str(a["possession_type"]))
    return vals


def collect_money(records: list[dict]) -> tuple[set, set]:
    """Distinct reserve prices and EMDs across all auction_terms entities."""
    res, emd = set(), set()
    for r in records:
        if r.get("cls") != "auction_terms":
            continue
        a = r.get("attrs") or {}
        rp, em = _fnum(a.get("reserve_price_num")), _fnum(a.get("emd_num"))
        if rp is not None:
            res.add(rp)
        if em is not None:
            emd.add(em)
    return res, emd


@dataclass
class PRF:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    over_emitted: list = field(default_factory=list)   # (aid, field, gold, extras)
    invented: list = field(default_factory=list)       # (aid, field, extras) EXPECT_NULL
    slotting: list = field(default_factory=list)       # (aid, field, gold, found_under)

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 1.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def _score_scalar(prf: PRF, aid: str, field_name: str, gold, extracted: set,
                  all_scalars: dict[str, set]) -> None:
    """One closed-world scalar field. Updates prf in place."""
    if gold is EXPECT_NULL:
        if extracted:                       # anything emitted is an invention
            prf.fp += len(extracted)
            prf.invented.append((aid, field_name, sorted(extracted)))
        return
    matched = any(_smatch(gold, x) for x in extracted)
    extras = [x for x in extracted if not _smatch(gold, x)]
    if matched:
        prf.tp += 1
    else:
        prf.fn += 1
        # slotting: the gold value shows up under a DIFFERENT scalar field
        for other, vals in all_scalars.items():
            if other != field_name and any(_smatch(gold, x) for x in vals):
                prf.slotting.append((aid, field_name, gold, other))
                break
    if extras:                              # spurious extra values for this field
        prf.fp += len(extras)
        prf.over_emitted.append((aid, field_name, gold, extras))


def _score_money(prf: PRF, aid: str, label: str, gold_set: set,
                 extracted: set) -> None:
    """Closed-world money field(s). gold_set may hold 1 (single) or N (multi)."""
    gold_set = {g for g in gold_set if g is not None}
    for g in gold_set:
        if g in extracted:
            prf.tp += 1
        else:
            prf.fn += 1
    extras = [x for x in extracted if x not in gold_set]
    if extras:
        prf.fp += len(extras)
        prf.over_emitted.append((aid, label, sorted(gold_set), extras))


def score_prf(gold_entries: list[dict], records_by_aid: dict[str, list[dict]]) -> PRF:
    """Aggregate precision/recall/F1 over the closed-world fields of every notice."""
    prf = PRF()
    for g in gold_entries:
        aid = g["aid"]
        records = records_by_aid.get(aid) or []
        scalars = collect_scalars(records)
        res_x, emd_x = collect_money(records)

        # notice-level scalars (legal_basis, bank_name, geo, possession, ...)
        for fname, gval in g["fields"].items():
            if fname in ("reserve_price_num", "emd_num", "borrower_primary"):
                continue
            if fname not in SCALAR_FIELDS:
                continue
            if gval is None:
                continue
            _score_scalar(prf, aid, fname, gval, scalars.get(fname, set()), scalars)

        # money — single notice uses fields; multi uses the lots union
        if g.get("lots"):
            gold_res = {_fnum(l.get("reserve_price_num")) for l in g["lots"]}
            gold_emd = {_fnum(l.get("emd_num")) for l in g["lots"]}
        else:
            gold_res = {_fnum(g["fields"].get("reserve_price_num"))}
            gold_emd = {_fnum(g["fields"].get("emd_num"))}
        _score_money(prf, aid, "reserve_price_num", gold_res, res_x)
        _score_money(prf, aid, "emd_num", gold_emd, emd_x)
    return prf


def report(prf: PRF) -> str:
    lines = [
        "PRECISION-AWARE SCORE (closed-world fields: single-valued scalars + money)",
        f"  precision : {prf.precision*100:5.1f}%   (tp={prf.tp} fp={prf.fp})",
        f"  recall    : {prf.recall*100:5.1f}%   (tp={prf.tp} fn={prf.fn})",
        f"  F1        : {prf.f1*100:5.1f}%",
        f"  over-emitted values : {sum(len(e[-1]) for e in prf.over_emitted)}"
        f"  (across {len(prf.over_emitted)} fields)",
        f"  invented (EXPECT_NULL): {len(prf.invented)}",
        f"  slotting errors       : {len(prf.slotting)}",
    ]
    if prf.over_emitted:
        lines.append("\n  OVER-EMISSION (field  gold -> spurious extras):")
        for aid, fname, gold, extras in prf.over_emitted:
            lines.append(f"    {aid}  {fname:22} {gold!r} -> extra {extras!r}")
    if prf.slotting:
        lines.append("\n  SLOTTING (gold value landed under the wrong field):")
        for aid, fname, gold, other in prf.slotting:
            lines.append(f"    {aid}  {gold!r} expected under {fname!r}, found under {other!r}")
    if prf.invented:
        lines.append("\n  INVENTED (field expected empty, model emitted):")
        for aid, fname, extras in prf.invented:
            lines.append(f"    {aid}  {fname:22} -> {extras!r}")
    return "\n".join(lines)
