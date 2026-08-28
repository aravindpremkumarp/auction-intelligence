"""Score lender-name matchers against the hand-labelled set in gold.py.

    python experiments/bank_entity_resolution/compare.py

Offline: reads the corpus snapshot in banks.csv, touches no database. Splink is
optional — without it the three rule-based methods still run.

Scored pairwise over distinct name strings: for every pair of names, did the
method put them in one cluster, and does the gold set agree? Once per name pair,
not once per notice, so 246 Canara Bank notices cannot drown out the
single-notice OCR damage that is the entire question.

Method D is the production rule (pipeline.entity_resolution.ocr_variant_of),
imported rather than reimplemented so this file measures the shipped code.
Method E is a rejected idea kept for the record: it is why the second tier
compares tokens instead of whole names.
"""
from __future__ import annotations

import collections
import csv
import itertools
import math
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))
sys.path.insert(0, str(_HERE))

from pipeline.entity_resolution import (  # noqa: E402
    _LEADING_THE, _LEGAL_SUFFIX, normalize, ocr_variant_of, org_key)
from gold import JUDGMENT, NOT_A_LENDER, gold_clusters  # noqa: E402

CSV_PATH = _HERE / "banks.csv"
HELD_OUT = {n for pair in JUDGMENT for n in pair}
_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(name: str) -> list[str]:
    s = _LEGAL_SUFFIX.sub(" ", _LEADING_THE.sub("", normalize(name)))
    return sorted(set(_TOKEN.findall(s)))


# --------------------------------------------------------------- scoring ----
def pairwise(labels, names):
    return {frozenset((a, b)) for a, b in itertools.combinations(names, 2)
            if labels[a] == labels[b]}


def score(pred, truth):
    tp, fp, fn = len(pred & truth), len(pred - truth), len(truth - pred)
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec,
            "f1": 2 * prec * rec / (prec + rec) if prec + rec else 0.0}


def components(names, edges):
    """Connected components — the same clustering step promote_extractions
    phase C uses to build :Parcel, and the step method E blows up."""
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    roots = {}
    return {n: roots.setdefault(find(n), len(roots)) for n in names}


def equality_edges(names):
    """Tier one on its own: normalized token-set equality."""
    by_key = collections.defaultdict(list)
    for n in names:
        by_key[org_key(n)].append(n)
    return [(m[0], o) for m in by_key.values() for o in m[1:]]


# ---------------------------------------------------------------- methods ----
def method_a(names):
    """Token-set equality alone — the rule before this experiment."""
    return components(names, equality_edges(names))


def method_b(names, min_score=88.0):
    """Equality plus every review-queue proposal auto-applied. Not what
    production does; the obvious "just lower the bar" alternative."""
    from rapidfuzz import fuzz
    edges = equality_edges(names)
    edges += [(a, b) for a, b in itertools.combinations(names, 2)
              if fuzz.token_sort_ratio(org_key(a), org_key(b)) >= min_score]
    return components(names, edges)


def method_d(names):
    """Equality plus the shipped one-misread-token tier."""
    edges = equality_edges(names)
    edges += [(a, b) for a, b in itertools.combinations(names, 2)
              if ocr_variant_of(a, b)]
    return components(names, edges)


_HONORIFIC = re.compile(r"^m\s*/?\s*s\b")


def _whole_name_close(a, b, max_rel=0.20):
    from rapidfuzz.distance import DamerauLevenshtein
    na = _HONORIFIC.sub("", normalize(a)).replace(" ", "")
    nb = _HONORIFIC.sub("", normalize(b)).replace(" ", "")
    if not na or not nb:
        return False
    return (DamerauLevenshtein.distance(na, nb)
            / max(len(na), len(nb))) <= max_rel


def method_e(names):
    """REJECTED. Adds whole-name similarity with the spaces removed, to reach
    "Tamil Nadu" / "Tamilnadu". It also puts "AU Small Finance Bank" within
    reach of every other small finance bank, and connected components then
    chains them into one lender: two more true pairs, 215 wrong ones."""
    edges = equality_edges(names)
    edges += [(a, b) for a, b in itertools.combinations(names, 2)
              if ocr_variant_of(a, b) or _whole_name_close(a, b)]
    return components(names, edges)


