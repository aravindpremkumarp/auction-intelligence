"""Hand-labelled lender identities for the 194 raw bank_name strings.

Gold clusters = normalized token-set equality (pipeline.entity_resolution.org_key,
whose merges were checked by hand across all 39 multi-variant groups and join
nothing that should stay apart), PLUS the merges below, which equality cannot
reach.

The base is org_key rather than resolve(), deliberately. resolve() is the thing
under test and has since grown a second auto-merge tier; seeding the labels from
it would let the answer sheet move whenever the code does, and every method
would score against its own output.

Independently corroborated: of the 14 pairs these labels add, nine already
carry an "approved" (:ResolutionDecision) from a human reviewer, recorded
before this file existed.

EXTRA_MERGES: each tuple is one real institution written several ways. Every
one is OCR damage, an abbreviation, or a stray prefix, checked by hand against
the printed corpus.

JUDGMENT: pairs where a reasonable reviewer could go either way. Held out of
the headline numbers so the metrics stay defensible; listed so they are not
silently ignored.

The traps are not enumerated: any two names not in the same gold cluster are a
non-match, so "Axis Bank Ltd" vs "Axis Finance Limited" is scored as a negative
automatically.
"""
from __future__ import annotations

EXTRA_MERGES: list[tuple[str, ...]] = [
    # OCR damage inside a word: the token set differs, so org_key cannot merge.
    ("ICICI Bank Limited", "IICI Bank Limited"),
    ("The Karur Vysya Bank Ltd", "The Kanur Vysya Bank Ltd",
     "The Karur Vyssa Bank Ltd."),
    ("Manappuram Home Finance Ltd", "Manapouram Home Finance Ltd"),
    ("Piramal Finance Ltd", "Pirama Finance Ltd"),
    # IIFL Home Finance, damaged two different ways.
    ("IFL Home Finance Limited", "IFIL Home Finance Limited"),
    # Hinduja Housing Finance, truncated.
    ("Hinduja Housing Finance Limited", "Hindu Housing Finance Limited"),
    # Vistaar Financial Services, truncated.
    ("Vistaar Financial Services Private Limited",
     "Vista Financial Services Private Limited"),
    # One lender, four spellings: OCR damage plus a "Housing" prefix bleeding
    # in from the surrounding line. CIFCL is a single legal entity; there is no
    # separate "Housing Cholamandalam".
    ("Cholamandalam Investment and Finance Company Limited",
     "CHOLAMANDAM INVESTMENT AND FINANCE COMPANY LIMITED",
     "Housing CHOLAMANDALAM INVESTMENT AND FINANCE COMPANY LIMITED",
     "Housing CHOLAMANDALMENT INVESTMENT AND FINANCE COMPANY LIMITED"),
    # The name with its own abbreviation appended.
    ("Asset Reconstruction Company (India) Limited",
     "Asset Reconstruction Company (India) Limited (ARCIL)"),
    ("JM Financial Asset Reconstruction Company Limited", "JMFARC (JM)"),
    # "Tamilnadu" / "Tamil Nadu" / a leading "The" / a trailing abbreviation.
    ("Tamil Nadu Industrial Investment Corporation Limited",
     "Tamilnadu Industrial Investment Corporation Limited (TIIC Ltd.)",
     "The Tamilnadu Industrial Investment Corporation Limited"),
    # Pure abbreviation. Note PNB Housing Finance is a DIFFERENT company and
    # must not join this cluster.
    ("Punjab National Bank", "PNB"),
    # "M/s" is a form of address, not part of the name.
    ("Religare Finvest Ltd", "M/s Religare Finvest Ltd."),
]

#: Held out of scoring. Same legal entity after a rename, which is a policy
#: question about what a lender node means, not a string-matching question.
JUDGMENT: list[tuple[str, ...]] = [
    ("Reliance Asset Reconstruction Company Ltd",
     "REFORM ARC LIMITED (Formerly known as Reliance Asset Reconstruction "
     "Company Limited)"),
]

#: Strings that are not lenders at all. They reached bank_name through an
#: extraction error and each stands alone in the gold set. Recorded because a
#: matcher that pulls any of them into a real lender is doing damage the
#: pairwise numbers alone under-state.
NOT_A_LENDER: set[str] = {
    "Bank/Secured Creditor",
    "M/s. Fipola Retail (India) Private Limited (In Liquidation)",
    "St. John Freight Systems Limited",
    "Jaatvedas Construction Company Private Limited",
    "SPP Insolvency Professionals LLP",
    "Incorp Restructuring Services LLP",
    "KrazyBee Services Limited",
}


def gold_clusters(names) -> dict[str, int]:
    """Map every raw name string to a gold cluster id.

    Buckets by org_key, then unions the buckets named in EXTRA_MERGES. Raises
    if a name in EXTRA_MERGES is not in the corpus, so the labels cannot
    silently rot as the corpus grows.
    """
    import sys
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from pipeline.entity_resolution import org_key

    keys: dict[str, int] = {}
    label: dict[str, int] = {}
    for name in names:
        label[name] = keys.setdefault(org_key(name), len(keys))

    known = set(label)
    for merge in EXTRA_MERGES:
        missing = [n for n in merge if n not in known]
        if missing:
            raise KeyError(f"gold names not present in corpus: {missing}")
        target = min(label[n] for n in merge)
        doomed = {label[n] for n in merge}
        for name, cid in list(label.items()):
            if cid in doomed:
                label[name] = target
    return label
