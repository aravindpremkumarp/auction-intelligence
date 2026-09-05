"""
pipeline/notice_twins.py
------------------------
One page, several file names — group the copies so a paid pass runs once.

Why this exists
~~~~~~~~~~~~~~~
Portals name an upload with the millisecond it was uploaded, so one bank
publishing one notice against six lots produces six file names
(``KARNTK17819383495370.jpg`` … ``KARNTK17819391325440.jpg``, 1.6 seconds apart
and byte-for-byte identical — see ``pipeline/ink_fingerprint.py``). Each copy is
a separate ``:Document`` carrying one auction's link, and both paid passes key
on the file rather than on the page:

* OCR (``scripts/ocr_missing_markdowns.py``) caches under ``file_path``, so six
  names are six cache misses and six billed jobs.
* Extraction (``pipeline/load_extractions.py``) runs one multi-minute model call
  per Document with no content check at all.

Deleting five of the six is not the fix and never can be: five of them are the
only link to their own auction's data (``scripts/collapse_identical_documents.py``
refuses exactly that). The fix is to *share* — do the work once and hand the
copies the answer.

Two keys, deliberately not one
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The safe key differs per pass, because each pass consumes a different artifact:

``source_key`` — SHA-256 of the **file bytes** (``Document.content_sha256``,
    written by ``scripts/find_duplicate_notices.py`` and, from now on, by the
    OCR script itself as it downloads). OCR turns bytes into markdown, so the
    same bytes give the same markdown. Exact equality, no threshold, no false
    positives.

``text_key`` — SHA-256 of the **stored markdown**. Extraction reads markdown and
    returns character offsets *into that markdown* (``extraction_json`` carries
    ``start``/``end``, which ``api/review/extraction.py`` maps back onto
    ``d.markdown``). Sharing an extraction across two documents whose markdown
    differs by one character silently misaligns every highlight — so identical
    bytes are **not** enough here. Two byte-identical files OCR'd by different
    engines, or by the same engine either side of a re-ingest, hold different
    markdown; only the text key can see that.

Neither key is the ink signature. ``ink_fingerprint`` is a high-precision,
*low-recall* similarity measure that explicitly cannot separate a duplicate from
a re-auction, which is why it reports and never merges. Sharing a result is a
write, so it takes an exact key or nothing.

What this module does not do
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
It never overwrites. Callers hand it only documents that are missing the
artifact in question, and the copy statements they build re-assert that in the
``WHERE``. A Document that already holds an extraction may also hold reviewer
corrections keyed by field id (``extraction_corrections_json``), and replacing
the extraction under them would re-point every correction at a different field.

DB-free on purpose: the grouping is arithmetic, its tests should need nothing on
the path, and the two callers keep their own Cypher.
"""
from __future__ import annotations

import hashlib


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_key(doc: dict) -> str | None:
    """Identity of the *file*: its stored SHA-256, or None when unknown.

    Never falls back to the file name — that is the one thing the portal varies,
    and a fallback that keys on it would group nothing while looking like it
    worked.
    """
    sha = (doc.get("content_sha256") or "").strip()
    return sha or None


def text_key(doc: dict) -> str | None:
    """Identity of the *markdown*: SHA-256 of the stored text, or None if empty.

    Whitespace is not stripped and case is not folded. The offsets in an
    extraction are positions in this exact string, so anything that changes a
    character count changes the identity.
    """
    md = doc.get("markdown")
    if md is None:
        md = doc.get("md")
    return _sha256(md) if md else None


def group_twins(docs: list[dict], key=text_key) -> list[list[dict]]:
    """Group ``docs`` by ``key``, keeping input order within and between groups.

    Documents the key cannot identify (no hash, no markdown) come back as
    singleton groups rather than being dropped or lumped together: unknown is
    not a match, and the caller still has to process them.
    """
    groups: list[list[dict]] = []
    index: dict[str, int] = {}
    for d in docs:
        k = key(d)
        if k is None:
            groups.append([d])
            continue
        if k in index:
            groups[index[k]].append(d)
        else:
            index[k] = len(groups)
            groups.append([d])
    return groups


def merge_rosters(group: list[dict], field: str = "roster") -> list[dict]:
    """Union the group's portal rosters, de-duplicated by ``aid``.

    The roster is the portal's own row per lot, handed to the extractor as
    segmentation context (``pipeline/load_extractions.ROSTER_CYPHER``). It is
    built per Document, and each copy of a six-lot notice links one auction — so
    every copy tells the model to find one lot on a page that lists six. Merging
    restores the list the notice actually advertises, which is the whole point of
    passing a roster.

    First occurrence of an ``aid`` wins; rows without one are kept in order (a
    roster row is already filtered to something usable, and dropping an
    unlabelled lot would under-report the page).
    """
    out: list[dict] = []
    seen: set = set()
    for doc in group:
        for row in doc.get(field) or []:
            aid = row.get("aid") if isinstance(row, dict) else None
            if aid is None:
                out.append(row)
                continue
            if aid in seen:
                continue
            seen.add(aid)
            out.append(row)
    return out


def plan_reuse(docs: list[dict], donors: dict[str, str],
               key=source_key) -> tuple[list[dict], list[dict]]:
    """Split work into "do it once" and "copy the answer".

    ``docs``   — documents still missing the artifact, each carrying the key
                 field (``content_sha256`` for the source key).
    ``donors`` — ``{key: donor_filename}`` for keys some *other* Document
                 already holds a finished artifact for.

    Returns ``(to_run, copies)``:

    * ``to_run``  — one document per group that has no donor: the leader, which
      actually pays for the pass. Order follows the input.
    * ``copies``  — ``{"donor": filename, "file_path": ..., "filename": ...}``
      rows the caller writes once the donor holds the artifact. A group with a
      donor contributes every member; a group without one contributes its
      followers, whose donor is the leader that just ran.

    Unkeyable documents are their own group with no donor, so they always land
    in ``to_run`` — reuse degrades to today's behaviour rather than skipping a
    document whose identity we could not establish.
    """
    to_run: list[dict] = []
    copies: list[dict] = []
    for group in group_twins(docs, key=key):
        k = key(group[0])
        donor = donors.get(k) if k is not None else None
        if donor:
            members = group
        else:
            leader = group[0]
            to_run.append(leader)
            donor = leader.get("filename")
            members = group[1:]
        for m in members:
            copies.append({"donor": donor,
                           "file_path": m.get("file_path"),
                           "filename": m.get("filename")})
    return to_run, copies
