"""Spot-check sampling and statistics — the honest measuring stick.

WHY THIS EXISTS (and why it is NOT the review queue)
-----------------------------------------------------
``pipeline/validators.py`` scores a notice without ground truth: it counts
*self-evident* defects (a missing borrower, an ungrounded span, an EMD that
isn't ~10% of the reserve). That is a useful triage signal, but it can never
say how ACCURATE the pipeline is, because every check asks "is this field
present / self-consistent?" and none asks "is this value actually right?".
A notice full of confidently wrong values scores 100.

The only way to know the real accuracy is for a human to check a sample
against the source. This module defines that sample. Two rules make it a
measurement rather than a chore:

  1. RANDOM, from a stated population. A queue sorted by "most suspicious"
     tells you about suspicious documents, not about the corpus. Estimating
     corpus accuracy from a triage queue is the classic selection-bias
     mistake, so the draw here is seeded-random over an explicit scope and the
     scope travels with the result (see ``describe_scope``).
  2. SEPARATE STORAGE. Spot-check verdicts never touch
     ``Document.extraction_review_status`` / ``extraction_corrections_json``.
     The fix-queue answers "make this document right"; the audit answers "how
     right is the pipeline". Letting audit verdicts flow into the corrections
     bag would both bias the next audit and quietly promote audited values
     into the eval gold set (``evals/export_review_gold.py``), which is how a
     measuring stick turns into a mirror.

THE UNIT IS AN ATOMIC CLAIM, NOT A DOCUMENT
-------------------------------------------
Asking "is this notice correct?" means reading ~130 entities, which is how a
review session turns into a bulk-verify click. So an extraction is flattened
into atomic claims (``iter_claims``) — one crisp yes/no each:

    span claim : "this highlighted text is a `borrower`"
    attr claim : "`village` = 'Sathyamangala' for this `location`"

A claim takes a few seconds to judge with the source on screen, so a
100-claim sample is minutes of work, not an afternoon.

WHAT THIS MEASURES, AND WHAT IT CANNOT
--------------------------------------
Sampling from what the model EMITTED measures precision: of the values
produced, how many are right. It is structurally blind to recall — a field the
model never emitted cannot be sampled, so a pipeline that silently drops half
the lots can post a perfect precision. ``build_report`` states this in its
output rather than leaving the reader to infer a completeness claim that was
never made. Measuring recall needs a different probe (read the source, list
what SHOULD be there); the ``missed`` verdict below is a cheap partial signal
— a reviewer who happens to notice an omission can record it — but a low
``missed`` count is evidence of nothing.

SMALL SAMPLES LIE, SO INTERVALS ARE MANDATORY
---------------------------------------------
18/20 correct is not "90% accurate"; it is "somewhere between 70% and 97%,
95% of the time" (Wilson). Reporting the point estimate alone is what makes a
number feel authoritative and vague at once — precisely the complaint this
module answers. ``wilson_interval`` is therefore applied to every rate, and
``build_report`` refuses to state a rate at all below ``MIN_N``.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# ── verdict vocabulary ───────────────────────────────────────────────────────
# Deliberately tiny: a reviewer holds four options in their head, not ten, and
# every extra option is a hesitation on every item.
#
# The split between WRONG and NOT_IN_SOURCE is the one distinction worth the
# extra key. Both are errors, but they have different causes and different
# fixes: a wrong value means the model read the document and mis-assigned
# (prompt/schema work), while a value with no basis in the source means it
# invented one (a grounding failure — usually a missing-data case the prompt
# never gave it permission to leave empty). Collapsing them hides which of the
# two you have.
CORRECT = "correct"            # value is right
WRONG = "wrong"                # value is wrong, but something was there to read
NOT_IN_SOURCE = "not_in_source"  # invented — no basis in the document
UNCLEAR = "unclear"            # the SOURCE is ambiguous/illegible, not the model
MISSED = "missed"              # reviewer spotted an omission (recall hint only)

VERDICTS = (CORRECT, WRONG, NOT_IN_SOURCE, UNCLEAR, MISSED)

# Verdicts that count toward the precision denominator. UNCLEAR is excluded on
# purpose: when the OCR is mush or the notice itself hedges, the extraction
# cannot be graded, and folding those into the denominator would charge the
# model for the scanner's failures. They are reported separately as a source-
# quality rate, which is its own useful number. MISSED is excluded because it
# describes something the sample never contained (see the recall note above).
_GRADED = (CORRECT, WRONG, NOT_IN_SOURCE)

# Below this many graded verdicts a rate is not reported at all. At n=3 the
# 95% interval spans almost the whole unit line, so a printed percentage would
# carry far more authority than information.
MIN_N = 8

# Attributes that are bookkeeping rather than an extracted claim — sampling
# them wastes reviewer time on things no human can get wrong.
_NON_CLAIM_ATTRS = frozenset({"lot_index"})


# ── atomic claims ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Claim:
    """One reviewable yes/no assertion drawn from a stored extraction.

    ``key`` is stable across re-draws and re-runs so a verdict can be matched
    back to its claim, and so the same claim is never sampled twice.
    """
    filename: str
    field_id: str
    cls: str
    attr: str | None      # None -> the span/class assertion itself
    value: str
    # The entity's own source text. For a span claim this equals ``value``; for
    # an attr claim it is the passage the attribute was read FROM, which is
    # what the UI highlights and what re-anchoring searches for when the
    # markdown has moved (an attribute value like "constructive" is often a
    # normalised label that appears nowhere in the document verbatim).
    span_text: str
    start: int | None
    end: int | None
    lot_index: str | None

    @property
    def key(self) -> str:
        return f"{self.filename}#{self.field_id}#{self.attr or '@span'}"

    @property
    def stratum(self) -> str:
        """The bucket this claim is reported under.

        Prefixed because an attribute and an entity class can share a name
        (``borrower`` is both), and silently merging the two would average a
        span assertion with an attribute value into one meaningless rate.
        """
        return f"attr:{self.attr}" if self.attr else f"cls:{self.cls}"

    @property
    def question(self) -> str:
        """The exact thing the reviewer is being asked to judge."""
        if self.attr:
            return f"{self.attr} = {self.value!r}"
        return f"this text is a {self.cls}"


def iter_claims(filename: str, entities: list[dict]) -> list[Claim]:
    """Flatten one document's stored entities into atomic claims.

    ``entities`` is the ``Document.extraction_json`` shape used everywhere else
    in the pipeline: ``[{id, cls, text, start, end, attrs}]``.

    Each entity yields one span claim (is this text really a `cls`?) plus one
    attr claim per non-empty attribute. Empty/null-ish attributes are skipped:
    "the model left this blank" is a recall question, and this sample is about
    what it emitted.
    """
    out: list[Claim] = []
    for i, e in enumerate(entities or []):
        if not isinstance(e, dict):
            continue
        fid = str(e.get("id") or i)
        cls = str(e.get("cls") or "")
        attrs = e.get("attrs") if isinstance(e.get("attrs"), dict) else {}
        lot = attrs.get("lot_index")
        lot = str(lot) if lot not in (None, "") else None
        text = str(e.get("text") or "")
        start, end = e.get("start"), e.get("end")
        if text.strip():
            out.append(Claim(filename=filename, field_id=fid, cls=cls, attr=None,
                             value=text, span_text=text,
                             start=start, end=end, lot_index=lot))
        for k, v in sorted(attrs.items()):
            if k in _NON_CLAIM_ATTRS:
                continue
            sv = "" if v is None else str(v)
            # The same null-ish set validators.py treats as absent, so the two
            # modules agree on what counts as an emitted value.
            if not sv.strip() or sv.strip().lower() in {"null", "na", "n/a"}:
                continue
            out.append(Claim(filename=filename, field_id=fid, cls=cls, attr=str(k),
                             value=sv, span_text=text,
                             start=start, end=end, lot_index=lot))
    return out


# ── field focus ──────────────────────────────────────────────────────────────
# The priority fields from pipeline/validators.py — the ones that decide whether
# a lot is usable at all — plus `village`, which anchors a lot geographically and
# is the field most often mis-slotted between village/taluk/district.
#
# This list exists because an unfocused audit cannot answer a per-field
# question. An extraction carries ~60 distinct field types, so a 60-claim
# stratified draw lands n=1 on every one of them: enough for a single corpus-
# wide precision figure, and useless for "which field should I fix?". Focusing
# the same 60 answers on 8 fields buys ~8 observations each, which is where a
# Wilson interval starts to say something (see MIN_N).
PRIORITY_FIELDS = (
    "full_description", "property_type", "possession_type", "extent",
    "undivided_share", "borrower", "reserve_price_num", "village",
)


def matches_field(claim: Claim, wanted: set[str]) -> bool:
    """Is this claim about one of ``wanted``?

    A bare name matches either sense of the field, because a reviewer asking
    for "extent" means the concept, not a stratum key: `extent` selects both
    the entity-class assertion (`cls:extent`) and any `extent` attribute
    (`attr:extent`). An explicit ``cls:``/``attr:`` prefix narrows to one.
    """
    if not wanted:
        return True
    if claim.stratum in wanted:          # explicit "attr:village" / "cls:extent"
        return True
    name = claim.attr if claim.attr else claim.cls
    return name in wanted


def filter_claims(claims: list[Claim], fields) -> list[Claim]:
    """Keep only claims about ``fields`` (empty/None keeps everything)."""
    wanted = {f.strip() for f in (fields or []) if f and f.strip()}
    if not wanted:
        return list(claims)
    return [c for c in claims if matches_field(c, wanted)]


# ── sampling ─────────────────────────────────────────────────────────────────
def draw_sample(claims: list[Claim], *, size: int, seed: int,
                per_stratum_cap: int | None = None,
                max_per_document: int | None = None) -> list[Claim]:
    """A seeded, stratified random draw.

    Stratified by ``Claim.stratum`` so the rare-but-important fields (UDS,
    possession_type) get enough observations to say anything about, instead of
    being swamped by whichever class happens to be most numerous. A pure
    uniform draw over 130-entity documents would spend most of the sample on
    ``identifier`` and ``boundary`` and leave n=1 on the fields that decide
    whether a lot is usable.

    Determinism matters as much as randomness: an audit that cannot be redrawn
    cannot be re-checked, so the input is sorted by ``key`` before shuffling
    and the seed is stored with the sample. Database row order therefore has
    no effect on which claims are drawn.

    ``max_per_document`` stops one enormous multi-lot notice from dominating
    the sample — without it a 400-claim notice can supply most of a 100-claim
    draw, and the result describes that one document rather than the corpus.
    """
    if size <= 0 or not claims:
        return []
    rng = random.Random(seed)

    by_stratum: dict[str, list[Claim]] = {}
    for c in sorted(claims, key=lambda c: c.key):
        by_stratum.setdefault(c.stratum, []).append(c)
    for bucket in by_stratum.values():
        rng.shuffle(bucket)

    if per_stratum_cap is None:
        # Aim for an even spread across strata, but never below 1 — a stratum
        # with no observations cannot be reported on at all.
        per_stratum_cap = max(1, math.ceil(size / max(1, len(by_stratum))))

    per_doc: dict[str, int] = {}
    picked: list[Claim] = []
    # Round-robin across strata rather than filling one at a time, so hitting
    # `size` truncates the tail of every stratum evenly instead of starving
    # whichever strata sort last.
    order = sorted(by_stratum)
    rng.shuffle(order)
    for depth in range(per_stratum_cap):
        for stratum in order:
            bucket = by_stratum[stratum]
            if depth >= len(bucket):
                continue
            c = bucket[depth]
            if max_per_document is not None:
                if per_doc.get(c.filename, 0) >= max_per_document:
                    continue
                per_doc[c.filename] = per_doc.get(c.filename, 0) + 1
            picked.append(c)
            if len(picked) >= size:
                return picked
    return picked


def describe_scope(scope: dict) -> str:
    """One line stating what population a sample was drawn from.

    Carried into the report because a precision figure is meaningless without
    it: "94% accurate" over last week's single-lot notices is a different
    claim from "94% accurate" over the whole corpus, and six months later
    nobody remembers which one was run.
    """
    if not scope:
        return "all extracted documents"
    bits = []
    for label, key in (("batch", "batch"), ("notice_type", "notice_type"),
                       ("from", "date_from"), ("to", "date_to"),
                       ("score>=", "score_min"), ("score<=", "score_max")):
        v = scope.get(key)
        if v not in (None, ""):
            bits.append(f"{label}={v}")
    # A focused sample says nothing about the fields it excluded, so the field
    # list belongs in the one line that states what the number covers — without
    # it, "92% accurate" reads as a claim about the whole extraction.
    fields = [f for f in (scope.get("fields") or []) if f]
    if fields:
        bits.append("fields=" + "|".join(fields))
    return ", ".join(bits) if bits else "all extracted documents"


# ── statistics ───────────────────────────────────────────────────────────────
def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion, as (low, high) in 0..1.

    Wilson rather than the textbook normal approximation because the samples
    here are small and the rates are near 1, exactly where the normal
    approximation breaks: at 20/20 it returns the interval [1.0, 1.0],
    asserting certainty from twenty observations. Wilson gives [0.84, 1.0],
    which is the honest reading.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class Tally:
    counts: dict = field(default_factory=dict)

    def add(self, verdict: str) -> None:
        self.counts[verdict] = self.counts.get(verdict, 0) + 1

    @property
    def graded(self) -> int:
        return sum(self.counts.get(v, 0) for v in _GRADED)

    @property
    def correct(self) -> int:
        return self.counts.get(CORRECT, 0)

    @property
    def precision(self) -> float | None:
        n = self.graded
        return (self.correct / n) if n else None

    @property
    def interval(self) -> tuple[float, float] | None:
        n = self.graded
        return wilson_interval(self.correct, n) if n else None


def build_report(sample: dict, verdicts: dict) -> dict:
    """Turn a frozen sample plus its verdicts into an accuracy report.

    ``sample`` is the stored draw: ``{"id", "seed", "scope", "items": [...]}``
    where each item carries at least ``key`` and ``stratum``.
    ``verdicts`` maps claim key -> ``{"verdict": ..., ...}``.

    Unjudged items are counted as ``pending`` and excluded from every rate, so
    a half-finished audit reports on what was actually looked at instead of
    silently treating the remainder as correct.
    """
    items = sample.get("items") or []
    overall = Tally()
    by_stratum: dict[str, Tally] = {}
    by_document: dict[str, Tally] = {}
    pending = 0
    missed = 0

    for it in items:
        key = it.get("key")
        rec = verdicts.get(key) if key else None
        v = (rec or {}).get("verdict")
        if v not in VERDICTS:
            pending += 1
            continue
        if v == MISSED:
            missed += 1
            continue
        overall.add(v)
        by_stratum.setdefault(it.get("stratum") or "?", Tally()).add(v)
        by_document.setdefault(it.get("filename") or "?", Tally()).add(v)

    def _rate_block(t: Tally) -> dict:
        n = t.graded
        lo_hi = t.interval
        return {
            "n": n,
            "correct": t.correct,
            "wrong": t.counts.get(WRONG, 0),
            "not_in_source": t.counts.get(NOT_IN_SOURCE, 0),
            "unclear": t.counts.get(UNCLEAR, 0),
            # Below MIN_N the point estimate is withheld rather than shown with
            # a caveat — a number on screen gets quoted, a caveat does not.
            "precision": (t.precision if n >= MIN_N else None),
            "ci_low": (lo_hi[0] if (lo_hi and n >= MIN_N) else None),
            "ci_high": (lo_hi[1] if (lo_hi and n >= MIN_N) else None),
            "enough": n >= MIN_N,
        }

    unclear_total = overall.counts.get(UNCLEAR, 0)
    judged_total = overall.graded + unclear_total
    strata = {k: _rate_block(t) for k, t in sorted(by_stratum.items())}
    return {
        "sample_id": sample.get("id"),
        "seed": sample.get("seed"),
        "scope": describe_scope(sample.get("scope") or {}),
        "size": len(items),
        "judged": judged_total + missed,
        "pending": pending,
        "overall": _rate_block(overall),
        # Source quality, not model quality: how often the document itself was
        # too ambiguous or too badly OCR'd to grade. A high rate here means the
        # ingest pipeline is the thing to fix, not the prompt.
        "unclear_rate": (unclear_total / judged_total) if judged_total else None,
        "by_stratum": strata,
        "documents_sampled": len(by_document),
        "missed_reported": missed,
        # Stated, not implied. See the module docstring: this sample can only
        # speak about values that were emitted.
        "measures": "precision of emitted values",
        "does_not_measure": (
            "recall — fields the model never emitted cannot be sampled, so "
            "this says nothing about what was missed"
        ),
        "weakest": sorted(
            ((k, b) for k, b in strata.items() if b["enough"]),
            key=lambda kv: kv[1]["precision"],
        )[:5],
    }
