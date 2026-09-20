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
    # The same name again, spelled as the notices spell it. Similarity cannot
    # reach "Chengalpattu" from these, and no other Tamil Nadu district is a
    # candidate for any of them — which is the bar for adding one here.
    # Deliberately NOT added: "Chengalpattu MGR", the composite district that
    # split into Kancheepuram and Tiruvallur in 1997. It is not this district.
    "chenglepet": "Chengalpattu",
    "chengalpeta": "Chengalpattu",
    "chengalput": "Chengalpattu",
    "chengpaltu": "Chengalpattu",
    "madras": "Chennai",
    "tirunelveli kattabomman": "Tirunelveli",
    "virudunagar": "Virudhunagar",
    "toothukudi": "Thoothukudi",
    # Towns the portal writes in its :City slot. They are not districts and
    # never were, so nothing above resolves them and `Gazetteer.district`
    # returned None — which `resolve_places` reads as "no portal opinion" and
    # silently skips, hiding 43 listings from the conflict flag entirely.
    # Every one of these is the town's own district, verified against the
    # taluk each name belongs to in the gazetteer.
    "periyakulam": "Theni",
    "chidambaram": "Cuddalore",
    "palayamkottai": "Tirunelveli",
    "palani": "Dindigul",
    "kumbakonam": "Thanjavur",
    "pattukottai": "Thanjavur",
    "tindivanam": "Villupuram",
    "udumalaipet": "Tiruppur",
}

# Taluk spellings the fuzzy floor cannot reach. Stated for the same reason as
# the district ones — aliases before similarity — but the bar is higher here,
# because a taluk names its own district: a wrong alias does not merely
# misspell a place, it files the property in the wrong district entirely.
#
# So an entry earns its place only when the notice's own village confirms it.
# "Kodavasal" scores 88.9 against "Kudavasal" and so misses FUZZY_MIN by 1.1 —
# and the village beside it, Manavalanallur, is a real village in Kudavasal
# taluk. That is the evidence, not the similarity score.
#
# 264 listings name a taluk scoring 80-90 against a real one. Most are the same
# kind of variant, but "Tiruppur"/"Thiruppattur" and "Tharamangalam"/
# "Karimangalam" score in that band too and are different places, so the rest
# belong in the human-decision queue rather than here.
# The entries below were harvested the same way, from the 1,182 listings whose
# taluk string resolved to nothing: a candidate was proposed only when exactly
# one taluk in the notice's own district matched it, and kept only when a
# village on that same notice is a real village of the proposed taluk. 67
# spellings clear that bar and carry 371 listings; the 100 that do not stay
# out. Most of those are one-off OCR damage, and OCR damage is the fuzzy
# matcher's job, not a lookup table's.
#
# "Tirupattur" and its spellings are deliberately absent. Tirupathur (its own
# district) and Thiruppattur (Sivaganga) fold to the same key, so no global
# alias can name one without misfiling the other — the district decides, and
# this table cannot see it.
TALUK_ALIASES = {
    "kodavasal":           "Kudavasal",
    "andipatti":           "Aundipatti",
    "animalai":            "Anaimalai",
    "aravakurichi":        "Arvakurichi",
    "arni":                "Arani",
    "barugur":             "Bargur",
    "chengalpet":          "Chengalpattu",
    "chengpaltu":          "Chengalpattu",
    "denkanikottai":       "Denkanikotta",
    "ganangavalli":        "Gangavalli",
    "gandharvakottai":     "Gandarvakottai",
    "gopi chettipalyam":   "Gobichettipalayam",
    "gopichettipalyam":    "Gobichettipalayam",
    "gummidipondu":        "Gummidipoondi",
    "gumudipoondi":        "Gummidipoondi",
    "kaadaiyampatty":      "Kadayampatti",
    "kaliukudi":           "Kalligudi",
    "kangeyam":            "Kangayam",
    "kantharvakottai":     "Gandarvakottai",
    "killyoor":            "Killiyoor",
    "kinathukadavu":       "Kinathukkdavu",
    "kundathur":           "Kundrathur",
    "kuninjipadi":         "Kurinjipadi",
    "kunrathur":           "Kundrathur",
    "madaravoyal":         "Maduravoyal",
    "madathakulam":        "Madathukulam",
    "madhukarai":          "Madukkarai",
    "madukkari":           "Madukkarai",
    "natrampalli":         "Natarampalli",
    "oddanchathram":       "Oddenchatram",
    "orathanadu":          "Orathanad",
    "palladom":            "Palladam",
    "paramathi vellore":   "Paramathivelur",
    "poonamalle":          "Poonamallee",
    "sangagiri":           "Sankari",
    "sankagiri":           "Sankari",
    "sherahmahadevi":      "Cheranmahadevi",
    "shoinganallur":       "Sholinganallur",
    "shoinganatur":        "Sholinganallur",
    "sholingur":           "Sholinghur",
    "siperumbudur":        "Sriperumbudur",
    "sivagangai":          "Sivaganga",
    "sriperumbadur":       "Sriperumbudur",
    "sriperumbur":         "Sriperumbudur",
    "sriperumpudur":       "Sriperumbudur",
    "sripurumbudur":       "Sriperumbudur",
    "srirangam":           "Srirengam",
    "srivaikuntam":        "Srivaikundam",
    "striperumbudur":      "Sriperumbudur",
    "thandampattu":        "Thandarampattu",
    "thirukazhkundram":    "Tirukalukundram",
    "thirukazhukundram":   "Tirukalukundram",
    "thirukkuvai":         "Thirukkuvalai",
    "thirupparankundrum":  "Thirupparankundram",
    "thirupurur":          "Thiruporur",
    "thiruvaidaimaruthur": "Thiruvidaimarudur",
    "thiruvidaimaruthur":  "Thiruvidaimarudur",
    "thisaiyanvilai":      "Thisayanvilai",
    "thovalal":            "Thovalai",
    "ushilamppatti":       "Usilampatti",
    "utnukkottai":         "Uthukottai",
    "vedharanyam":         "Vedaranyam",
    "vedsandur":           "Vedasandur",
    "veerakeralamputhur":  "Veerakeralampudur",
    "vilavancode":         "Vilavamcode",
    "virdhachalam":        "Vridhachalam",
    "walaja":              "Walajah",
    "walajaa":             "Walajah",
}

