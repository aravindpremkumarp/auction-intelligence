"""Repair lot numbering that restarted at a LangExtract window boundary.

Why this exists
---------------
``extract_routing.char_buffer_for`` sizes the LangExtract window to the whole
notice, but caps it (``LANGEXTRACT_MAX_CHAR_BUFFER_CEILING``, default 30000) so
a pathologically long bundle still splits. A notice longer than that ceiling is
therefore extracted in two or more windows, and LangExtract extracts each one
INDEPENDENTLY — the second window cannot see the lots the first one found, so
its ``lot_index`` counter restarts at 1.

Nothing downstream noticed. ``build_lots`` and ``group_lots`` key a lot on the
model's own ``lot_index``, and ``promote_extractions`` then does
``MERGE (l:Lot {lot_key: "<filename>#<lot_index>"})``. Same index, same key,
same node: the second window's lot 1 was silently FUSED into the first
window's lot 1, carrying its identifiers, borrowers and boundaries in with it.
A 49k-char notice holding 19 real properties promoted to 12 Lot nodes, seven of
which were two unrelated properties mixed together.

That is worse than a dropped lot. A missing lot is visible — a listing with
nothing to link to. A fused lot links fine and reads as healthy, so the
lot-to-listing audit reports green while the lot underneath is wrong.

What this does
--------------
``renumber_window_lots`` finds where the numbering restarted and continues it
across the boundary, so each real property keeps a distinct index (and
therefore a distinct ``lot_key``).

It is deliberately conservative, because the mirror of this bug — splitting one
property into two lots — is just as damaging, and a stray ``lot_index`` has
several causes of which the window cut is only one. So a reset counts only
where LangExtract could actually have cut: the notice must be longer than the
window ``char_buffer_for`` would give it, the numbering must fall back to 1
(an independent window starts counting at 1; a lot straddling the cut that
gets tagged in both windows does not), and that fall must land near a multiple
of the window size. A notice failing any of those is returned UNCHANGED, down
to the object identity of its entities — which is every notice that fits one
window, i.e. nearly the whole corpus. Renumbering more widely would rewrite
``lot_key`` corpus-wide and orphan the ``resolved_lot_key`` pointers that
already resolve correctly. Even in a document that did reset, the first window
keeps its original indices; only the later windows shift up.

What this does NOT repair: a model that numbered badly inside one window. One
notice here tags 37 of its 41 properties ``lot_index=1``, and those stay fused,
because nothing in the entity stream says where one ends and the next begins.
That is an extraction-quality problem, not a windowing one.

Deliberately NOT solved here: the window split itself. Extracting a long notice
in one call is a routing/cost decision (see ``char_buffer_for``); this module
makes the split non-destructive whatever the ceiling is set to.
"""
from __future__ import annotations

from typing import NamedTuple


class Window(NamedTuple):
    """One LangExtract extraction window's share of a notice.

    ``shift`` is what this window's ``lot_index`` values must be raised by to
    continue the numbering of the windows before it. ``local_indices`` are the
    indices its property entities actually used, which is how a child entity
    naming a lot that does not exist here is told apart from one that does.
    """

    start: int
    shift: int
    local_indices: frozenset[int]


# Entity class that stands 1:1 with a lot. The model emits exactly one per
# property, so its lot_index sequence is what reveals a window reset; the other
# per-lot classes (identifier, boundary, extent, ...) repeat within one lot and
# cannot carry the signal on their own.
LOT_ANCHOR_CLS = "property"


def _index_of(entity: dict) -> int | None:
    """The entity's lot_index as an int, or None when it has none//is free text.

    Non-integer indices are left alone rather than guessed at: the key they
    produce is already unique, and renumbering text we do not understand would
    do more damage than the collision this module exists to prevent.
    """
    attrs = entity.get("attrs") or {}
    raw = attrs.get("lot_index")
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _start_of(entity: dict) -> int | None:
    start = entity.get("start")
    return start if isinstance(start, int) else None


#: How far a numbering reset may sit from the arithmetic window edge and still
#: be read as that edge. LangExtract cuts on text boundaries rather than at an
#: exact character count, and the first lot of a window starts some way into it
#: (a window opens on notice preamble, not on a property), so the observed
#: resets in this corpus run from ~30 chars before a 30k edge to ~1.6k after.
#: A tenth of the window covers that spread without reaching the next edge.
BOUNDARY_TOLERANCE = 0.10


