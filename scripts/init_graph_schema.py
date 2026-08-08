"""
scripts/init_graph_schema.py
----------------------------
Idempotent creation of Neo4j constraints + indexes for the unified auction
graph — the labels the LangExtract promotion pipeline writes on top of the
existing scraped-listing graph.

    python -m scripts.init_graph_schema [--dry-run]

If Bolt (port 7687) is blocked in your environment — Claude Code on the web,
or any HTTP-only egress proxy — prefix with NEO4J_HTTP_API=1 to route through
Aura's HTTPS Query API instead.

Purely additive: every statement is `IF NOT EXISTS`, nothing is dropped, and
none of the existing labels (:AuctionProperty, :Document, :Bank, :City, :Area,
…) change. Safe to re-run.

New labels, grouped by what they model:

  spine        :Lot            one lot of one notice (filename#lot_index)
               :Parcel         the physical land, persisting across notices —
                               replaces the pairwise :SAME_PROPERTY_AS guess
  asset detail :Identifier     survey/patta/door/CERSAI numbers (the dedup key)
               :Measurement    one extent, with its unit and normalized sq.ft
               :Unit           conversion factors (cent/acre/are/hectare/…)
               :Boundary       one side: adjacency, road width, dimension
               :Schedule       a sub-parcel (Schedule A/B/C, Item 1/2/3)
               :Amenity        shared facet node
               :Fact           `extras` as queryable {key, value}
  sale event   :Auction        one sale event; re-auctions accrue per parcel
               :EMDAccount     where EMD is remitted (shared across notices)
               :TermsTemplate  hash-deduped T&C block
               :Contact        phone/email, shared across notices
               :Platform       BAANKNET / MSTC / … (both sources name it)
  enforcement  :LegalFramework SARFAESI | DRT | IBC | other
               :CaseReference  DRT/NCLT case numbers
               :Officer        authorised officers and IBC liquidators
               :Trust          ARC securitisation trusts
               :LoanAccount    the debt itself
  geography    :PlaceAlias     every spelling of a place, from any source
               :Locality       genuine sub-village names (nagar/colony/layout)
               :LocalBody      panchayat | municipality | corporation
               :RegDistrict    registration (SRO) hierarchy — NOT revenue
               :RegSubDistrict
  taxonomy     :PropertyCategory  parent of the existing :PropertyType
               :PossessionType    symbolic | constructive | physical

:PlaceAlias is also applied as a SECOND label to existing :City and :Area
nodes by pipeline/resolve_places.py — those keep their own labels and edges,
so `LOCATED_IN_CITY` / `LOCATED_IN_AREA` never stop working.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from api.neo4j_client import run_query
from pipeline.embeddings import GEMINI_EMBED_DIM


# ── uniqueness ───────────────────────────────────────────────────────────────
# Each of these is an identity claim: two rows with the same key ARE the same
# thing. That is what makes repeat notices join instead of duplicating.
CONSTRAINTS = [
    # spine
    "CREATE CONSTRAINT lot_key_unique IF NOT EXISTS "
    "FOR (l:Lot) REQUIRE l.lot_key IS UNIQUE",
    "CREATE CONSTRAINT parcel_id_unique IF NOT EXISTS "
    "FOR (p:Parcel) REQUIRE p.parcel_id IS UNIQUE",

    # asset detail — (kind, value_norm) is the dedup key that builds :Parcel
    "CREATE CONSTRAINT identifier_kind_value_unique IF NOT EXISTS "
    "FOR (i:Identifier) REQUIRE (i.kind, i.value_norm) IS UNIQUE",
    "CREATE CONSTRAINT unit_name_unique IF NOT EXISTS "
    "FOR (u:Unit) REQUIRE u.name IS UNIQUE",
    "CREATE CONSTRAINT amenity_name_unique IF NOT EXISTS "
    "FOR (a:Amenity) REQUIRE a.name IS UNIQUE",

    # sale event — one zonal EMD account / one reused T&C block serves many
    # notices, so these are join points rather than per-notice scribbles.
    "CREATE CONSTRAINT emd_account_unique IF NOT EXISTS "
    "FOR (e:EMDAccount) REQUIRE (e.account_no, e.ifsc) IS UNIQUE",
    "CREATE CONSTRAINT terms_hash_unique IF NOT EXISTS "
    "FOR (t:TermsTemplate) REQUIRE t.hash IS UNIQUE",
    "CREATE CONSTRAINT platform_name_unique IF NOT EXISTS "
    "FOR (p:Platform) REQUIRE p.name IS UNIQUE",

    # enforcement
    "CREATE CONSTRAINT legal_framework_unique IF NOT EXISTS "
    "FOR (f:LegalFramework) REQUIRE f.name IS UNIQUE",
    "CREATE CONSTRAINT case_reference_unique IF NOT EXISTS "
    "FOR (c:CaseReference) REQUIRE c.ref IS UNIQUE",
    "CREATE CONSTRAINT loan_account_unique IF NOT EXISTS "
    "FOR (a:LoanAccount) REQUIRE a.account_no IS UNIQUE",

    # geography — one row per distinct spelling, resolved once and for all
    "CREATE CONSTRAINT place_alias_norm_unique IF NOT EXISTS "
    "FOR (a:PlaceAlias) REQUIRE a.name_norm IS UNIQUE",
    "CREATE CONSTRAINT reg_district_unique IF NOT EXISTS "
    "FOR (d:RegDistrict) REQUIRE d.name IS UNIQUE",

    # taxonomy
    "CREATE CONSTRAINT property_category_unique IF NOT EXISTS "
    "FOR (c:PropertyCategory) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT possession_type_unique IF NOT EXISTS "
    "FOR (x:PossessionType) REQUIRE x.name IS UNIQUE",
]

# ── query paths ──────────────────────────────────────────────────────────────
INDEXES = [
    # the dedup lookup: "what else was auctioned on this survey number"
    "CREATE INDEX identifier_value_idx IF NOT EXISTS "
    "FOR (i:Identifier) ON (i.value_norm)",

    # date-range filtering on sale events
    "CREATE INDEX auction_end_idx IF NOT EXISTS "
    "FOR (a:Auction) ON (a.auction_end_dt)",
    "CREATE INDEX auction_start_idx IF NOT EXISTS "
    "FOR (a:Auction) ON (a.auction_start_dt)",
    "CREATE INDEX auction_reserve_idx IF NOT EXISTS "
    "FOR (a:Auction) ON (a.reserve_price_num)",

    # size filtering + comparables
    "CREATE INDEX measurement_sqft_idx IF NOT EXISTS "
    "FOR (m:Measurement) ON (m.sqft_norm)",
    "CREATE INDEX measurement_unit_idx IF NOT EXISTS "
    "FOR (m:Measurement) ON (m.unit)",

    # "properties fronting a 30ft+ road" — the number that was buried in text
    "CREATE INDEX boundary_road_width_idx IF NOT EXISTS "
    "FOR (b:Boundary) ON (b.road_width_ft)",
    "CREATE INDEX lot_road_width_idx IF NOT EXISTS "
    "FOR (l:Lot) ON (l.road_width_ft)",

    # geography resolution scans these while matching
    "CREATE INDEX revenue_village_name_idx IF NOT EXISTS "
    "FOR (v:RevenueVillage) ON (v.name)",
    "CREATE INDEX taluk_name_idx IF NOT EXISTS "
    "FOR (t:Taluk) ON (t.name)",
    "CREATE INDEX district_name_idx IF NOT EXISTS "
    "FOR (d:District) ON (d.name)",
    "CREATE INDEX locality_norm_idx IF NOT EXISTS "
    "FOR (l:Locality) ON (l.name_norm)",
    "CREATE INDEX reg_sub_district_norm_idx IF NOT EXISTS "
    "FOR (s:RegSubDistrict) ON (s.name_norm)",

    # promotion bookkeeping: find what still needs promoting / verifying
    "CREATE INDEX lot_verified_idx IF NOT EXISTS "
    "FOR (l:Lot) ON (l.verified_at)",
    "CREATE INDEX parcel_last_seen_idx IF NOT EXISTS "
    "FOR (p:Parcel) ON (p.last_seen)",
]

# Lat/long is present on only ~5% of lots today, but a point index costs
# nothing on an empty property and saves a migration when it fills in.
POINT_INDEXES = [
    "CREATE POINT INDEX lot_location_idx IF NOT EXISTS "
    "FOR (l:Lot) ON (l.location)",
]

# Text search the graph has never had — the vector indexes exist, but there is
# no way to search a borrower by name or a description by keyword.
FULLTEXT_INDEXES = [
    "CREATE FULLTEXT INDEX party_name_ft IF NOT EXISTS "
    "FOR (n:Borrower) ON EACH [n.name]",
    "CREATE FULLTEXT INDEX lot_description_ft IF NOT EXISTS "
    "FOR (n:Lot) ON EACH [n.full_description]",
    "CREATE FULLTEXT INDEX identifier_raw_ft IF NOT EXISTS "
    "FOR (n:Identifier) ON EACH [n.value_raw]",
]

# Lot reuses the same embedding model as :AuctionProperty.description_embedding
# so lot-level and listing-level vectors stay comparable — hence the shared
# GEMINI_EMBED_DIM rather than a local constant that could drift.
VECTOR_INDEXES = [
    "CREATE VECTOR INDEX lot_description_embedding IF NOT EXISTS "
    "FOR (l:Lot) ON (l.description_embedding) "
    "OPTIONS {indexConfig: {"
    f"`vector.dimensions`: {GEMINI_EMBED_DIM}, "
    "`vector.similarity_function`: 'cosine'}}",
]

ALL_STATEMENTS = (
    CONSTRAINTS + INDEXES + POINT_INDEXES + FULLTEXT_INDEXES + VECTOR_INDEXES
)


def run(dry_run: bool = False) -> int:
    ok = failed = 0
    for stmt in ALL_STATEMENTS:
        short = stmt.split(" IF NOT EXISTS")[0]
        if dry_run:
            print(f"  [dry-run] {short}")
            ok += 1
            continue
        try:
            run_query(stmt)
            print(f"  ✓ {short}")
            ok += 1
        except Exception as exc:  # keep going: one unsupported index (e.g. an
            # older Neo4j without POINT/VECTOR support) shouldn't block the rest
            failed += 1
            print(f"  ✗ {short}\n      {exc}")
    print(f"\ngraph schema: {ok} applied, {failed} failed "
          f"({len(ALL_STATEMENTS)} statements)")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the statements without executing them")
    args = ap.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
