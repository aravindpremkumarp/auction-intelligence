"""
pipeline/resolution_review.py
-----------------------------
Make a human's resolution decisions permanent.

Both resolvers stop where a rule cannot decide: bank resolution queues
lookalike pairs, place resolution queues district conflicts and villages it
could not match. A human's verdict on those must be *remembered, not just
applied* — a decision that only edits today's data is resurrected as an open
question by the next resolution run, and the queue never shrinks.

So every verdict becomes a small stored fact — a ``(:ResolutionDecision)``
node keyed deterministically from the strings it is about — and the resolvers
consult the stored facts before doing anything else:

* an **approved** bank merge is applied like an alias, every run, forever;
* a **rejected** pair is never proposed again;
* a village alias a human supplies enters the lookup ahead of the gazetteer
  rules, scoped to its taluk;
* a **confirmed** district conflict (the taluk was right) stops appearing.

This module is pure — keys and application logic only, exercised by tests
without a database. Reading and writing the nodes belongs to the scripts and
the API. (The one file it reads is the OSM spelling lookup, applied after
every human verdict by :func:`settle_village`.)

Decision kinds and their payloads::

    bank-merge        {"a": label, "b": label}      approve joins the groups
    district-conflict {"raw": str, "taluk": str}    approve = taluk was right
    village-alias     {"raw": str, "taluk": str,    approve maps raw -> target
                       "target": str,                inside that taluk, or inside
                       "target_taluk": str           target_taluk when given
                       (optional)}
    village-skip      {"raw": str}                  approve = not a revenue
                                                    village (urban locality);
                                                    drop it from the queue
    lot-match         {"auction_id": str,            approve = this listing IS
                       "lot_key": str,                this lot on its notice;
                       "note": str (optional)}        reject + lot_key
                                                       NONE_LOT_KEY = none of
                                                       the candidates fit
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.entity_resolution import branch_key, canonical_label, org_key
from pipeline.lot_resolution import lot_match_key
from pipeline.place_resolution import Gazetteer, normalize_place

APPROVED = "approved"
REJECTED = "rejected"

KINDS = ("bank-merge", "branch-merge", "district-conflict",
         "village-alias", "village-skip", "lot-match", "portal-match")


def bank_pair_key(a: str, b: str) -> str:
    """Stable key for a lookalike lender pair, order-independent.

    Built from :func:`org_key` rather than the display labels, so the same
    pair keeps its key when a later run picks a different canonical spelling.
    """
    return "bank-merge:" + "|".join(sorted((org_key(a), org_key(b))))


def branch_pair_key(bank: str, a: str, b: str) -> str:
    """Key for a lookalike branch pair, scoped to its bank.

    The bank is part of the key because branch identity only exists within a
    bank — a verdict on Canara's two "ARM Trichy" spellings must never touch
    another bank's Trichy office.
    """
    return (f"branch-merge:{org_key(bank)}:"
            + "|".join(sorted((branch_key(a), branch_key(b)))))


def district_conflict_key(raw_district: str, taluk: str) -> str:
    """Key for one conflict *pattern* — every notice writing this district
    over this taluk is a single decision, which is what makes 27 notices of
    ``Kanchipuram + Pallavaram`` one click."""
    return ("district-conflict:"
            f"{normalize_place(raw_district)}|{normalize_place(taluk)}")


def village_alias_key(raw: str, taluk: str) -> str:
    """Key for a village spelling inside one taluk. Scoped because the same
    string can be a fine alias in one taluk and wrong in another."""
    return f"village-alias:{normalize_place(raw)}@{normalize_place(taluk)}"


def village_skip_key(raw: str) -> str:
    """Key for "this string is not a revenue village" — unscoped, because an
    urban locality like Selaiyur is not a revenue village anywhere."""
    return f"village-skip:{normalize_place(raw)}"


def price_check_key(auction_id: str) -> str:
    """Key for "I have looked at this listing's price disagreement".

    Scoped to the listing alone, not to the two prices: a reviewer settles
    the LISTING, and a re-extraction that shifts the notice price by a rupee
    must not reopen a question they already answered.
    """
    return f"price-check:{auction_id}"


def area_check_key(auction_id: str) -> str:
    """Key for "I have looked at this listing's area disagreement".

    Scoped to the listing for the same reason `price_check_key` is: a
    reviewer settles the LISTING, and a re-extraction that nudges the lot's
    measurement must not reopen a question they already answered.
    """
    return f"area-check:{auction_id}"


def portal_match_key(subject_id: str, other_source: str) -> str:
    """Key for "a person decided which of our listings this portal listing is".

    One decision per subject per other source — the review queue shows one
    case per pair, and re-deciding that case replaces only its own decision."""
    return f"portal-match:{subject_id}:{other_source}"


def decision_key(kind: str, payload: dict) -> str:
    """The key for a decision, derived from its kind and payload — the API
    never accepts a caller-supplied key, so a decision can only ever land on
    the strings it names."""
    if kind == "bank-merge":
        return bank_pair_key(payload["a"], payload["b"])
    if kind == "branch-merge":
        return branch_pair_key(payload["bank"], payload["a"], payload["b"])
    if kind == "district-conflict":
        return district_conflict_key(payload["raw"], payload["taluk"])
    if kind == "village-alias":
        return village_alias_key(payload["raw"], payload["taluk"])
    if kind == "village-skip":
        return village_skip_key(payload["raw"])
    if kind == "lot-match":
        return lot_match_key(payload["auction_id"], payload["lot_key"])
    if kind == "price-check":
        return price_check_key(payload["auction_id"])
    if kind == "area-check":
        return area_check_key(payload["auction_id"])
    if kind == "portal-match":
        return portal_match_key(payload["subject_id"], payload["other_source"])
    raise ValueError(f"unknown decision kind: {kind!r}")


def _decided(decisions: list[dict], kind: str) -> dict[str, dict]:
    return {d["key"]: d for d in decisions
            if d.get("kind") == kind and d.get("key")}


def apply_bank_merges(res: dict, decisions: list[dict]) -> dict:
    """Fold approved bank-merge decisions into a fresh ``resolve()`` result.

    Approved pairs join their two groups: variants union, counts add, and the
    canonical label is re-chosen from the combined variants — the human
    approved *that the two are one lender*, not which spelling wins, so the
    spelling stays a data question. Chains resolve transitively: A=B and B=C
    put all three in one group.

    Returns the same shape ``resolve()`` produced, with ``merged_by_decision``
    on each group counting spellings a human's verdict brought in.
    """
    approved = [d for d in _decided(decisions, "bank-merge").values()
                if d.get("verdict") == APPROVED]
    pairs = [(org_key(d["payload"]["a"]), org_key(d["payload"]["b"]))
             for d in approved]
    return _apply_merge_pairs(res, pairs)


def apply_branch_merges(res: dict, decisions: list[dict], *,
                        bank: str) -> dict:
    """Fold approved branch-merge decisions for ``bank`` into that bank's
    ``resolve(..., kind="branch")`` result. Same mechanics as the lender
    version; the bank filter is what keeps one bank's verdicts from ever
    touching another's identically named office."""
    scope = org_key(bank)
    approved = [d for d in _decided(decisions, "branch-merge").values()
                if d.get("verdict") == APPROVED
                and org_key((d.get("payload") or {}).get("bank") or "") == scope]
    pairs = [(branch_key(d["payload"]["a"]), branch_key(d["payload"]["b"]))
             for d in approved]
    return _apply_merge_pairs(res, pairs)