# Chennai is fully urban and keeps no revenue villages, so 12 of its taluks
# hold zero in the gazetteer. A village that cannot be found there is a gap in
# the reference data, not a bad read of the notice, and must be reported as
# such rather than counted as a failure.
VILLAGE_NOT_APPLICABLE = "taluk-has-no-villages"

# Chennai city is divided into REGISTRATION taluks — "Mambalam-Guindy",
# "Egmore-Nungambakkam", "Fort-Tondiarpet" — which the revenue record does not
# hold at all, because the city proper keeps no revenue villages. A notice
# naming one is not misread: there is no village to find. 290 lots sat in
# `no-parent-taluk` for this reason, reading as extraction failures.
#
# Matched on distinctive tokens rather than whole names, because the composites
# arrive in both orders and with the second half misread every way the corpus
# can manage — Guindy also as "Gundy" and "Gandy", Purasawalkam also as
# "Purasaiwakkam". The token is what survives that.
#
# Guarded by the district: each of these is a Chennai locality, and this table
# must not speak for a place of the same name elsewhere. Note the five OUTER
# Chennai taluks (Sholinganallur, Madhavaram, Maduravoyal, Thiruvottiyur,
# Alandur) are deliberately absent — they do hold revenue villages, 48 between
# them, and a lot naming one has a real answer to find.
_CHENNAI_CITY_TALUK_TOKENS = frozenset((
    "mambalam", "guindy", "gundy", "gandy", "egmore", "nungambakkam",
    "nungabakkam", "nungambakam", "purasawalkam", "purasaiwakkam",
    "purasaiwalkam", "perambur", "perumbur", "tondiarpet", "mylapore",
    "triplicane", "saidapet", "ayanavaram", "aminjikarai", "velachery",
))