def method_c(names, threshold):
    """Fellegi-Sunter via Splink, trained unsupervised on the same names."""
    import pandas as pd
    from splink import DuckDBAPI, Linker, SettingsCreator, block_on
    import splink.comparison_library as cl

    df = pd.DataFrame([{
        "unique_id": i, "bank_name": n,
        "name_norm": normalize(n),
        "name_nospace": normalize(n).replace(" ", ""),
        "token_key": org_key(n),
        "token_count": len(tokens(n)),
        "first_token": (tokens(n) or [""])[0],
    } for i, n in enumerate(names)])

    settings = SettingsCreator(
        link_type="dedupe_only",
        # Cartesian. 192 names is 18k pairs, free in DuckDB, and no blocking
        # rule survives the case that matters: damaged leading characters
        # ("IICI"/"ICICI", "Kanur"/"Karur") defeat every prefix and token block.
        blocking_rules_to_generate_predictions=["1=1"],
        comparisons=[
            cl.JaroWinklerAtThresholds("name_norm", [0.96, 0.92, 0.86]),
            cl.LevenshteinAtThresholds("name_nospace", [2, 5]),
            cl.ExactMatch("token_count"),
            cl.ExactMatch("token_key").configure(
                term_frequency_adjustments=True),
        ],
    )
    linker = Linker(df, settings, db_api=DuckDBAPI())
    linker.training.estimate_probability_two_random_records_match(
        [block_on("token_key")], recall=0.8)
    linker.training.estimate_u_using_random_sampling(max_pairs=1e6)
    # One EM pass, blocked on first_token so every estimated comparison varies
    # inside the block. An earlier run also trained a pass blocked on
    # token_count, which holds token_count constant and leaves it
    # unidentifiable; EM returned a -147 weight for "token counts differ".
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on("first_token"))

    preds = linker.inference.predict(threshold_match_probability=0.01)
    cdf = linker.clustering.cluster_pairwise_predictions_at_threshold(
        preds, threshold_match_probability=threshold).as_pandas_dataframe()
    by_cluster = collections.defaultdict(list)
    for _, row in cdf.iterrows():
        by_cluster[row["cluster_id"]].append(row["bank_name"])
    edges = [(m[0], o) for m in by_cluster.values() for o in sorted(m)[1:]]
    return components(names, edges), linker


def main() -> int:
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    counts = collections.Counter(r["bank_name"] for r in rows)
    truth_labels = gold_clusters(sorted(counts))
    names = [n for n in sorted(counts) if n not in HELD_OUT]
    truth = pairwise(truth_labels, names)

    print(f"{len(rows)} notices, {len(names)} names scored, "
          f"{len(set(truth_labels[n] for n in names))} gold lenders, "
          f"{len(truth)} true pairs of {len(names) * (len(names) - 1) // 2}\n")

    results = {
        "A. token-set equality only": method_a(names),
        "B. equality + fuzzy>=88": method_b(names),
        "D. equality + misread token": method_d(names),
        "E. D + space-insensitive (rejected)": method_e(names),
    }
    linker = None
    try:
        for thr in (0.99, 0.9, 0.7):
            results[f"C. splink @ {thr}"], linker = method_c(names, thr)
    except ImportError:
        print("(splink not installed — skipping method C)\n")

    print(f"{'method':<38} {'prec':>6} {'rec':>6} {'F1':>6} "
          f"{'missed':>7} {'wrong':>6}")
    print("-" * 74)
    for label, lab in results.items():
        s = score(pairwise(lab, names), truth)
        print(f"{label:<38} {s['precision']:6.3f} {s['recall']:6.3f} "
              f"{s['f1']:6.3f} {s['fn']:7d} {s['fp']:6d}")

    pred = pairwise(results["D. equality + misread token"], names)
    print("\n--- what the shipped rule still leaves for a human ---")
    for p in sorted(truth - pred, key=lambda p: sorted(p)):
        a, b = sorted(p)
        print(f"  {a}  ||  {b}")
    for p in sorted(pred - truth, key=lambda p: sorted(p)):
        a, b = sorted(p)
        flag = "   <-- NOT A LENDER" if (
            a in NOT_A_LENDER or b in NOT_A_LENDER) else ""
        print(f"  WRONG  {a}  ||  {b}{flag}")

    if linker:
        print("\n--- weights splink learned (log2 bayes factor) ---")
        for c in linker.misc.save_model_to_json()["comparisons"]:
            print(f"  {c['output_column_name']}")
            for lvl in c["comparison_levels"]:
                m, u = lvl.get("m_probability"), lvl.get("u_probability")
                if lvl.get("is_null_level") or not m or not u:
                    continue
                print(f"      {lvl['label_for_charts']:<34} "
                      f"{math.log2(m / u):+7.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