def _apply_merge_pairs(res: dict,
                       pairs: list[tuple[str, str]]) -> dict:
    if not pairs:
        for g in res["groups"]:
            g.setdefault("merged_by_decision", 0)
        return res

    # Union-find over group keys, seeded by the approved pairs.
    parent: dict[str, str] = {}

    def find(k: str) -> str:
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for a, b in pairs:
        parent[find(a)] = find(b)

    by_root: dict[str, list[dict]] = {}
    for g in res["groups"]:
        by_root.setdefault(find(g["key"]), []).append(g)

    groups: list[dict] = []
    by_value: dict[str, str] = {}
    for members in by_root.values():
        variants: dict[str, int] = {}
        for g in members:
            for name, count in g["variants"]:
                variants[name] = variants.get(name, 0) + int(count)
        label = canonical_label(variants)
        base = max(members, key=lambda g: g["count"])
        joined = sum(g["merged"] for g in members)
        groups.append({
            "key": base["key"],
            "canonical": label,
            "variants": sorted(variants.items(), key=lambda kv: -kv[1]),
            "count": sum(variants.values()),
            "merged": joined + (len(members) - 1),
            "merged_by_decision": len(members) - 1,
        })
        for name in variants:
            by_value[name] = label
    groups.sort(key=lambda g: -g["count"])
    return {"groups": groups, "by_value": by_value}


