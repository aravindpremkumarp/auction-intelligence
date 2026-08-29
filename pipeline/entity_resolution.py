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

Three tiers, and the split is the whole design:

**Auto-merge — normalized key equality.** Lowercase, unescape HTML (``&amp;``
leaks in from table cells), drop punctuation, fold ``&`` to ``and``, strip legal
suffixes (Ltd / Limited / Pvt / Company …), then compare the *set* of remaining
tokens. Only exact set equality merges. On the live corpus this absorbs 66
spellings into 133 groups and, verified by hand, merges nothing that should stay
apart.

**Auto-merge — one misread token** (:func:`ocr_variant_of`). Token-set equality
cannot reach ``Pirama Finance`` / ``Piramal Finance``, ``IICI Bank`` /
``ICICI Bank``, ``Kanur Vysya`` / ``Karur Vysya`` or ``Manapouram`` /
``Manappuram``, because the damage changes the token itself. Dropping the
tokens two names share and demanding the single leftover pair be within a tiny
edit budget reaches all four, and still refuses the pairs below — a name that
gains or swaps a whole word is a different company. Measured on 192
hand-labelled names from the live corpus, this lifts recall from 0.72 to 0.84
with precision unchanged at 1.00.

**Review queue — fuzzy similarity.** Everything else is a proposal for a human,
never an automatic merge. The corpus shows why: at 92.9 similarity,
``Asset Reconstruction Company (India) Limited`` and ``India SME Asset
Reconstruction Company Limited`` are different companies. Auto-merging every
proposal at or above 88 scores 0.96 precision against the same labels — six
wrong merges, five of them that one pair — which is why the queue stays
advisory.

Substring containment is never used: ``Bank of India`` sits inside ``State Bank
of India`` and ``Indian Bank`` inside ``The South Indian Bank Ltd``, and those
are four separate banks. Nor is whole-name similarity with spaces removed: it
buys two more true pairs and 215 wrong ones, because ``AU Small Finance Bank``
then reaches every other small finance bank and connected components chains
them into a single lender.

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


def branch_key(value: str) -> str:
    """The auto-merge key for a branch name, always used *within one bank*.

    A branch name is word soup around a place or unit name — ``ARM Branch
    Trichy``, ``Trichy ARM Branch``, ``ARM TRICHY`` are one office of Canara
    Bank, spelt six ways across the corpus. Normalized, the filler word
    "branch" dropped, remaining tokens sorted as a set. Numerals survive as
    tokens, which is what keeps ``ARM I`` and ``ARM II`` — two different
    Chennai offices — apart.

    Never compare these keys across banks: "Chennai" names a different branch
    in every bank that has one, which is also why the scraped graph's shared
    ``(:Branch {name})`` nodes (one "Chennai" node claimed by 23 banks)
    cannot be trusted as identities.
    """
    s = _LEADING_THE.sub("", normalize(value))
    s = re.sub(r"\b(branch|br)\b", " ", s)
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


#: Absolute edit budget for one damaged token. Two edits is what turns
#: "Cholamandalam" into "Cholamandam" or "Manappuram" into "Manapouram"; three
#: starts reaching genuinely different words.
OCR_MAX_EDIT = 2
#: Relative edit budget for the same token, and the half that carries the
#: weight. Two edits inside a fourteen-letter name is noise; two edits inside
#: "CSB" produces "DCB", a different bank. This corpus is full of three- and
#: four-letter lender acronyms (CSB / DCB / UCO, ICICI / IDBI), and an absolute
#: budget alone merges them.
OCR_MAX_RELATIVE = 0.30


def _identity_tokens(value: str) -> set[str]:
    """The tokens that carry identity: normalized, legal form and "the" gone.

    Same treatment :func:`org_key` applies, returned as a set instead of a
    joined string so callers can ask which tokens two names do NOT share.
    """
    s = _LEGAL_SUFFIX.sub(" ", _LEADING_THE.sub("", normalize(value)))
    return {t for t in s.split() if t}


