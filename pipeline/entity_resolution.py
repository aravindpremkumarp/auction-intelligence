"""
pipeline/entity_resolution.py
-----------------------------
Decide which extracted name strings mean the same real-world thing.

LangExtract records a name as the notice printed it, so one lender arrives as
``Canara Bank`` and ``CANARA BANK``, or as three different capitalisations of
``Cholamandalam Investment and Finance Company Limited``. Across the corpus 199
distinct ``bank_name`` strings stand for roughly 130 actual institutions. Until
they are resolved, "every Canara Bank auction" misses the ones typed in caps and
any count of lenders is inflated.

Two tiers, and the split is the whole design:

**Auto-merge — normalized key equality.** Lowercase, unescape HTML (``&amp;``
leaks in from table cells), drop punctuation, fold ``&`` to ``and``, strip legal
suffixes (Ltd / Limited / Pvt / Company …), then compare the *set* of remaining
tokens. Only exact set equality merges. On the live corpus this absorbs 66
spellings into 133 groups and, verified by hand, merges nothing that should stay
apart.

**Review queue — fuzzy similarity.** Everything else is a proposal for a human,
never an automatic merge. The corpus shows why both halves are needed:

  * real merges only fuzzy can find, all OCR damage —
    ``Pirama Finance`` / ``Piramal Finance``, ``IICI Bank`` / ``ICICI Bank``,
    ``Kanur Vysya`` / ``Karur Vysya``, ``Manapouram`` / ``Manappuram``;
  * a trap at 92.9 similarity — ``Asset Reconstruction Company (India) Limited``
    and ``India SME Asset Reconstruction Company Limited`` are different
    companies.

Substring containment is never used: ``Bank of India`` sits inside ``State Bank
of India`` and ``Indian Bank`` inside ``The South Indian Bank Ltd``, and those
are four separate banks.

Place names (district / taluk / village / area / city) are the next kind to
resolve. They need their own rules — a village is only the same village when its
taluk and district agree too — so this module keeps the organisation logic
behind ``kind="org"`` rather than pretending one rule fits both.
"""
from __future__ import annotations

import html
import re
import unicodedata
from collections import Counter, defaultdict

# Legal-form words that carry no identity: "Repco Home Finance Limited" and
# "Repco Home Finance Ltd" are one lender.
_LEGAL_SUFFIX = re.compile(
    r"\b(ltd|limited|pvt|private|public|co|company|corporation|corp|"
    r"incorporated|inc|llp|plc)\b")
# Leading article, likewise: "The Karur Vysya Bank" is "Karur Vysya Bank".
_LEADING_THE = re.compile(r"^the\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Fuzzy score at or above which a pair is worth a human's attention. Set from
# the live corpus: below 80 the suggestions are noise, and the genuine OCR
# typos all land at 94+ while the closest false pair sits at 92.9 — close
# enough that the queue must stay advisory rather than auto-merging near the top.
REVIEW_MIN_SCORE = 88.0


def normalize(value: str) -> str:
    """Casefold and strip a name to comparable text.

    Unescapes HTML first: table cells reach us carrying ``&amp;``, so
    "Cholamandalam Investment &amp; Finance" must become "... and finance"
    rather than "... amp finance".
    """
    if not value:
        return ""
    s = html.unescape(value)
    s = unicodedata.normalize("NFKD", s)
    s = s.replace("&", " and ")
    s = _NON_ALNUM.sub(" ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def org_key(value: str) -> str:
    """The auto-merge key for an organisation name.

    Normalized, legal suffixes and a leading "the" removed, remaining tokens
    de-duplicated and sorted — so word order and legal form stop mattering while
    every meaningful token still has to be present in both names. That last part
    is what keeps ``bank of india`` and ``state bank of india`` apart: their
    token sets differ by ``state``.
    """
    s = _LEADING_THE.sub("", normalize(value))
    s = _LEGAL_SUFFIX.sub(" ", s)
    return " ".join(sorted(set(s.split())))


def canonical_label(variants: dict[str, int]) -> str:
    """Pick the spelling to display for a group of {variant: count}.

    Most frequent wins. Ties break toward mixed case, because ALL CAPS is how a
    banner was typeset rather than how the institution writes its name, and then
    toward the longer string, which usually carries the fuller legal form.
    """
    if not variants:
        return ""
    def rank(item: tuple[str, int]):
        name, count = item
        return (count, not name.isupper(), len(name))
    return max(variants.items(), key=rank)[0]


def resolve(values: Counter | dict[str, int], *, kind: str = "org") -> dict:
    """Group name strings that mean the same thing.

    ``values`` maps each raw string to how often it appears. Returns
    ``{"groups": [...], "by_value": {raw: canonical}}`` where each group carries
    its canonical label, its variants and a total count. Auto-merge only — the
    fuzzy proposals live in :func:`propose_merges`, which a human reviews.
    """
    if kind != "org":
        raise ValueError(f"unsupported kind: {kind!r} (only 'org' so far)")
    buckets: dict[str, dict[str, int]] = defaultdict(dict)
    for raw, count in values.items():
        if not (raw or "").strip():
            continue
        buckets[org_key(raw)][raw] = int(count)

    groups = []
    by_value: dict[str, str] = {}
    for key, variants in buckets.items():
        label = canonical_label(variants)
        total = sum(variants.values())
        groups.append({
            "key": key,
            "canonical": label,
            "variants": sorted(variants.items(), key=lambda kv: -kv[1]),
            "count": total,
            "merged": len(variants) - 1,
        })
        for raw in variants:
            by_value[raw] = label
    groups.sort(key=lambda g: -g["count"])
    return {"groups": groups, "by_value": by_value}


def propose_merges(groups: list[dict], *,
                   min_score: float = REVIEW_MIN_SCORE,
                   limit: int = 200) -> list[dict]:
    """Pairs of resolved groups similar enough to be worth a human's look.

    Advisory by construction. These are the pairs the auto rule cannot decide:
    OCR damage inside a word ("Piramal" read as "Pirama") produces a different
    token set, so only similarity can suggest it — and similarity alone cannot
    be trusted, since two genuinely different asset-reconstruction companies
    score 92.9 against each other.

    Returns ``[{"score", "a", "b", "a_count", "b_count"}]``, highest first.
    ``rapidfuzz`` is a pipeline dependency; without it this returns nothing
    rather than failing, so auto-merge still works on its own.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return []
    out = []
    for i, g in enumerate(groups):
        for h in groups[i + 1:]:
            score = fuzz.token_sort_ratio(g["key"], h["key"])
            if score >= min_score:
                out.append({
                    "score": round(float(score), 1),
                    "a": g["canonical"], "b": h["canonical"],
                    "a_count": g["count"], "b_count": h["count"],
                })
    out.sort(key=lambda p: -p["score"])
    return out[:limit]