def filter_proposals(proposals: list[dict], decisions: list[dict]) -> list[dict]:
    """Drop lookalike pairs a human has already ruled on, either way.

    Approved pairs are gone because they are now merged; rejected pairs are
    gone because asking twice is how a review queue teaches people to stop
    reading it.
    """
    ruled = set(_decided(decisions, "bank-merge"))
    return [p for p in proposals
            if bank_pair_key(p["a"], p["b"]) not in ruled]


def filter_branch_proposals(proposals: list[dict],
                            decisions: list[dict]) -> list[dict]:
    """Same idea for branch pairs; each proposal carries its ``bank``."""
    ruled = set(_decided(decisions, "branch-merge"))
    return [p for p in proposals
            if branch_pair_key(p["bank"], p["a"], p["b"]) not in ruled]


def village_aliases(decisions: list[dict]) -> dict[str, str]:
    """``{alias key -> official village name}`` from approved alias verdicts.

    Keyed exactly as :func:`village_alias_key` builds them, so the resolver
    looks up ``(raw, taluk)`` and gets the official name a human vouched for.
    """
    return {d["key"]: d["payload"]["target"]
            for d in _decided(decisions, "village-alias").values()
            if d.get("verdict") == APPROVED and (d.get("payload") or {}).get("target")}


def village_alias_taluks(decisions: list[dict]) -> dict[str, str]:
    """``{alias key -> the taluk its target sits in}``, for the approved
    aliases whose target is not in the notice's own taluk.

    Notices still name the taluk a village sat in before the 2019 splits
    ("Varadharajapuram, Sriperumbudur Taluk" — the register now holds it in
    Kundrathur). The verdict stays keyed by the notice's spelling and taluk;
    this says where the answer lives."""
    out = {}
    for d in _decided(decisions, "village-alias").values():
        payload = d.get("payload") or {}
        where = payload.get("target_taluk")
        if d.get("verdict") == APPROVED and where and where != payload.get("taluk"):
            out[d["key"]] = where
    return out


def skipped_villages(decisions: list[dict]) -> set[str]:
    """Normalized village strings a human ruled out of the revenue system."""
    return {normalize_place((d.get("payload") or {}).get("raw") or "")
            for d in _decided(decisions, "village-skip").values()
            if d.get("verdict") == APPROVED}


#: Village spellings confirmed against OpenStreetMap by
#: scripts/harvest_osm_village_aliases — a lookup file, not human decisions,
#: so they carry their own source and never outrank a person's verdict.
OSM_ALIASES = Path(__file__).resolve().parent / "lookups" / "village_aliases_osm.json"