def ocr_variant_of(a: str, b: str) -> bool:
    """True when two names differ only by damage INSIDE a shared token.

    This is the distinction the corpus actually turns on. OCR breaks characters
    within a word — ``Karur`` read as ``Kanur``, ``ICICI`` as ``IICI``,
    ``Piramal`` as ``Pirama`` — while a genuinely different company adds or
    swaps a whole word: ``Asset Reconstruction Company (India)`` against
    ``India SME Asset Reconstruction Company``, ``Bajaj Finance`` against
    ``Bajaj Housing Finance``, ``Axis Bank`` against ``Axis Finance``.

    So drop every token the two names share and require what is left to pair up
    one-for-one within the edit budget. An unmatched extra token means a
    different lender however alike the two strings look end to end, which is
    what keeps this rule away from the 92.9-similarity trap that makes
    :func:`propose_merges` advisory.

    Measured against 192 hand-labelled names from the live corpus: recall rises
    from 0.72 (token-set equality alone) to 0.84 with precision still at 1.00.
    ``rapidfuzz`` is a pipeline dependency; without it this returns False, so
    :func:`resolve` degrades to plain token-set equality rather than failing.
    """
    try:
        from rapidfuzz.distance import DamerauLevenshtein
    except ImportError:
        return False
    ta, tb = _identity_tokens(a), _identity_tokens(b)
    only_a, only_b = sorted(ta - tb), sorted(tb - ta)
    # Exactly one unmatched token a side. Zero means org_key already merged
    # them; two or more is a different name, not a misread one.
    if len(only_a) != 1 or len(only_b) != 1:
        return False
    x, y = only_a[0], only_b[0]
    edit = DamerauLevenshtein.distance(x, y)
    return (edit <= OCR_MAX_EDIT
            and edit / max(len(x), len(y)) <= OCR_MAX_RELATIVE)


def _merge_ocr_variants(buckets: dict[str, dict[str, int]]) -> dict[str, str]:
    """Map each bucket key to the key of the bucket it belongs with.

    Compares one representative per bucket (the most frequent spelling), so the
    cost is quadratic in *distinct lenders* rather than in notices. At the
    corpus's ~130 lenders that is a few thousand comparisons; if the lender
    count ever reaches the thousands this needs a blocking step, because the
    damaged-character case defeats every prefix or token block.
    """
    reps = {k: canonical_label(v) for k, v in buckets.items()}
    parent = {k: k for k in buckets}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    keys = sorted(buckets)
    for i, ka in enumerate(keys):
        for kb in keys[i + 1:]:
            if ocr_variant_of(reps[ka], reps[kb]):
                ra, rb = find(ka), find(kb)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    return {k: find(k) for k in keys}


def resolve(values: Counter | dict[str, int], *, kind: str = "org") -> dict:
    """Group name strings that mean the same thing.

    ``values`` maps each raw string to how often it appears. Returns
    ``{"groups": [...], "by_value": {raw: canonical}}`` where each group carries
    its canonical label, its variants and a total count. Auto-merge only — the
    fuzzy proposals live in :func:`propose_merges`, which a human reviews.

    Two auto-merge tiers, both exact enough to run unattended. Token-set
    equality folds case, punctuation and legal form. :func:`ocr_variant_of`
    then folds misread characters inside a shared token, which token-set
    equality cannot see because the damage changes the token. Branch names get
    the first tier only: ``ARM I`` and ``ARM II`` are two Chennai offices one
    edit apart, so the second tier would merge different branches.
    """
    key_fns = {"org": org_key, "branch": branch_key}
    if kind not in key_fns:
        raise ValueError(f"unsupported kind: {kind!r} "
                         f"(one of {sorted(key_fns)})")
    key_fn = key_fns[kind]
    buckets: dict[str, dict[str, int]] = defaultdict(dict)
    for raw, count in values.items():
        if not (raw or "").strip():
            continue
        buckets[key_fn(raw)][raw] = int(count)

    if kind == "org":
        target = _merge_ocr_variants(buckets)
        if any(k != t for k, t in target.items()):
            folded: dict[str, dict[str, int]] = defaultdict(dict)
            for key, variants in buckets.items():
                folded[target[key]].update(variants)
            buckets = folded

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