# A property in another state cannot match a Tamil Nadu gazetteer, and calling
# that a failed match hides the real gap. Worse, it invites a wrong one: a
# Kerala district string is close enough to reach for a Tamil Nadu name at the
# fuzzy floor, and a wrong place is worse than a missing one.
#
# Only names that are unambiguous at district level are listed — none of the 38
# Tamil Nadu districts folds to any key below, which is the bar for an entry.
# Taluk and village names are NOT listed: "Kollam" is a Kerala district and
# also a Tamil Nadu village, and this table cannot tell which one a notice
# means.
OUTSIDE_STATE = "outside-tamil-nadu"

_NON_TN_DISTRICT_NAMES = (
    # Kerala
    "Thiruvananthapuram", "Trivandrum", "Kollam", "Pathanamthitta",
    "Alappuzha", "Kottayam", "Idukki", "Ernakulam", "Thrissur", "Palakkad",
    "Malappuram", "Kozhikode", "Wayanad", "Kannur", "Kasaragod",
    # neighbours that turn up in the corpus
    "Chittoor", "Puducherry", "Pondicherry", "Karaikal",
    "Sindhudurg", "Kolhapur", "Ratnagiri", "Ratlam",
)

_NON_TN_STATE_NAMES = (
    "Kerala", "Karnataka", "Andhra Pradesh", "Telangana", "Maharashtra",
    "Madhya Pradesh", "Puducherry", "Pondicherry", "Goa", "Gujarat",
    "Odisha", "West Bengal", "Delhi", "Rajasthan", "Uttar Pradesh",
)

# Office words in a Sub-Registrar's Office name that are not part of the place:
# "Dindigul Joint -II", "Cuddalore II Joint", "Annur SRO".
_SRO_QUALIFIER = re.compile(
    r"\b(joint|sro|sub|registrar|registrar's|office|circle|jt|no)\b\.?", re.I)


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


# Folded here rather than beside the names above, because normalize_place is
# what decides whether two spellings are the same place.
_NON_TN_DISTRICTS = frozenset(normalize_place(n) for n in _NON_TN_DISTRICT_NAMES)
_NON_TN_STATES = frozenset(normalize_place(n) for n in _NON_TN_STATE_NAMES)


def outside_tamil_nadu(*, district: str | None = None,
                       state: str | None = None) -> bool:
    """True when the notice names a state or district Tamil Nadu does not hold.

    Read before any matching: the point is to refuse a Kerala property outright
    rather than let its district string fuzzy-reach for a Tamil Nadu one.
    """
    if state and normalize_place(state) in _NON_TN_STATES:
        return True
    return bool(district and normalize_place(district) in _NON_TN_DISTRICTS)


# The direction and numeral suffixes the state appends when it splits a taluk.
_SPLIT_SUFFIX = re.compile(r"\s+(north|south|east|west|i{1,3}|\d+)$", re.I)


def names_a_chennai_city_taluk(value: str) -> bool:
    """True when this taluk string names one of Chennai city's registration
    taluks, which the revenue record does not hold."""
    tokens = re.split(r"[^a-z]+", (value or "").lower())
    return any(tok in _CHENNAI_CITY_TALUK_TOKENS for tok in tokens if tok)