def load_osm_aliases(path: Path = OSM_ALIASES) -> dict[str, dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def apply_osm_alias(gaz: Gazetteer, res: dict, village: str | None,
                    osm: dict[str, dict]) -> dict:
    """``res`` with an OSM-confirmed answer for a still-unmatched village.

    Only ``unmatched`` is touched — every other status either resolved, was
    settled by a person, or has no taluk to scope the lookup. An alias applies
    only into a village the gazetteer really holds under that taluk (so a stale
    entry cannot invent a place); an ``osm-urban`` entry places nothing and
    only marks the name ``not-a-revenue-village``."""
    if not (village and res.get("taluk") and not res.get("village")
            and res.get("village_status") == "unmatched"):
        return res
    hit = osm.get(village_alias_key(village, res["taluk"]))
    if not hit:
        return res
    if hit.get("rule") == "osm-urban":
        return {**res, "village_status": "not-a-revenue-village",
                "village_source": "osm-urban"}
    official = gaz.village(hit.get("target") or "", res["taluk"], fuzzy=False)
    if not official:
        return res
    return {**res, "village": official, "village_status": "resolved",
            "village_source": hit["rule"]}


def settle_village(gaz: Gazetteer, res: dict, village: str | None, *,
                   aliases: dict[str, str], skips: set[str],
                   osm: dict[str, dict],
                   alias_taluks: dict[str, str] | None = None) -> dict:
    """``res`` with every decided spelling applied, strongest first: a human
    alias, a human skip, then a spelling OpenStreetMap confirms.

    Shared by both place writers — listings (scripts/resolve_places) and lots
    (pipeline/promote_extractions) — so one verdict settles a village the same
    way on both ends of a listing-lot link. A human alias applies only into a
    village the gazetteer holds under its taluk (the notice's, or the one the
    verdict names — :func:`village_alias_taluks`), so a typo in a decision
    cannot invent a place; an answer in another taluk moves the taluk and
    district with it."""
    if village and not res.get("village"):
        taluk = res.get("taluk")
        key = village_alias_key(village, taluk) if taluk else None
        target = aliases.get(key) if key else None
        where = (alias_taluks or {}).get(key) or taluk
        official = gaz.village(target, where, fuzzy=False) if target else None
        if official:
            out = {**res, "village": official, "village_status": "resolved",
                   "village_source": "human-alias"}
            if where != taluk:
                hit = gaz.taluk(where)
                out["taluk"] = hit[0] if hit else where
                out["district"] = hit[1] if hit else res.get("district")
            return out
        if normalize_place(village) in skips:
            # Ruled "not a revenue village" (an urban locality) — true in
            # every taluk, so it needs no parent to apply.
            return {**res, "village_status": "not-a-revenue-village"}
    return apply_osm_alias(gaz, res, village, osm)


def settled_conflicts(decisions: list[dict]) -> set[str]:
    """Keys of district-conflict patterns a human has confirmed or overruled —
    either way the pattern leaves the queue."""
    return set(_decided(decisions, "district-conflict"))


#: Sentinel `lot_key` for "a human reviewed this listing and none of the
#: candidate lots fit" — a real decision (with its own stable key, via
#: `lot_match_key`), just naming no lot rather than one. Distinct from
#: leaving a listing untouched, which the queue keeps re-offering.
NONE_LOT_KEY = "__none__"


def decided_lot_matches(decisions: list[dict]) -> set[str]:
    """`auction_id`s a human (or the resolver) has already settled — approved
    (a lot was picked) or rejected with `NONE_LOT_KEY` (none fit).

    An approval alone doesn't move `resolved_lot_key` on the graph — that
    still needs the resolver's apply step — but the DECISION is what
    settles the question, same distinction `ruled_pairs`/`settled_conflicts`/
    `skipped_villages` already draw for their kinds: the queue stops asking
    the moment a human answers, not the moment the answer is applied.
    """
    out: set[str] = set()
    for d in _decided(decisions, "lot-match").values():
        payload = d.get("payload") or {}
        aid = payload.get("auction_id")
        if not aid:
            continue
        if d.get("verdict") == APPROVED and payload.get("lot_key"):
            out.add(aid)
        elif (d.get("verdict") == REJECTED
              and payload.get("lot_key") == NONE_LOT_KEY):
            out.add(aid)
    return out


def decided_price_checks(decisions: list[dict]) -> set[str]:
    """`auction_id`s whose price disagreement a human has already settled.

    Both verdicts settle it and both drop the row: `approved` means "yes, one
    of these prices is wrong", `rejected` means "false alarm". The queue asks
    a reviewer to LOOK, so either answer means they have — what happens to
    the number afterwards is a separate job.
    """
    out: set[str] = set()
    for d in _decided(decisions, "price-check").values():
        aid = (d.get("payload") or {}).get("auction_id")
        if aid and d.get("verdict") in (APPROVED, REJECTED):
            out.add(aid)
    return out


def decided_area_checks(decisions: list[dict]) -> set[str]:
    """`auction_id`s whose area disagreement a human has already settled.

    Same contract as `decided_price_checks`: both verdicts settle the row,
    because the queue asks a reviewer to LOOK. `approved` means one of the
    two figures is wrong, `rejected` means the two describe the same area
    after all (a unit the parser read differently, most often).
    """
    out: set[str] = set()
    for d in _decided(decisions, "area-check").values():
        aid = (d.get("payload") or {}).get("auction_id")
        if aid and d.get("verdict") in (APPROVED, REJECTED):
            out.add(aid)
    return out


def portal_decisions(decisions: list[dict]) -> dict[tuple[str, str], dict]:
    """Every portal-match verdict, keyed by ``(subject id, other source)``, in
    the shape ``sources.match.match_listings(decisions=...)`` reads:
    ``{"verdict", "linked_ids": set, "rejected_ids": set, "snapshot": dict}``.
    One subject can have a separate open (or settled) case per other source,
    so the key must carry both."""
    out: dict[tuple[str, str], dict] = {}
    for d in _decided(decisions, "portal-match").values():
        payload = d.get("payload") or {}
        sid = payload.get("subject_id")
        other_source = payload.get("other_source")
        if not sid or not other_source or d.get("verdict") not in (APPROVED, REJECTED):
            continue
        out[(sid, other_source)] = {"verdict": d["verdict"],
                                    "linked_ids": set(payload.get("linked_ids") or ()),
                                    "rejected_ids": set(payload.get("rejected_ids") or ()),
                                    "snapshot": payload.get("snapshot") or {}}
    return out
