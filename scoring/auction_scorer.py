"""
scoring/auction_scorer.py
-------------------------
Ten-dimensional investment scoring for bank auction properties.
Adapts the career-ops A-F block scoring pattern.

Each dimension returns a 0-100 score. The composite is the weighted average
driven by SCORING_WEIGHTS in pipeline/config.py.

Dimension implementations start as heuristics over existing Neo4j data;
they can be refined as more signals (rental yields, time-series prices) arrive.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime

from pipeline.config import SCORING_WEIGHTS, DECISION_THRESHOLDS
from api.neo4j_client import run_query


@dataclass
class DimensionScore:
    name: str
    score: float          # 0-100
    weight: float
    rationale: str


@dataclass
class AuctionScore:
    auction_id: str
    composite_score: float   # 0-100
    grade: str               # A+..F
    dimensions: list[DimensionScore]

    def to_dict(self) -> dict:
        return {
            "auction_id": self.auction_id,
            "composite_score": self.composite_score,
            "grade": self.grade,
            "dimensions": [asdict(d) for d in self.dimensions],
        }


def _grade(score: float) -> str:
    if score >= 90: return "A+"
    if score >= DECISION_THRESHOLDS["strong_buy"]: return "A"
    if score >= DECISION_THRESHOLDS["worth_pursuing"]: return "B"
    if score >= DECISION_THRESHOLDS["selective"]: return "C"
    if score >= 40: return "D"
    return "F"


def _fetch_auction(auction_id: str) -> dict | None:
    rows = run_query(
        """
        MATCH (a:AuctionProperty {auction_id: $id})
        OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(c:City)
        OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(area:Area)
        OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(b:Bank)
        OPTIONAL MATCH (a)-[:HAS_ASSET_CATEGORY]->(:AssetCategory)-[:HAS_TYPE]->(pt:PropertyType)
        RETURN a, c.name AS city, area.name AS area, b.name AS bank, pt.name AS property_type
        """,
        {"id": auction_id},
    )
    if not rows:
        return None
    r = rows[0]
    a = dict(r["a"])
    a["city"] = r.get("city")
    a["area"] = r.get("area")
    a["bank"] = r.get("bank")
    a["property_type"] = r.get("property_type")
    return a


# ── Dimension scorers ───────────────────────────────────────────────────────

def score_price_attractiveness(a: dict) -> DimensionScore:
    price = a.get("reserve_price_num") or 0.0
    area = a.get("area")
    if not price or not area:
        return DimensionScore("price_attractiveness", 50.0, SCORING_WEIGHTS["price_attractiveness"],
                              "insufficient data, default 50")
    rows = run_query(
        """
        MATCH (:Area {name: $area})<-[:LOCATED_IN_AREA]-(o:AuctionProperty)
        RETURN avg(o.reserve_price_num) AS avg_price
        """, {"area": area})
    avg = (rows[0] or {}).get("avg_price")
    if not avg:
        return DimensionScore("price_attractiveness", 50.0, SCORING_WEIGHTS["price_attractiveness"], "no comparables")
    ratio = price / avg
    score = max(0.0, min(100.0, 100.0 - (ratio - 1.0) * 100.0))
    return DimensionScore("price_attractiveness", score, SCORING_WEIGHTS["price_attractiveness"],
                          f"reserve {price:.0f} vs area avg {avg:.0f} (ratio {ratio:.2f})")


def score_location_quality(a: dict) -> DimensionScore:
    city = a.get("city")
    if not city:
        return DimensionScore("location_quality", 40.0, SCORING_WEIGHTS["location_quality"], "unknown city")
    rows = run_query(
        """
        MATCH (:City {name: $city})<-[:LOCATED_IN_CITY]-(o:AuctionProperty)
        RETURN count(o) AS density
        """, {"city": city})
    density = (rows[0] or {}).get("density") or 0
    score = max(30.0, min(95.0, 40.0 + density * 0.5))
    return DimensionScore("location_quality", score, SCORING_WEIGHTS["location_quality"],
                          f"city {city} has {density} listings")


def score_legal_clarity(a: dict) -> DimensionScore:
    completeness = a.get("description_completeness") or 0.5
    field_conflicts = a.get("field_conflicts") or []
    conflict_penalty = min(40.0, 8.0 * len(field_conflicts))
    score = max(20.0, completeness * 100 - conflict_penalty)
    return DimensionScore("legal_clarity", score, SCORING_WEIGHTS["legal_clarity"],
                          f"completeness={completeness:.2f}, conflicts={len(field_conflicts)}")


def score_bank_reliability(a: dict) -> DimensionScore:
    bank = a.get("bank")
    if not bank:
        return DimensionScore("bank_reliability", 50.0, SCORING_WEIGHTS["bank_reliability"], "unknown bank")
    rows = run_query(
        """
        MATCH (:Bank {name: $bank})<-[:CONDUCTED_BY]-(o:AuctionProperty)
        RETURN count(o) AS total
        """, {"bank": bank})
    total = (rows[0] or {}).get("total") or 0
    score = max(40.0, min(90.0, 40.0 + total * 0.3))
    return DimensionScore("bank_reliability", score, SCORING_WEIGHTS["bank_reliability"],
                          f"{bank} has {total} auctions in graph")


def score_property_condition(a: dict) -> DimensionScore:
    pt = a.get("property_type") or ""
    desc_len = len(a.get("description") or "")
    type_bonus = {"Residential": 20, "Commercial": 15, "Plot": 10, "Industrial": 10}.get(pt, 0)
    desc_score = min(60.0, desc_len / 50.0)
    score = 40.0 + type_bonus + desc_score * 0.5
    return DimensionScore("property_condition", min(95.0, score), SCORING_WEIGHTS["property_condition"],
                          f"type={pt}, desc_len={desc_len}")


def score_timeline_urgency(a: dict) -> DimensionScore:
    deadline = a.get("application_deadline_dt")
    if not deadline:
        return DimensionScore("timeline_urgency", 50.0, SCORING_WEIGHTS["timeline_urgency"], "no deadline")
    try:
        dt = datetime.fromisoformat(str(deadline).replace("Z", ""))
    except Exception:
        return DimensionScore("timeline_urgency", 50.0, SCORING_WEIGHTS["timeline_urgency"], "bad date")
    days = (dt - datetime.now()).days
    if days < 0:
        return DimensionScore("timeline_urgency", 0.0, SCORING_WEIGHTS["timeline_urgency"], "deadline passed")
    if days < 3:    score = 60.0
    elif days < 14: score = 90.0
    elif days < 30: score = 80.0
    else:           score = 60.0
    return DimensionScore("timeline_urgency", score, SCORING_WEIGHTS["timeline_urgency"],
                          f"{days} days until deadline")


def score_due_diligence_ease(a: dict) -> DimensionScore:
    completeness = a.get("description_completeness") or 0.3
    score = completeness * 100.0
    return DimensionScore("due_diligence_ease", score, SCORING_WEIGHTS["due_diligence_ease"],
                          f"completeness={completeness:.2f}")


def score_area_price_trend(a: dict) -> DimensionScore:
    # Placeholder — requires historical snapshots (Phase 6).
    return DimensionScore("area_price_trend", 60.0, SCORING_WEIGHTS["area_price_trend"],
                          "trend analysis TBD (needs historical data)")


def score_competition_risk(a: dict) -> DimensionScore:
    area = a.get("area")
    if not area:
        return DimensionScore("competition_risk", 50.0, SCORING_WEIGHTS["competition_risk"], "unknown area")
    rows = run_query(
        """
        MATCH (:Area {name: $area})<-[:LOCATED_IN_AREA]-(o:AuctionProperty)
        WHERE o.auction_start_dt IS NOT NULL
        RETURN count(o) AS concurrent
        """, {"area": area})
    concurrent = (rows[0] or {}).get("concurrent") or 0
    score = max(20.0, 100.0 - concurrent * 2.0)
    return DimensionScore("competition_risk", score, SCORING_WEIGHTS["competition_risk"],
                          f"{concurrent} other auctions in {area}")


def score_yield_potential(a: dict) -> DimensionScore:
    price = a.get("reserve_price_num") or 0
    emd = a.get("emd_num") or 0
    if price <= 0:
        return DimensionScore("yield_potential", 50.0, SCORING_WEIGHTS["yield_potential"], "no price")
    emd_ratio = emd / price if price else 0
    score = 50.0 + (0.10 - abs(emd_ratio - 0.10)) * 400.0
    return DimensionScore("yield_potential", max(20.0, min(90.0, score)),
                          SCORING_WEIGHTS["yield_potential"],
                          f"emd/price={emd_ratio:.3f}")


# ── Composite ───────────────────────────────────────────────────────────────

_SCORERS = [
    score_price_attractiveness, score_location_quality, score_legal_clarity,
    score_bank_reliability, score_property_condition, score_timeline_urgency,
    score_due_diligence_ease, score_area_price_trend, score_competition_risk,
    score_yield_potential,
]


def score_auction(auction_id: str) -> AuctionScore | None:
    a = _fetch_auction(auction_id)
    if not a:
        return None
    dims = [fn(a) for fn in _SCORERS]
    composite = sum(d.score * d.weight for d in dims)
    return AuctionScore(auction_id=auction_id, composite_score=round(composite, 2),
                        grade=_grade(composite), dimensions=dims)


def score_and_persist(auction_id: str) -> AuctionScore | None:
    """Score an auction and upsert an InvestmentTracker node in Neo4j."""
    result = score_auction(auction_id)
    if not result:
        return None
    run_query(
        """
        MERGE (t:InvestmentTracker {auction_id: $id})
        ON CREATE SET t.created_at = datetime(), t.state = 'SCORED'
        SET t.composite_score = $score,
            t.grade = $grade,
            t.dimension_scores = $dims,
            t.updated_at = datetime()
        WITH t
        MATCH (a:AuctionProperty {auction_id: $id})
        MERGE (a)-[:TRACKED_BY]->(t)
        """,
        {
            "id": auction_id,
            "score": result.composite_score,
            "grade": result.grade,
            "dims": json.dumps([asdict(d) for d in result.dimensions]),
        },
    )
    return result
