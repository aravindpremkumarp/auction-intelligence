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
the API.

Decision kinds and their payloads::

    bank-merge        {"a": label, "b": label}      approve joins the groups
    district-conflict {"raw": str, "taluk": str}    approve = taluk was right
    village-alias     {"raw": str, "taluk": str,    approve maps raw -> target
                       "target": str}                inside that taluk
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

from pipeline.entity_resolution import branch_key, canonical_label, org_key
from pipeline.lot_resolution import lot_match_key
from pipeline.place_resolution import normalize_place

APPROVED = "approved"
REJECTED = "rejected"

KINDS = ("bank-merge", "branch-merge", "district-conflict",
         "village-alias", "village-skip", "lot-match")


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


def skipped_villages(decisions: list[dict]) -> set[str]:
    """Normalized village strings a human ruled out of the revenue system."""
    return {normalize_place((d.get("payload") or {}).get("raw") or "")
            for d in _decided(decisions, "village-skip").values()
            if d.get("verdict") == APPROVED}


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
