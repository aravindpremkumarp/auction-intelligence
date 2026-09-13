"""What a portal listing id looks like in prose — the one pattern every gate,
manifest and panel reads, so they cannot drift apart.

Two shapes (docs/SCHEMA.md, "Ids"):

- **bare six digits** — eauctionsindia, the id band `600 000–999 999`
  (wider than the observed range on purpose: it is a portal sequence that
  grows). A six-digit *price* sits in that band too — `₹6,50,000` is
  650000 — so bare ids are also checked for currency context.
- **prefixed** — `bn-<digits>` (BAANKNET) and `be-<digits>` (bankeauctions),
  four to eight digits. Never a price, never in the band; the prefix is the
  whole guard.

Stdlib only: `api/chat/v2/middleware/answer_gate.py` and `api/chat/panel.py`
import this on the request path and must stay free of the agent stack.
"""
from __future__ import annotations

import re
from typing import Iterator, NamedTuple

#: The lookarounds on the bare form reject a six-digit run that is part of a
#: longer number: a bare digit either side, or a comma/period that is itself
#: between digits (`1,234,567`, `1234.567890`). They must NOT reject a
#: trailing sentence period — an id in prose almost always sits before one.
#: The prefixed form is bounded by anything that is not a word char or a
#: hyphen, so `abn-1234` and `bn-1234-5` are not ids.
ID_LIKE = re.compile(
    r"(?<![\w-])(?P<prefixed>(?:bn|be)-\d{4,8})(?![\w-])"
    r"|(?<!\d)(?<!\d,)(?<!\d\.)(?P<bare>\d{6})(?!\d)(?!,\d)(?!\.\d)",
    re.IGNORECASE)
ID_BAND = (600_000, 999_999)

_CURRENCY_BEFORE = re.compile(r"(₹|rs\.?|inr)\s*$", re.I)
_CURRENCY_AFTER = re.compile(
    r"^\s*(lakh|lakhs|lac|crore|crores|cr\b|l\b|rupees)", re.I)


class IdToken(NamedTuple):
    token: str        # normalised: prefixed ids lowercased
    prefixed: bool
    start: int
    end: int


def iter_id_tokens(text: str | None) -> Iterator[IdToken]:
    """Every id-shaped token in ``text``, unguarded, in order."""
    for m in ID_LIKE.finditer(text or ""):
        if m.group("prefixed"):
            yield IdToken(m.group("prefixed").lower(), True, m.start(), m.end())
        else:
            yield IdToken(m.group("bare"), False, m.start(), m.end())


def is_portal_id(token: str | None) -> bool:
    """Whole-string test: is this one id, of either shape?"""
    if not token:
        return False
    m = ID_LIKE.fullmatch(token.strip())
    if not m:
        return False
    if m.group("prefixed"):
        return True
    return ID_BAND[0] <= int(m.group("bare")) <= ID_BAND[1]


def guarded_ids(text: str) -> list[str]:
    """Portal ids in prose, in order, deduplicated.

    A bare six-digit run must sit in the id band and not read as a price
    (currency before, or a unit like lakh / crore after). A prefixed id needs
    neither guard. The guards are the whole value: without them every
    correctly-quoted six-digit price reads as a citation.
    """
    text = text or ""
    out: list[str] = []
    for t in iter_id_tokens(text):
        if not t.prefixed:
            if not (ID_BAND[0] <= int(t.token) <= ID_BAND[1]):
                continue
            if _CURRENCY_BEFORE.search(text[max(0, t.start - 6):t.start]):
                continue
            if _CURRENCY_AFTER.match(text[t.end:t.end + 12]):
                continue
        if t.token not in out:
            out.append(t.token)
    return out


def all_ids(text: str) -> list[str]:
    """Every id-shaped token, no band or currency guard, deduplicated — the
    looser variant the artifact fallback wants (it fetches what the answer
    names and lets the graph say no)."""
    out: list[str] = []
    for t in iter_id_tokens(text):
        if t.token not in out:
            out.append(t.token)
    return out
