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
    # LGD's own spelling of the district, which the fold cannot reach: the
    # doubled "n" survives it, so "kaniyakumari" never meets "kanyakumari".
    # This recovers no place by itself — Kanyakumari's taluks resolve on their
    # own names — but without it the district is simply unknown for every
    # Kanyakumari row of an official state export (1,086 of them), which means
    # any caller cross-checking a taluk against the district it was filed under
    # has nothing to check against, and silently skips the check.
    "kanniyakumari": "Kanyakumari",
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
#
# The eleven below were harvested differently, and to a higher bar, from the
# Local Government Directory's village-to-gram-panchayat export for Tamil Nadu
# (20,277 rows, 2026-09-21) — see scripts/lgd_village_mapping_to_csv.py. These
# are not OCR damage or a notice's guess; they are how the state register itself
# spells eleven taluks this graph holds under another name, and they are the
# eleven the fuzzy floor cannot reach: of the 38 spellings in that export that a
# strict fold rejects, similarity finds 27 on its own and these eleven score
# 57-88, below FUZZY_MIN. They carry 618 rows of the export.
#
# Each earned its place on evidence stronger than the rule above asks for: the
# export states its own district, so candidates were drawn only from the taluks
# of that district, and every entry has exactly one candidate whose villages
# overlap the incoming ones — 67 of Virudhachalam's 127 villages are already
# Vridhachalam's in the graph, 46 of Palakkodu's 71 are Palacode's, 39 of
# Vazhapadi's 72 are Valapady's. The runner-up in every case sits more than 20
# similarity points behind with an overlap of 0-2, so none of these is a
# near-twin of the kind this table refuses.
#
# Two are worth naming. "Udhagamandalam" -> "Udhagai" scores only 57 and is kept
# anyway: 13 of its 19 villages are Udhagai's, which is the evidence, and the
# graph simply holds Ooty under its short name. "Purasawalkam" is the one entry
# no village confirms — Chennai keeps no revenue villages, so there were none to
# check — and it is kept on the name alone, 83 against a runner-up at 50.
#
# Deliberately left out: "Kolathur [Chennai]" (3 rows). LGD lists it as a
# Chennai taluk and the graph has no taluk it resembles, so it is a hole in the
# hierarchy, not a spelling — a finding, per this module's own rule, rather than
# a row to invent.
TALUK_ALIASES = {
    "mathavaram":          "Madhavaram",
    "palakkodu":           "Palacode",
    "pallipattu":          "Pallipet",
    "panthalur":           "Pandalur",
    "purasawalkam":        "Purasaivakkam",
    "shenkottai":          "Shencottai",
    "sirkali":             "Sirkazhi",
    "thandrampet":         "Thandarampattu",
    "udhagamandalam":      "Udhagai",
    "vazhapadi":           "Valapady",
    "virudhachalam":       "Vridhachalam",
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

#: A property this gazetteer will never place because it is not in Tamil Nadu.
#: Distinct from every other failure here: those say "the reference data or the
#: read let us down", this says "there is nothing to look up". Without it, 167
#: out-of-state lots sat in `no-parent-taluk` looking like 15% of a bucket
#: someone might try to fix.
OUTSIDE_TAMIL_NADU = "outside-tamil-nadu"

#: States and union territories other than Tamil Nadu, matched against the
#: notice's own `state` field — the one unambiguous signal, since a notice that
#: says "Kerala" is not describing Tamil Nadu whatever else it says. 46 lots.
NON_TN_STATES = {
    "kerala", "karnataka", "andhra pradesh", "telangana", "maharashtra",
    "puducherry", "pondicherry", "goa", "odisha", "orissa", "gujarat",
    "chhattisgarh", "madhya pradesh", "rajasthan", "delhi", "new delhi",
    "west bengal", "bihar", "jharkhand", "uttar pradesh", "haryana", "punjab",
    "assam", "uttarakhand", "himachal pradesh", "jammu and kashmir",
}

#: Districts of other states, consulted ONLY when the Tamil Nadu gazetteer
#: cannot place the district string itself.
#:
#: This list is enumerated rather than inferred, because "the gazetteer cannot
#: map it" is NOT evidence of another state: of the 242 unmappable district
#: strings in the corpus, "Thiruvurur" is Thiruvarur and "Trichirapalli" is
#: Tiruchirappalli — Tamil Nadu districts misspelt past the fold. Treating
#: unmappable as foreign would file those abroad, which is worse than leaving
#: them unresolved. So only names verified as another state's district are here,
#: and every one of them appears in the corpus.
#:
#: Kerala dominates (115 lots) because it borders three Tamil Nadu districts and
#: the same banks auction on both sides. "Palakkad" is Kerala's; Tamil Nadu's
#: similar-looking Palakkodu (Dharmapuri) is a TALUK and resolves as one, so the
#: two never meet here.
NON_TN_DISTRICTS = {
    # Kerala
    "thiruvananthapuram", "trivandrum", "kollam", "quilon", "pathanamthitta",
    "puthanmathitta", "alappuzha", "alleppey", "kottayam", "idukki",
    "ernakulam", "kochi", "cochin", "thrissur", "trichur", "palakkad",
    "malappuram", "kozhikode", "calicut", "wayanad", "kannur", "cannanore",
    "kasaragod",
    # Andhra Pradesh
    "chittoor", "guntur", "nellore", "anantapur", "kurnool", "prakasam",
    # Karnataka
    "bangalore", "bengaluru", "bangalore urban", "bangalore rural", "mysore",
    "mysuru", "kolar", "tumkur", "hassan", "mandya", "chamarajanagar",
    # Maharashtra
    "sindhudurg", "kolhapur", "ratnagiri", "palghar", "thane", "pune",
    # elsewhere, each seen in the corpus
    "ratlam", "ganjam", "nawada", "puducherry", "pondicherry", "karaikal",
}


def outside_tamil_nadu(gaz: Gazetteer, *, district: str | None,
                       state: str | None) -> bool:
    """Does the notice place this property outside Tamil Nadu?

    The state field decides on its own. The district field only speaks when the
    Tamil Nadu gazetteer cannot place it — a district that resolves here is a
    Tamil Nadu district, whatever a stale alias elsewhere might suggest.
    """
    folded_state = " ".join(str(state or "").strip().lower().split())
    if folded_state and folded_state in NON_TN_STATES:
        return True
    raw = " ".join(str(district or "").strip().lower().split())
    if raw and not gaz.district(district) and raw in NON_TN_DISTRICTS:
        return True
    return False


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


def already_held_as(value: str, pool: dict[str, str]) -> tuple[str, float] | None:
    """The name in ``pool`` this resolver would read as ``value``, and its score.

    ``pool`` is ``{folded key: official name}`` — one taluk's villages, as
    ``Gazetteer`` indexes them. The question is the inverse of the usual one: not
    "which village does this notice mean" but "is this incoming name a place the
    reference already holds, spelled differently". Same matcher, same guards, so
    a caller loading an official list can decide not to add a near-twin of a
    village that is already there — which would leave the two scoring within
    ``FUZZY_MARGIN`` of each other and cost the resolver a village it places
    correctly today (see scripts/refresh_village_gazetteer.py).
    """
    return _fuzzy_match(normalize_place(value), pool)


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

    def compound_taluk(self, value: str) -> tuple[tuple[str, str] | None,
                                                  str | None]:
        """Pull a taluk, or at least a district, out of a two-named string.

        Notices write a taluk as a pair: the old Chennai composites
        ("Egmore-Nungambakkam", "Mylapore-Triplicane", "Fort - Tondaiarpet")
        and the 2019 renamings ("Sriperumbudur Taluk, Now Kundrathur Taluk").
        Neither half is the whole string, so ``taluk`` finds nothing and 27 lots
        lose their geography to punctuation.

        Returns ``(taluk_hit, district)``, either of which may be None:

        * A half marked as the current name — "now X", "new X" — wins outright.
          The gazetteer holds today's register, so when a notice says a taluk is
          *now* Kundrathur, Kundrathur is the answer and Sriperumbudur is
          history.
        * Otherwise a single resolving half is taken.
        * Several halves naming DIFFERENT taluks is refused — "Perambur-
          Purasawalkam" is two real Chennai taluks and picking one is a
          coin flip. But when they agree on a district, that district is
          returned on its own: it is the truth they share, and it lets the
          village be searched district-wide instead of not at all.
        """
        parts = [p.strip() for p in re.split(r"[-/,&]| and ", value or "")
                 if p.strip()]
        if len(parts) < 2:
            return None, None

        # "Now Kundrathur" / "New Kundrathur": the marker is the notice telling
        # us which name is current, so it is read before anything else.
        for part in parts:
            m = re.match(r"^(?:now|new)\s+(.*)$", part, flags=re.I)
            if m:
                hit = self.taluk(m.group(1))
                if hit:
                    return hit, hit[1]

        hits = {self.taluk(p) for p in parts}
        hits.discard(None)
        if len(hits) == 1:
            hit = next(iter(hits))
            return hit, hit[1]
        if hits:
            districts = {d for _, d in hits}
            if len(districts) == 1:
                return None, next(iter(districts))
        return None, None


def resolve_place(gaz: Gazetteer, *, district: str | None = None,
                  taluk: str | None = None,
                  village: str | None = None,
                  registration_district: str | None = None,
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
                "registration_district": registration_district,
                "state": state},
        "conflict": False,
    }

    # Asked and answered before anything else: a property in Kerala is not a
    # failure of this gazetteer, and saying so is the only honest status for it.
    if outside_tamil_nadu(gaz, district=district, state=state):
        out["village_status"] = OUTSIDE_TAMIL_NADU
        return out

    t = gaz.taluk(taluk) if taluk else None
    d_direct = gaz.district(district) if district else None

    # The taluk written as a pair — an old Chennai composite, or a 2019
    # renaming — resolves to neither half on its own.
    compound_district = None
    if not t and taluk:
        t, compound_district = gaz.compound_taluk(taluk)

    if t:
        out["taluk"], out["district"] = t
        out["district_source"] = "taluk"
        # A district that disagrees with its own taluk is worth surfacing: it
        # is usually the 2019 reorganisation, occasionally a misread.
        out["conflict"] = bool(d_direct and d_direct != t[1])
    elif d_direct:
        out["district"] = d_direct
        out["district_source"] = "district"
    elif compound_district:
        # Both halves named a real taluk and disagreed, but agreed on the
        # district. That much is not a coin flip.
        out["district"] = compound_district
        out["district_source"] = "compound-taluk-field"
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