def _sro_place(value: str) -> str:
    """The place inside a Sub-Registrar's Office name.

    "Dindigul Joint -II" is the SRO at Dindigul; the rest is office bookkeeping.
    Trailing numerals go with it — an SRO is numbered where one town has
    several, and the number never distinguishes two taluks.
    """
    s = _SRO_QUALIFIER.sub(" ", value or "")
    s = re.sub(r"[-–&,]", " ", s)
    s = re.sub(r"\b(i{1,3}|iv|v|\d+)\b", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


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
        # Every village of the state under one key, so a name carried by
        # exactly one village can be placed with no parent at all. 70% of
        # village names are shared, which is why this is a last resort and
        # never a fuzzy one.
        self._v_global: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        for village, taluk, district in self.villages:
            key = normalize_place(village)
            if self._v_by_taluk[taluk].get(key) == village:
                self._v_ambiguous.add((taluk, key))
            self._v_by_taluk[taluk].setdefault(key, village)
            self._v_by_district[district][key].add((village, taluk))
            self._v_global[key].add((village, taluk, district))
        for taluk, district in self.taluks:
            self._t_by_district[district].setdefault(normalize_place(taluk), taluk)
        # Taluks the state has split and the notices have not: the gazetteer
        # holds "Coimbatore North" and "Coimbatore South" where a notice still
        # writes "Coimbatore". Indexed by the shared stem so the siblings can
        # be offered to the village, which is the only thing that can choose
        # between them.
        self._t_split: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for taluk, district in self.taluks:
            stem = _SPLIT_SUFFIX.sub("", taluk).strip()
            if stem and normalize_place(stem) != normalize_place(taluk):
                self._t_split[normalize_place(stem)].append((taluk, district))
        # Districts holding no revenue villages at all (Chennai). Kept as a set
        # of the districts that DO hold them, so an unknown name is never
        # mistaken for an urban one.
        self._d_with_villages: set[str] = {d for _, _, d in self.villages}

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
        # Aliases before similarity, as with districts. An alias naming a taluk
        # this gazetteer does not hold falls through to the normal path rather
        # than returning None, so a stale entry degrades to today's behaviour
        # instead of blanking a taluk that would otherwise have matched.
        alias = TALUK_ALIASES.get(re.sub(r"\s+", " ", value.lower().strip()))
        if alias:
            hit = self._t.get(normalize_place(alias))
            if hit:
                return hit
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

    def district_has_villages(self, district: str) -> bool:
        """False for a district the revenue record keeps no villages in."""
        return district in self._d_with_villages

    def taluk_from_sub_registrar(self, value: str,
                                 village: str) -> tuple[str, str] | None:
        """``(taluk, district)`` named by a Sub-Registrar's Office.

        Registration and revenue are two different hierarchies that happen to
        share most of their names, so a match here is a coincidence worth
        using and never one worth trusting on its own: the answer is returned
        only when ``village`` is a real village of that taluk. Exact only —
        the fuzzy floor was calibrated on revenue names, and a fuzzy leap
        across hierarchies would file the property in the wrong district.
        """
        place = _sro_place(value)
        if not place or not village:
            return None
        hit = self._t.get(normalize_place(place))
        if not hit:
            return None
        taluk, district = hit
        return hit if self.village(village, taluk, fuzzy=False) else None

    def split_taluk_for(self, value: str,
                        village: str) -> tuple[str, str] | None:
        """``(taluk, district)`` for a taluk the state has split since.

        The notice writes "Coimbatore"; the gazetteer holds Coimbatore North
        and South. The village decides which — and only when exactly one of
        the siblings holds it, because guessing between them is a coin flip.
        """
        siblings = self._t_split.get(normalize_place(value or ""))
        if not siblings or not village:
            return None
        hits = [(t, d) for t, d in siblings
                if self.village(village, t, fuzzy=False)]
        return hits[0] if len(hits) == 1 else None

    def village_anywhere(self, value: str) -> tuple[str, str, str] | None:
        """``(village, taluk, district)`` for a name carried by one village.

        The last resort, for a notice that names no parent at all. Exact only
        and unique-or-nothing: 1,150 village names are shared, and at state
        scope a fuzzy match would have 17,164 chances to be wrong.
        """
        found = self._v_global.get(normalize_place(value or "")) or set()
        if len(found) != 1:
            return None
        village, taluk, district = next(iter(found))
        return (village, taluk, district) if (
            taluk, normalize_place(value)) not in self._v_ambiguous else None


def resolve_place(gaz: Gazetteer, *, district: str | None = None,
                  taluk: str | None = None,
                  village: str | None = None,
                  registration_district: str | None = None,
                  sub_registrar: str | None = None,
                  state: str | None = None) -> dict:
    """Resolve one notice's place fields against the gazetteer.

    Bottom-up: the taluk is tried first because it carries its district, so a
    misspelt or outdated district string is corrected rather than believed.
    The village is then looked up only within that taluk.

    ``registration_district`` is the SRO division the notice quotes for the
    sale deed, and it is consulted ONLY when the revenue fields resolve to
    nothing — see the block below for why it is a last resort and why it
    never supplies a taluk.

    Returns the resolved names, what each was derived from, and — when the
    village cannot be placed — why, so the review queue can tell a bad read
    from a gap in the reference data.
    """
    out = {
        "district": None, "taluk": None, "village": None,
        "district_source": None, "village_source": None, "village_status": None,
        "raw": {"district": district, "taluk": taluk, "village": village,
                "registration_district": registration_district},
        "conflict": False,
    }
    # Before any matching. A property in another state has no answer in this
    # gazetteer, and letting its district string reach the fuzzy floor would
    # produce a Tamil Nadu one anyway.
    if outside_tamil_nadu(district=district, state=state):
        out["village_status"] = OUTSIDE_STATE
        return out

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

    # Last resort: the registration (SRO) district. Hundreds of notices quote
    # only "SRO Chidambaram" and leave the revenue hierarchy unwritten, which
    # used to leave the lot with no district at all — unsearchable, unfilterable
    # and unpriceable. Registration and revenue are different divisions, so this
    # is deliberately the weakest source and gives the DISTRICT ONLY:
    #
    #   * An SRO district usually names a TALUK ("Chidambaram", "Tindivanam",
    #     "Palani"), not a revenue district, so the taluk lookup is tried after
    #     the district one — a taluk carries its district for free.
    #   * The taluk it names is NOT recorded. SRO boundaries do not follow
    #     taluk boundaries, so the office named Chidambaram serves land outside
    #     Chidambaram taluk. Its district is reliable; its taluk is not.
    #
    # The source is stamped so a district derived this way is never mistaken
    # for one the notice actually stated (see :attr:`district_source`).
    if not out["district"] and registration_district:
        from_reg = gaz.district(registration_district)
        if from_reg:
            out["district"] = from_reg
            out["district_source"] = "registration-district"
        else:
            names_taluk = gaz.taluk(registration_district)
            if names_taluk:
                out["district"] = names_taluk[1]
                out["district_source"] = "registration-district-names-a-taluk"

    if not village:
        out["village_status"] = "absent"
        return out

    # No taluk yet. Three ways to earn one, each requiring the notice's own
    # village as evidence, in order of how much they assume.
    if not out["taluk"]:
        # The Sub-Registrar's Office names it. The commonest gap by far: the
        # extractor fills the registration hierarchy (R.D / S.R.O) and leaves
        # the revenue taluk empty, because that is how the notice is worded.
        #
        # This is NOT the rule above relaxed. That one reads the registration
        # DISTRICT and refuses to take a taluk from it, because SRO district
        # boundaries do not follow taluk boundaries. This one reads the
        # registration SUB-district — the office itself, one level down — and
        # still refuses to trust the name alone: the taluk is taken only when
        # the notice's own village turns out to be a real village of it. The
        # village is the evidence, exactly as it is for an entry in
        # TALUK_ALIASES. Without it the coincidence proves nothing and the lot
        # stays in the queue.
        by_sro = (gaz.taluk_from_sub_registrar(sub_registrar, village)
                  if sub_registrar else None)
        if by_sro:
            out["taluk"], from_sro_district = by_sro
            out["conflict"] = bool(out["district"]
                                   and out["district"] != from_sro_district)
            out["district"] = from_sro_district
            out["district_source"] = "sub-registrar"
        else:
            # The taluk has been split since the notice was written.
            split = gaz.split_taluk_for(taluk, village) if taluk else None
            if split:
                out["taluk"], out["district"] = split
                out["district_source"] = "taluk-split"

    if not out["taluk"]:
        # Nothing names a parent, but the village name itself may be carried by
        # only one village in the state.
        if not out["district"]:
            alone = gaz.village_anywhere(village)
            if alone:
                out["village"], out["taluk"], out["district"] = alone
                out["district_source"] = "village"
                out["village_status"] = "resolved"
                out["village_source"] = "state"
                return out
        # Two ways the reference data, not the notice, is what ran out: a
        # district holding no revenue villages at all, and Chennai city, whose
        # registration taluks hold none even though its outer ones do. Either
        # way there is no village to find, so this is not queued as a bad read.
        if out["district"] and (
                not gaz.district_has_villages(out["district"])
                or (out["district"] == "Chennai"
                    and names_a_chennai_city_taluk(taluk))):
            out["village_status"] = VILLAGE_NOT_APPLICABLE
            return out
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
