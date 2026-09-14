"""The audit that says whether strong pairs would merge distinct properties in the spine."""
from scripts.audit_listing_links import same_source_clusters


def _src(aid):
    return "baanknet" if aid.startswith("bn-") else "eauctionsindia"


def _p(a, b, conf="PROBABLE"):
    return {"a_id": a, "b_id": b, "a_source": _src(a), "b_source": _src(b), "method": "borrower", "confidence": conf}


def test_only_strong_clusters_with_two_listings_of_one_portal_are_reported():
    pairs = [_p("bn-1", "10"), _p("bn-2", "10"),            # bn-1 and bn-2 would merge through 10
             _p("bn-3", "30"),                              # a clean one-to-one pair
             _p("bn-4", "40"), _p("bn-5", "40", "INFERRED")]  # INFERRED never merges
    assert same_source_clusters(pairs) == [["10", "bn-1", "bn-2"]]
