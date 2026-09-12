"""
pipeline/place_lineage.py
-------------------------
Which Tamil Nadu districts used to be one district, and what that means for a
portal/notice disagreement.

``scripts/resolve_places.py`` records ``place_portal_conflict`` whenever the
portal's :City, mapped to a district, differs from the district the notice
resolved to. It is a plain string inequality::

    portal = gaz.district(p["city"])
    portal_conflict = bool(portal and portal != res["district"])

On the live corpus that fires on **527 of 2,964 listings**, and the number has
never been worked, because as a single boolean it cannot separate three very
different things:

* **232** are the state re-cutting its districts. Tamil Nadu split five
  districts in 2019 and one more in 2020, and the portal still carries the
  pre-split parent name. ``Kanchipuram -> Chengalpattu`` alone is 152 of them.
  The notice is right, the portal is stale, and there is nothing to fix.
* **188** are the Chennai metro. The portal writes a *city* covering three
  revenue districts; the notice writes the revenue district. The two are not
  answering the same question, so comparing them at all is the mistake.
* **~107** are genuine disagreement, and several pairs appear in **both**
  directions (Coimbatore/Tiruppur 18 and 9, Salem/Namakkal 5 and 2), which
  means at least one side is mis-resolving. That is the real queue.

So this module does not suppress anything. It turns one boolean into a named
reason, exactly the way :data:`place_village_status` already names why a
village stopped where it did — a reviewer should not have to know Tamil Nadu's
administrative history to read the backlog.

**The lineage convention: the remainder keeps the identity.** When a district
splits, the piece that keeps the old name is treated as the parent and the new
piece as its child, even where the split was really a three-way rename. So
Chengalpattu and Tiruvallur are both children of Kancheepuram, and Villupuram
is a child of Cuddalore, though all four descend from districts (Chengalpattu-
MGR, South Arcot) that no longer exist under those names. Modelling the
vanished parents would add nodes no source ever writes; what a notice or a
portal actually carries is the surviving name.

Years are the date of formation. They are carried because a reviewer reading
``portal-names-parent`` wants to know whether the portal is six years stale or
forty — not because anything branches on them.
"""
from __future__ import annotations

#: child district -> (the district it was carved out of, year formed).
#: Names are spelled as :District nodes spell them in the graph; a key or value
#: that drifts from the gazetteer silently stops matching, which is why
#: `test_place_lineage.py` asserts every name here is a real district.
DISTRICT_LINEAGE: dict[str, tuple[str, int]] = {
    # 2019-2020 reorganisation — the splits that produce most of the 527.
    "Chengalpattu":   ("Kancheepuram", 2019),
    "Ranipet":        ("Vellore", 2019),
    "Tirupathur":     ("Vellore", 2019),
    "Kallakurichi":   ("Villupuram", 2019),
    "Tenkasi":        ("Tirunelveli", 2019),
    "Mayiladuthurai": ("Nagapattinam", 2020),
    # Earlier splits. A notice can be years old and a portal row older still,
    # so these keep firing long after the event.
    "Tiruppur":       ("Coimbatore", 2009),
    "Ariyalur":       ("Perambalur", 2007),
    "Krishnagiri":    ("Dharmapuri", 2004),
    "Namakkal":       ("Salem", 1997),
    "Thiruvarur":     ("Nagapattinam", 1997),
    "Tiruvallur":     ("Kancheepuram", 1997),
    "Theni":          ("Madurai", 1996),
    "Karur":          ("Thiruchirappalli", 1995),
    "Perambalur":     ("Thiruchirappalli", 1995),
    "Villupuram":     ("Cuddalore", 1993),
    "Nagapattinam":   ("Thanjavur", 1991),
    "Tiruvannamalai": ("Vellore", 1989),
    "Thoothukudi":    ("Tirunelveli", 1986),
    "Dindigul":       ("Madurai", 1985),
    "Sivagangai":     ("Ramanathapuram", 1985),
    "Virudhunagar":   ("Ramanathapuram", 1985),
    "Erode":          ("Coimbatore", 1979),
    "Pudukkottai":    ("Thiruchirappalli", 1974),
    "Dharmapuri":     ("Salem", 1965),
}

