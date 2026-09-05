"""Which place name a listing is shown — and searched — under.

The notice is the trusted source and the portal is only a witness. That is
already how the graph is built: :mod:`scripts.resolve_places` matches the
village/taluk/district the sale notice names against the Tamil Nadu revenue
gazetteer, writes ``revenue_district`` / ``revenue_taluk`` /
``revenue_village`` plus the ``LOCATED_IN_DISTRICT`` edge, and records the
portal's disagreement as ``place_portal_conflict`` — a tripwire, never an
input.

What was missing is the read side. The portal's ``:City`` edge kept being
returned beside the notice-resolved district, so a listing whose notice puts
it in Chengalpattu still displayed "Chennai" — the same listing in two places
at once, with nothing saying which one the notice actually supports.

So the resolved revenue district wins, and the portal City is the fallback
for a listing place resolution never reached — never the override. This is
deliberately the same shape as
:func:`pipeline.property_taxonomy.effective_bucket`, for the same reason: a
listing with no notice value must stay findable rather than vanish from a
district search because the better source is silent about it.

The expression is Cypher rather than Python because every caller reads it
straight out of a query, and one shared string is what keeps the browse
filter, its facet, and the row it renders agreeing on a single answer.
"""
from __future__ import annotations


def district_effective(prop: str = "a", city: str | None = None,
                       var: str = "_pc") -> str:
    """A Cypher expression for the district a listing should be shown as.

    ``prop`` is the bound :AuctionProperty variable. Pass ``city`` when the
    query already OPTIONAL MATCHes the portal :City node (cheaper, and keeps
    the existing plan); leave it out and the portal side is read with a
    pattern comprehension, which needs no extra clause and so can be dropped
    into a bare WHERE.

    ``var`` names the comprehension's own variable. Two comprehensions in one
    clause must not both declare the same name, so a caller using this twice
    side by side passes a distinct one.
    """
    portal = (f"{city}.name" if city
              else f"[({prop})-[:LOCATED_IN_CITY]->({var}:City) | {var}.name][0]")
    return f"coalesce({prop}.revenue_district, {portal})"


def suppress_portal_city(row: dict, city_key: str = "city",
                         district_key: str = "district") -> dict:
    """Drop the portal city from a row that already carries a notice district.

    Payloads that return both keys (the agent tools) do not need the
    coalesce: the notice district is already there under its own name, so the
    portal value is not a fallback but a second, contradicting answer. Remove
    it and the remaining one is unambiguous.

    Mutates and returns ``row`` — callers shape rows in place.
    """
    if row.get(district_key):
        row[city_key] = None
    return row
