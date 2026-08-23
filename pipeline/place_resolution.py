"""
pipeline/place_resolution.py
----------------------------
Match the place a notice names to the official revenue record.

Bank resolution had no answer key, so it voted on the most common spelling.
Places are different: Tamil Nadu's revenue hierarchy is already in the graph —
``State -> District(38) -> Taluk(316) -> RevenueVillage(17,164)``, with
government codes. So resolving a place is not clustering; it is matching a
messy string to a known record.

Three things shape the design.

**Read bottom-up, not top-down.** The obvious order is district, then taluk,
then village. The corpus says the reverse works better: taluk names are
globally unique (0 duplicates across all 316), so a taluk *names its own
district*, and a damaged district string can be repaired by the taluk beneath
it. This is what resolves the 2019 reorganisation — ``Pallavaram`` moved from
Kancheepuram to Chengalpattu, and the taluk knows which side it is on::

    district='Kanchipuram'   taluk='Pallavaram'  -> Chengalpattu
    district='Tiuchirapalli' taluk='Thuraiyur'   -> Thiruchirappalli

**Village names are not unique.** 1,150 names belong to more than one village —
``Nallur`` exists 22 times, ``Agaram`` 21. Only 30% of village mentions are
globally unambiguous, so a village is looked up strictly inside its parent
taluk. Without the parent the answer is a coin flip.

**Aliases before similarity, never the reverse.** Historic names share almost
no letters with the official one, so edit distance actively misleads: asked
which district ``Trichy`` is, similarity answers *Kallakurichi* (47) rather
than Thiruchirappalli. Aliases are looked up first, and fuzzy matching runs
only afterwards behind :data:`FUZZY_MIN` and the guards below.

Auto-accept means an exact normalized hit or a guarded fuzzy hit. Everything
else is returned unresolved with its reason, for a human — a wrong place is
worse than a missing one, because a missing one is visible.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

# Words that describe the *kind* of place rather than naming it. A notice
# writes "Sriperumbudur Taluk" and the gazetteer says "Sriperumbudur".
_QUALIFIER = re.compile(
    r"\b(taluk|taluks|taluq|tk|district|dist|districts|village|vill|"
    r"revenue|reg|registration|sub|panchayat|union|firka|circle)\b")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Fuzzy floor. Below this the suggestions are noise; above it they still have
# to clear every guard in _fuzzy_match. Calibrated on the corpus: genuine
# spelling damage (Mockanur/Mookkanur, Nazarathpet/Nazarathpettai) lands at
# 90+, while wrong pairs of real neighbouring villages (Murukampattu vs
# Erukkampattu) also reach 86 — which is why a bare threshold is not enough.
FUZZY_MIN = 90.0
# The winner must beat the runner-up by this much. Two villages in one taluk
# scoring alike means the taluk has near-twins and neither can be trusted.
FUZZY_MARGIN = 4.0

# Historic, colloquial and administrative names. Similarity cannot find these
# — the letters differ too much — so they are stated outright.
DISTRICT_ALIASES = {
    "trichy": "Thiruchirappalli",
    "tiruchi": "Thiruchirappalli",
    "tiruchirapalli": "Thiruchirappalli",
    "tanjore": "Thanjavur",
    "tuticorin": "Thoothukudi",
    "ootacamund": "Nilgiris",
    "ooty": "Nilgiris",
    "the nilgiris": "Nilgiris",
    "nilgiri": "Nilgiris",
    "karaikudi": "Sivagangai",
    "sivaganga": "Sivagangai",
    "kanchipuram": "Kancheepuram",
    "conjeevaram": "Kancheepuram",
    "chengalpet": "Chengalpattu",
    "chingleput": "Chengalpattu",
    "madras": "Chennai",
    "tirunelveli kattabomman": "Tirunelveli",
    "virudunagar": "Virudhunagar",
    "toothukudi": "Thoothukudi",
}

# Chennai is fully urban and keeps no revenue villages, so 12 of its taluks
# hold zero in the gazetteer. A village that cannot be found there is a gap in
# the reference data, not a bad read of the notice, and must be reported as
# such rather than counted as a failure.
VILLAGE_NOT_APPLICABLE = "taluk-has-no-villages"


def normalize_place(value: str) -> str:
    """Fold a place name to a comparable key.

    Handles the spelling axes that separate the same place across sources:
    the leading ``Th``/``T`` (Thiruvallur/Tiruvallur), doubled consonants
    (Pudukkottai/Pudukottai), and ``ee``/``i`` (Kancheepuram/Kanchipuram).
    Digits survive — ``Kengari-2`` and ``Kengarai 1`` are different villages.
    """
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", str(value)).lower().strip()
    s = _QUALIFIER.sub(" ", s)
    s = _NON_ALNUM.sub(" ", s)
    # "Jeyamangalam Bit I" and "Jeyamangalam Bit II" are two villages in one
    # taluk. Left as letters they would meet at the doubled-letter collapse,
    # so they become digits, where the digit guard already keeps them apart.
    s = re.sub(r"\biii\b", "3", s)
    s = re.sub(r"\bii\b", "2", s)
    s = re.sub(r"\bi\b", "1", s)
    s = re.sub(r"^th", "t", s.strip())
    s = s.replace("th", "t").replace("ph", "f")
    # Tamil writes one letter where English transliteration alternates: க is
    # both k and g (Mannarkudi/Mannargudi), and a final ய reaches paper as
    # either y or i (Edappadi/Edappady). Folding them is what makes the two
    # spellings of one taluk meet.
    # ...but only inside a word. A lone "G." or "K." is an initial that
    # distinguishes two villages ("G.Pappankulam" and "K.Pappankulam" both sit
    # in Madurai East), so single-letter tokens keep their spelling.
    s = re.sub(r"(?<=[a-z0-9])g|g(?=[a-z0-9])", "k", s)
    s = re.sub(r"(?<=[a-z0-9])y\b", "i", s)
    # "ee" folds before the doubled-letter collapse, or "kancheepuram" becomes
    # "kanchepuram" and never meets "kanchipuram". "oo" is deliberately left
    # alone: collapsing it separates "Mookkanur" from "Mockanur", which the
    # doubled-letter rule already brings together.
    s = s.replace("ee", "i")
    s = re.sub(r"(.)\1+", r"\1", s)          # collapse doubled letters
    return re.sub(r"\s+", "", s)


def _digits(value: str) -> str:
    return "".join(sorted(re.findall(r"\d", value or "")))


def _fuzzy_match(needle: str, pool: dict[str, str]) -> tuple[str, float] | None:
    """Best guarded fuzzy match of ``needle`` among ``{key: display}``.

    Four guards, each earning its place on real corpus failures:

    * ``FUZZY_MIN`` — a weak best guess is worse than none.
    * same first letter — ``Murukampattu`` scores 86 against the unrelated
      ``Erukkampattu``; a shared opening keeps neighbouring villages apart.
    * ``FUZZY_MARGIN`` over the runner-up — a taluk containing near-twins
      cannot pick between them, so it should not try.
    * identical digits — ``Kengari-2`` matched ``Kengarai 1`` at 93; numbered
      sub-villages are distinct places.
    """
    if not needle or not pool:
        return None
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return None
    scored = sorted(((fuzz.ratio(needle, key), key) for key in pool),
                    reverse=True)
    top_score, top_key = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if top_score < FUZZY_MIN:
        return None
    if needle[:1] != top_key[:1]:
        return None
    if top_score - runner_up < FUZZY_MARGIN:
        return None
    if _digits(needle) != _digits(top_key):
        return None
    return pool[top_key], float(top_score)


@dataclass
class Gazetteer:
    """The official hierarchy, indexed for lookup.

    Built from plain rows so it can be exercised without a database:
    ``districts`` as names, ``taluks`` as ``(taluk, district)`` and
    ``villages`` as ``(village, taluk, district)``.
    """
    districts: list[str] = field(default_factory=list)
    taluks: list[tuple[str, str]] = field(default_factory=list)
    villages: list[tuple[str, str, str]] = field(default_factory=list)

    def __post_init__(self):
        self._d: dict[str, str] = {}
        for name in self.districts:
            self._d.setdefault(normalize_place(name), name)
        # Taluk names are globally unique in the corpus, which is what lets a
        # taluk name its district. Should a duplicate ever appear, the first
        # wins and the ambiguity surfaces as a wrong district rather than
        # silently — so guard it here instead of assuming.
        self._t: dict[str, tuple[str, str]] = {}
        self._t_dupes: set[str] = set()
        for taluk, district in self.taluks:
            key = normalize_place(taluk)
            if key in self._t:
                self._t_dupes.add(key)
            else:
                self._t[key] = (taluk, district)
        # Villages are indexed per taluk; a bare village name is never enough.
        self._v_by_taluk: dict[str, dict[str, str]] = defaultdict(dict)
        # ... and per district, as a second chance. Taluk boundaries are
        # redrawn more often than district ones, so a village can be real and
        # simply sit under a neighbouring taluk from the notice's.
        self._v_by_district: dict[str, dict[str, set]] = defaultdict(
            lambda: defaultdict(set))
        # Taluk names per district, to recognise a "village" that is really a
        # taluk — "Kundrathur" and "Madhavaram" were villages before they were
        # promoted, and notices still write them in the village field.
        self._t_by_district: dict[str, dict[str, str]] = defaultdict(dict)
        # A taluk can hold several distinct villages under one name — Tiruvallur
        # has three called Karanai, each with its own village code. The name
        # alone cannot say which, so it is refused rather than guessed.
        self._v_ambiguous: set[tuple[str, str]] = set()
        for village, taluk, district in self.villages:
            key = normalize_place(village)
            if self._v_by_taluk[taluk].get(key) == village:
                self._v_ambiguous.add((taluk, key))
            self._v_by_taluk[taluk].setdefault(key, village)
            self._v_by_district[district][key].add((village, taluk))
        for taluk, district in self.taluks:
            self._t_by_district[district].setdefault(normalize_place(taluk), taluk)

    def district(self, value: str) -> str | None:
        """Official district for a raw string: alias, then exact, then fuzzy."""
        if not (value or "").strip():
            return None
        alias = DISTRICT_ALIASES.get(re.sub(r"\s+", " ", value.lower().strip()))
        if alias:
            return alias
        key = normalize_place(value)
        if key in self._d:
            return self._d[key]
        # Aliases are also matched on the folded key, so "Tiruchi District"
        # and "trichy" reach the same entry.
        for raw_alias, official in DISTRICT_ALIASES.items():
            if normalize_place(raw_alias) == key:
                return official
        hit = _fuzzy_match(key, self._d)
        return hit[0] if hit else None

    def taluk(self, value: str) -> tuple[str, str] | None:
        """``(taluk, district)`` for a raw string — the district comes free."""
        if not (value or "").strip():
            return None
        key = normalize_place(value)
        if key in self._t and key not in self._t_dupes:
            return self._t[key]
        pool = {k: k for k in self._t if k not in self._t_dupes}
        hit = _fuzzy_match(key, pool)
        return self._t[hit[0]] if hit else None

    def village(self, value: str, taluk: str, *,
                fuzzy: bool = True) -> str | None:
        """Official village name, looked up strictly inside ``taluk``.

        ``fuzzy=False`` restricts it to an exact folded hit, which lets the
        caller take the exact answer before trying looser rules.
        """
        pool = self._v_by_taluk.get(taluk) or {}
        if not (value or "").strip() or not pool:
            return None
        key = normalize_place(value)
        if (taluk, key) in self._v_ambiguous:
            return None
        if key in pool:
            return pool[key]
        if not fuzzy:
            return None
        hit = _fuzzy_match(key, pool)
        return hit[0] if hit else None

    def taluk_has_villages(self, taluk: str) -> bool:
        return bool(self._v_by_taluk.get(taluk))

    def village_in_district(self, value: str,
                            district: str) -> tuple[str, str] | None:
        """``(village, taluk)`` searched across a whole district.

        Exact match only, and only when the district holds exactly one village
        of that name — widening the search widens the chance of collision, so
        no fuzzy matching is allowed at this scope.
        """
        pool = self._v_by_district.get(district) or {}
        key = normalize_place(value)
        found = pool.get(key)
        if found and len(found) == 1:
            village, taluk = next(iter(found))
            # The set collapses same-name-same-taluk entries, so the
            # within-taluk ambiguity has to be re-checked here.
            if (taluk, key) not in self._v_ambiguous:
                return village, taluk
        return None

    def names_a_taluk(self, value: str, district: str) -> str | None:
        """The taluk this string names, if it names one rather than a village."""
        return (self._t_by_district.get(district) or {}).get(
            normalize_place(value))


def resolve_place(gaz: Gazetteer, *, district: str | None = None,
                  taluk: str | None = None,
                  village: str | None = None) -> dict:
    """Resolve one notice's place fields against the gazetteer.

    Bottom-up: the taluk is tried first because it carries its district, so a
    misspelt or outdated district string is corrected rather than believed.
    The village is then looked up only within that taluk.

    Returns the resolved names, what each was derived from, and — when the
    village cannot be placed — why, so the review queue can tell a bad read
    from a gap in the reference data.
    """
    out = {
        "district": None, "taluk": None, "village": None,
        "district_source": None, "village_source": None, "village_status": None,
        "raw": {"district": district, "taluk": taluk, "village": village},
        "conflict": False,
    }
    t = gaz.taluk(taluk) if taluk else None
    d_direct = gaz.district(district) if district else None

    if t:
        out["taluk"], out["district"] = t
        out["district_source"] = "taluk"
        # A district that disagrees with its own taluk is worth surfacing: it
        # is usually the 2019 reorganisation, occasionally a misread.
        out["conflict"] = bool(d_direct and d_direct != t[1])
    elif d_direct:
        out["district"] = d_direct
        out["district_source"] = "district"
    elif taluk:
        # The taluk field sometimes holds a district: "Coimbatore" is a
        # district whose taluks are Coimbatore North and South, and notices
        # write the district name into both fields. Better a right district
        # than nothing.
        from_taluk_field = gaz.district(taluk)
        if from_taluk_field:
            out["district"] = from_taluk_field
            out["district_source"] = "taluk-field-names-a-district"

    if not village:
        out["village_status"] = "absent"
        return out
    if not out["taluk"]:
        out["village_status"] = "no-parent-taluk"
        return out

    found = gaz.village(village, out["taluk"], fuzzy=False)
    if found:
        out["village"] = found
        out["village_status"] = "resolved"
        out["village_source"] = "taluk"
        return out

    # A village field repeating its own taluk's name is the taluk again, not a
    # village. Checked before fuzzy, which would otherwise reach for the
    # nearest suffixed variant — "Kundrathur" became "Kundrathur B" at 95.
    if normalize_place(village) == normalize_place(out["taluk"]):
        out["village_status"] = "names-a-taluk"
        return out

    found = gaz.village(village, out["taluk"])
    if found:
        out["village"] = found
        out["village_status"] = "resolved"
        out["village_source"] = "taluk"
        return out

    # Second chance across the district. A taluk boundary moved, or the notice
    # named the neighbouring taluk; the village itself is still real.
    wider = gaz.village_in_district(village, out["district"])
    if wider:
        out["village"], out["taluk"] = wider
        out["village_status"] = "resolved"
        out["village_source"] = "district"
        return out

    # Not a village at all — the field holds the name of a taluk.
    if gaz.names_a_taluk(village, out["district"]):
        out["village_status"] = "names-a-taluk"
        return out

    # Nothing found, so say *why*: a taluk holding no villages at all is a gap
    # in the reference data, and blaming the notice for it would be wrong.
    out["village_status"] = ("unmatched" if gaz.taluk_has_villages(out["taluk"])
                             else VILLAGE_NOT_APPLICABLE)
    return out