#: The Chennai metro spans four revenue districts, and the portal's :City for
#: any of them is as likely to read "Chennai" as the district's own name —
#: it is a postal/metro label, not a revenue one. Kancheepuram and Tiruvallur
#: are already ancestors of Chengalpattu and of each other's line, so lineage
#: catches most of these; this set covers the rest, where the two sources are
#: simply naming different levels of the same place.
METRO_RING = frozenset({"Chennai", "Chengalpattu", "Kancheepuram", "Tiruvallur"})

#: Reason codes written to ``p.place_portal_conflict_kind``. Ordered from
#: "nothing to do" to "someone must read the notice".
AGREE = "agree"
PORTAL_NAMES_PARENT = "portal-names-parent"
NOTICE_NAMES_PARENT = "notice-names-parent"
METRO = "metro-ring"
TALUK_OUTRANKS = "taluk-outranks-portal"
UNEXPLAINED = "unexplained"
NOT_A_DISTRICT = "portal-not-a-district"
NO_NOTICE_DISTRICT = "no-notice-district"
NO_PORTAL_DISTRICT = "no-portal-district"

#: `place_district_source` values that make the notice side authoritative.
#: A taluk name is globally unique across all 316 — it names its own district —
#: so a district derived from a matched taluk is the strongest answer this
#: pipeline produces, and a portal City disagreeing with it says something
#: about the portal, not about the district. Measured on the 69 conflicts left
#: after lineage and the metro ring: 58 came from a matched taluk, and in all
#: 58 that taluk sits in the stored district, with zero contradictions.
#:
#: `district` is NOT here. That value means the district came from the notice's
#: own district *string* with no taluk to check it against, which is exactly the
#: case a portal disagreement is worth reading.
AUTHORITATIVE_SOURCES = frozenset({"taluk"})

#: Kinds that need no review. `notice-names-parent` is deliberately NOT here:
#: the notice is the trusted source, so it naming the *older* district is a
#: resolution that stopped one level short, not a stale portal.
BENIGN = frozenset({AGREE, PORTAL_NAMES_PARENT, METRO, TALUK_OUTRANKS})

#: Kinds where there was never a disagreement to have, because one side said
#: nothing. These are coverage numbers — `api/review/queries.py::_place_panels`
#: already reports them as "how deep did each property reach" — and counting
#: them again as conflicts would report one gap twice.
NOT_COMPARABLE = frozenset({NO_NOTICE_DISTRICT, NO_PORTAL_DISTRICT})

#: Plain-English labels, for the review panel. Same intent as the `said` map in
#: `api/review/queries.py::_place_panels`: the stored value is a status code
#: and a reviewer should not have to decode it.
SAID = {
    AGREE: "portal and notice agree",
    PORTAL_NAMES_PARENT: "portal names the district this one was split from",
    NOTICE_NAMES_PARENT: "notice names the district this one was split from",
    METRO: "both inside the Chennai metro, naming different levels",
    TALUK_OUTRANKS: "district came from a matched taluk, which outranks the portal",
    UNEXPLAINED: "neither explains the other — read the notice",
    NOT_A_DISTRICT: "portal city the gazetteer cannot map to a district",
    NO_NOTICE_DISTRICT: "the notice resolved no district — nothing to compare",
    NO_PORTAL_DISTRICT: "the listing carries no portal city",
}


