"""Which portal listing speaks for an auction listed on several portals.

The bridge (``SAME_LISTING_AS``, written by ``scripts/link_listings.py``)
joins copies of one auction across portals. Until agent3 reads the spine
(plan Task 12), every list of listings — the agent's ``find_properties``,
``GET /properties`` and their facets — keeps ONE copy per bridged cluster:
the lowest ``source_rank`` (BAANKNET 1, bankeauctions 2, eauctionsindia 3),
ties broken by ``auction_id``. The others are reported as ``also_on`` rather
than counted twice. Only CONFIRMED / PROBABLE edges bridge; an INFERRED match
is "possibly the same" and both copies stay visible.

Stdlib only: the browse router and the agent tools both import this.
"""
from __future__ import annotations

#: Bridge grades that mean "the same auction". Mirrors scripts/build_spine.MERGE_GRADES.
BRIDGE_GRADES = ("CONFIRMED", "PROBABLE")

_GRADES = "['CONFIRMED', 'PROBABLE']"


def canonical_listing(prop: str = "a") -> str:
    """Cypher predicate: ``prop`` is its cluster's canonical copy (or is not
    bridged at all). Rank is read with ``coalesce(…, 3)`` because listings
    loaded before the adapters carry no ``source_rank`` yet."""
    return (
        f"NOT EXISTS {{ MATCH ({prop})-[_sl:SAME_LISTING_AS]-(_sp:AuctionProperty) "
        f"WHERE _sl.confidence IN {_GRADES} "
        f"AND (coalesce(_sp.source_rank, 3) < coalesce({prop}.source_rank, 3) "
        f"OR (coalesce(_sp.source_rank, 3) = coalesce({prop}.source_rank, 3) "
        f"AND _sp.auction_id < {prop}.auction_id)) }}"
    )


def also_on(prop: str = "a") -> str:
    """Cypher list: the other portals this auction is listed on."""
    return (f"[({prop})-[_ao:SAME_LISTING_AS]-(_aq:AuctionProperty) "
            f"WHERE _ao.confidence IN {_GRADES} | coalesce(_aq.source, 'eauctionsindia')]")


def other_listings(prop: str = "a") -> str:
    """Cypher list of maps: the bridged copies with what a reader needs to
    follow them — id, portal, link, price, and how they were matched."""
    return (f"[({prop})-[_ol:SAME_LISTING_AS]-(_oq:AuctionProperty) WHERE _ol.confidence IN {_GRADES} | "
            f"{{auction_id: _oq.auction_id, source: coalesce(_oq.source, 'eauctionsindia'), "
            f"url: coalesce(_oq.source_url, _oq.url), reserve_price: _oq.reserve_price_num, "
            f"method: _ol.method, confidence: _ol.confidence}}]")


def photos(prop: str = "a") -> str:
    """Cypher list of maps: the listing's photos, mirrored URL preferred."""
    return (f"[({prop})-[:HAS_MEDIA]->(_ph:Media) WHERE _ph.kind = 'image' | "
            f"{{url: coalesce(_ph.public_url, _ph.url), is_main: coalesce(_ph.is_main, false)}}]")


def source(prop: str = "a") -> str:
    return f"coalesce({prop}.source, 'eauctionsindia')"


def has_photos(prop: str = "a") -> str:
    return f"size(coalesce({prop}.photo_urls, [])) > 0"


def main_first(items: list[dict] | None) -> list[dict]:
    """Photos with the portal's main image first, order otherwise kept."""
    rows = [p for p in (items or []) if p and p.get("url")]
    return sorted(rows, key=lambda p: 0 if p.get("is_main") else 1)
