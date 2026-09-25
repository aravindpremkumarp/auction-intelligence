"""Only ever improve a stored extraction.

A re-extraction replaces a notice's whole entity list. A new read often gains
something (a reserve price the last read skipped) and loses something else (a
location it had), so saving it blindly makes the corpus drift instead of
improve. ``judge`` compares the stored read with the new one on what a notice
is worth, and says whether the new one may replace it:

1. **Lots.** With a reviewer lot count, the read whose lot count is closer to
   it wins outright — a notice split into the wrong number of lots is wrong
   whatever else it holds, and its lots cannot be compared one to one. Without
   a count, a read that finds fewer lots loses.
2. **Key facts per lot** (pipeline/key_entities: reserve price, auction date,
   property type, location, extent, full description, possession). A fact the
   stored read had for a lot and the new one lacks is a loss; the reverse is a
   gain.
3. **Validator score** (pipeline/validators). Catches what the checklist does
   not: wrong values, ungrounded spans, over-split extras.

The new read is saved only with at least one gain and no loss. A read that is
merely as good is not saved either: saving resets the review status, and
churn with nothing to show for it is a cost.

The caller decides when the comparison applies. It does not when the notice's
text changed since the stored read — the old spans point into a text that is
gone, so the new read wins by default.
"""
from __future__ import annotations

from pipeline.key_entities import KEY_LABELS, key_checklist
from pipeline.validators import validate_stored


def lot_count(ents: list[dict]) -> int:
    return len({str((e.get("attrs") or {}).get("lot_index"))
                for e in ents
                if (e.get("attrs") or {}).get("lot_index") not in (None, "")})


def _filled(ents: list[dict]) -> dict[str, set[str]]:
    """lot_index -> the key facts the read carries for that lot."""
    out: dict[str, set[str]] = {}
    for lot in key_checklist(ents)["lots"]:
        if lot.get("extracted"):
            out[str(lot["lot_index"])] = {
                k for k, c in lot["cells"].items() if c["status"] == "filled"}
    return out


def judge(old: list[dict], new: list[dict], text: str,
          expected_lot_count: int | None = None
          ) -> tuple[bool, list[str], list[str]]:
    """``(save, gains, losses)`` for replacing ``old`` with ``new``.

    ``gains`` and ``losses`` are short human-readable reasons ("lot 3:
    reserve price", "score 56 → 36"); the caller prints them.
    """
    if not old:
        return True, ["no stored extraction"], []
    if not new:
        return False, [], ["new read is empty"]

    gains: list[str] = []
    losses: list[str] = []

    n_old, n_new = lot_count(old), lot_count(new)
    if n_new != n_old:
        tag = f"lots {n_old} → {n_new}"
        if expected_lot_count:
            exp = int(expected_lot_count)
            d_old, d_new = abs(n_old - exp), abs(n_new - exp)
            if d_new < d_old:
                return True, [f"{tag} (expected {exp})"], []
            if d_new > d_old:
                return False, [], [f"{tag} (expected {exp})"]
        elif n_new < n_old:
            return False, [], [tag]

    had, has = _filled(old), _filled(new)
    for li in sorted(set(had) | set(has), key=lambda s: (len(s), s)):
        before, after = had.get(li, set()), has.get(li, set())
        losses += [f"lot {li}: {KEY_LABELS[k].lower()}"
                   for k in sorted(before - after)]
        gains += [f"lot {li}: {KEY_LABELS[k].lower()}"
                  for k in sorted(after - before)]

    s_old = validate_stored(old, source_text=text)["score"]
    s_new = validate_stored(new, source_text=text)["score"]
    if s_new < s_old:
        losses.append(f"score {s_old} → {s_new}")
    elif s_new > s_old:
        gains.append(f"score {s_old} → {s_new}")

    return (bool(gains) and not losses), gains, losses