def ancestors(district: str) -> list[str]:
    """Every district ``district`` was carved out of, nearest parent first.

    Walks the table rather than reading one level, so a pre-2019 portal row
    still explains a 2020 district: Mayiladuthurai -> Nagapattinam ->
    Thanjavur. The ``seen`` guard is not defensive decoration — a typo that
    makes two districts each other's parent would otherwise hang the resolver
    rather than fail a test.
    """
    out: list[str] = []
    seen = {district}
    current = district
    while True:
        entry = DISTRICT_LINEAGE.get(current)
        if not entry:
            return out
        parent = entry[0]
        if parent in seen:
            return out
        out.append(parent)
        seen.add(parent)
        current = parent


def split_year(child: str, parent: str) -> int | None:
    """The year ``child`` separated from ``parent``, if it is on that line.

    Reports the *most recent* split on the walk up, which is the one a reader
    means: Mayiladuthurai left Thanjavur's line in 1991, but it became its own
    district in 2020, and 2020 is what makes the portal stale.
    """
    current = child
    while True:
        entry = DISTRICT_LINEAGE.get(current)
        if not entry:
            return None
        nxt, year = entry
        if nxt == parent:
            return year
        if current == nxt:
            return None
        current = nxt


def classify(portal_district: str | None, notice_district: str | None,
             *, districts: frozenset[str] | set[str] | None = None,
             district_source: str | None = None) -> str:
    """Name the relationship between the two districts.

    ``portal_district`` is the portal :City already mapped through
    ``Gazetteer.district``; ``notice_district`` is ``p.revenue_district``.
    Pass ``districts`` (the gazetteer's own names) to have a value that is not
    a real district reported as :data:`NOT_A_DISTRICT` instead of silently
    counted as a genuine disagreement — an unmapped city name is a gazetteer
    gap, not a bad notice.

    ``district_source`` is ``p.place_district_source``. Pass it and a
    disagreement with a district that came from a matched taluk reads as
    :data:`TALUK_OUTRANKS` rather than as something to investigate — see
    :data:`AUTHORITATIVE_SOURCES`. Omit it and every disagreement is reported,
    which is the safe direction.

    A missing side is named rather than lumped in with a disagreement: a
    listing whose notice resolved no district never had a conflict to have, and
    the live corpus has 264 of them. Reporting those as conflicts would
    manufacture a backlog out of a coverage number.

    Order matters. Lineage is checked before the metro ring, because
    ``Kancheepuram`` vs ``Chengalpattu`` is both and the lineage answer is the
    more specific one. The taluk rule changes only the two outcomes that would
    otherwise reach a human — it never overrides a label that already says
    *why* the two names differ.
    """
    portal = (portal_district or "").strip()
    notice = (notice_district or "").strip()
    if not notice:
        return NO_NOTICE_DISTRICT
    if not portal:
        return NO_PORTAL_DISTRICT
    if portal == notice:
        return AGREE
    if districts is not None and notice not in districts:
        # The notice side comes out of the gazetteer, so a value that is not a
        # district means the resolver wrote something it should not have.
        return UNEXPLAINED
    if districts is not None and portal not in districts:
        return NOT_A_DISTRICT
    authoritative = district_source in AUTHORITATIVE_SOURCES
    if portal in ancestors(notice):
        return PORTAL_NAMES_PARENT
    if notice in ancestors(portal):
        # "The notice stopped one level short" only holds when nothing checked
        # it. A matched taluk positively names this district — Pollachi IS in
        # Coimbatore — so the parent name is the right answer and the portal's
        # child name is the wrong one, not the other way round.
        return TALUK_OUTRANKS if authoritative else NOTICE_NAMES_PARENT
    if portal in METRO_RING and notice in METRO_RING:
        return METRO
    return TALUK_OUTRANKS if authoritative else UNEXPLAINED


def needs_review(kind: str) -> bool:
    """Whether this kind belongs in the backlog a human works.

    Three states, not two: explained (:data:`BENIGN`), never comparable
    (:data:`NOT_COMPARABLE`), and the rest — which is the queue.
    """
    return kind not in BENIGN and kind not in NOT_COMPARABLE