def _document_length(entities: list[dict]) -> int:
    """Length of the markdown these entities were extracted from.

    Read off the entities' own spans so the caller need not carry the markdown
    around; the last span ends at or near the end of the document, which is all
    the precision ``char_buffer_for`` needs to reproduce the window size.
    """
    ends = [e["end"] for e in entities if isinstance(e.get("end"), int)]
    return max(ends) if ends else 0


def window_offsets(entities: list[dict]) -> list[Window]:
    """Split a notice into the LangExtract windows its numbering reveals.

    A reset is only believed where LangExtract could actually have cut the
    document: the notice must be longer than one window, and the numbering must
    fall back to 1 near a multiple of the window size ``char_buffer_for`` would
    have chosen for a notice this long. Numbering that stalls or steps back
    anywhere else is something other than a cut, and splitting on it would mint
    lots that do not exist — the mirror of the bug this module exists to fix.

    Returns [] when nothing qualifies, which is the caller's cue to leave the
    document alone.
    """
    from pipeline.extract_routing import char_buffer_for

    anchors = sorted(
        ((s, li) for s, li in
         ((_start_of(e), _index_of(e)) for e in entities
          if e.get("cls") == LOT_ANCHOR_CLS)
         if s is not None and li is not None),
        key=lambda pair: pair[0],
    )
    if len(anchors) < 2:
        return []

    length = _document_length(entities)
    buffer = char_buffer_for("x" * length)
    if length <= buffer:            # one window; nothing could have reset
        return []

    # Candidate resets: a window that cannot see the lots before it starts
    # counting at 1, so that is the only fall we treat as one. Numbering that
    # merely stalls or steps back is something else — most often a lot that
    # straddles the cut and gets tagged in both windows, where the second
    # window went on to number the rest correctly and shifting it would push
    # every later lot off its own listing.
    candidates = [start for start, index in anchors[1:] if index == 1]

    # Keep the one nearest each window edge, and only within tolerance of it.
    tolerance = buffer * BOUNDARY_TOLERANCE
    edges = sorted({
        min((c for c in candidates if abs(c - edge) <= tolerance),
            key=lambda c: abs(c - edge), default=None)
        for edge in range(buffer, length + buffer, buffer)
    } - {None})
    if not edges:
        return []

    # windows[k] = [offset it starts at, {local property indices inside it}]
    windows: list[tuple[int, set[int]]] = [(anchors[0][0], set())]
    for start, index in anchors:
        if start in edges and start != windows[-1][0]:
            windows.append((start, {index}))
        else:
            windows[-1][1].add(index)

    if len(windows) < 2:
        return []

    # The first window keeps its numbering (shift 0) so keys that already
    # resolve stay valid; each later window starts above every index used so
    # far, which is what makes the fused keys distinct.
    offsets: list[Window] = []
    shift = 0
    highest = 0
    for start, local_indices in windows:
        offsets.append(Window(start, shift, frozenset(local_indices)))
        highest = max(highest, max(local_indices, default=0) + shift)
        shift = highest
    return offsets


def _shift_for(start: int | None, index: int,
               offsets: list[Window]) -> int:
    """The shift applying to an entity at a character offset.

    An entity with no offset falls to the first window: that is where the
    pre-fix code put it, so an ungrounded entity keeps landing on the same lot
    it landed on before rather than moving somewhere new.

    A child entity whose ``lot_index`` names no property in its own window is
    also left where it was. The model does emit such orphans, and shifting one
    would mint a lot with no property in it — inventing a lot to avoid fusing
    two is not a trade worth making, and the pre-fix placement is at least the
    behaviour every downstream check was calibrated against.
    """
    if start is None:
        return 0
    window = offsets[0]
    for candidate in offsets:
        if start >= candidate.start:
            window = candidate
        else:
            break
    return window.shift if index in window.local_indices else 0


def renumber_window_lots(entities: list[dict]) -> list[dict]:
    """Continue lot numbering across LangExtract window resets.

    Returns the entity list to group by. When the numbering never reset (every
    document that fits one window — nearly the whole corpus) the input list is
    returned as-is. Otherwise a shallow copy is returned with ``lot_index``
    rewritten on the affected entities; the caller's list and the entities in
    the first window are never mutated.
    """
    if not entities:
        return entities
    offsets = window_offsets(entities)
    if not offsets:
        return entities

    renumbered: list[dict] = []
    for entity in entities:
        index = _index_of(entity)
        shift = (_shift_for(_start_of(entity), index, offsets)
                 if index is not None else 0)
        if not shift:
            renumbered.append(entity)
            continue
        moved = dict(entity)
        moved["attrs"] = {**(entity.get("attrs") or {}),
                          "lot_index": str(index + shift)}
        renumbered.append(moved)
    return renumbered
