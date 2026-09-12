"""Value normalisation shared by every adapter. Pure functions, no I/O.

``clean_price`` and ``parse_date`` moved here from ``scripts/prepare_tn_data.py``
unchanged in behaviour for the eauctionsindia format; ``parse_date`` also
learned the two shapes the new portals use. Every parser returns the same
thing the loader has always cast with ``datetime()``: a naive ISO string in
Indian time, ``YYYY-MM-DDTHH:MM:SS``.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

_NULLISH = ("", "none", "n/a", "null", "-", "--")

# eauctionsindia: 'DD-MM-YYYY HHMM AM/PM', HHMM may be 3 or 4 digits.
_PORTAL_DATE = re.compile(r"(\d{2})-(\d{2})-(\d{4})\s+(\d{3,4})\s*(AM|PM)", re.IGNORECASE)
# bankeauctions: '15 Sep 2026 11:00' or '12 Sep 2026'.
_TEXT_DATE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?$")


def clean_price(raw) -> tuple[str, float | None]:
    """``'₹1,23,456.00'`` or its mojibake twin ``'â‚¹1,23,456.00'`` → ``(raw, 123456.0)``.

    Returns the raw value untouched as the first element so the loader can
    keep the portal's own spelling beside the number.
    """
    if not raw:
        return raw, None
    cleaned = re.sub(r"(â‚¹|₹|₹)", "", str(raw))
    cleaned = cleaned.replace(",", "").strip()
    try:
        return raw, float(cleaned)
    except ValueError:
        return raw, None


def parse_date(raw) -> str | None:
    """Any of the three portal date shapes → naive IST ISO ``YYYY-MM-DDTHH:MM:SS``.

    - ``'23-03-2026 0130 PM'`` (eauctionsindia) → ``'2026-03-23T13:30:00'``
    - ``'2026-09-17T05:30:00.000Z'`` (BAANKNET, UTC) → ``'2026-09-17T11:00:00'``
    - ``'15 Sep 2026 11:00'`` / ``'12 Sep 2026'`` (bankeauctions) → ``'2026-09-15T11:00:00'``

    Returns ``None`` for anything else, including impossible calendar dates.
    """
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw.lower() in _NULLISH:
        return None

    m = _PORTAL_DATE.match(raw)
    if m:
        day, month, year, hhmm, meridiem = m.groups()
        hhmm = hhmm.zfill(4)
        hour, minute = int(hhmm[:2]), int(hhmm[2:])
        if meridiem.upper() == "PM" and hour != 12:
            hour += 12
        elif meridiem.upper() == "AM" and hour == 12:
            hour = 0
        try:
            return datetime(int(year), int(month), int(day), hour, minute).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None

    m = _TEXT_DATE.match(raw)
    if m:
        day, mon, year, hh, mm = m.groups()
        for fmt in ("%b", "%B"):
            try:
                dt = datetime.strptime(f"{day} {mon[:3] if fmt == '%b' else mon} {year}", f"%d {fmt} %Y")
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
        return dt.replace(hour=int(hh or 0), minute=int(mm or 0)).strftime("%Y-%m-%dT%H:%M:%S")

    # ISO 8601, with or without zone. A zoned value is moved to IST; a naive
    # one is trusted as already local, which is what the loader has assumed.
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def make_auction_id(prefix: str, native) -> str:
    """``('bn-', 358394)`` → ``'bn-358394'``; ``('', '841207')`` → ``'841207'``.

    eauctionsindia ids stay bare so URLs, watchlists and trackers keep
    working; every other portal is prefixed so its six-digit ids cannot
    collide with those or with the band agent3 guards.
    """
    native = str(native or "").strip()
    if not native:
        raise ValueError("auction id needs a native id")
    return f"{prefix}{native}"


# ── document roles ───────────────────────────────────────────────────────────
#
# Decided from the portal's own label or the bundle file name. Order matters:
# "Sale Proclamation" must not become "tender" because it mentions a sale,
# and "Terms and Conditions of E auction" must not become "sale_notice"
# because it mentions the auction.

_NEWSPAPERS = (
    "dinakaran", "dinamalar", "dina thanthi", "daily thanthi", "dailythanthi", "thanthi",
    "dinamani", "malai malar", "maalaimalar", "tamil murasu", "murasu",
    "the hindu", "hindu", "times of india", "indian express", "new indian express",
    "financial express", "business standard", "business line", "businessline",
    "economic times", "deccan chronicle", "deccan herald", "mint",
)
# '<lender>-<paper>-<city>-<dd>-<mm>-<yyyy>' — how bankeauctions names a
# newspaper cutting inside the NIT bundle.
_PUBLICATION_NAME = re.compile(r"^[^-]+-[^-]+-[^-]+-\d{2}-\d{2}-\d{4}(\.pdf)?$", re.IGNORECASE)


def doc_role_for(label: str | None) -> str:
    """Route a document by what the portal called it. Unknown stays unknown."""
    if not label:
        return "unknown"
    text = label.strip()
    low = re.sub(r"\.pdf$", "", text.lower()).strip()
    low_sp = low.replace("_", " ")

    if "property detail" in low_sp:
        return "property_details"
    if "affidavit" in low_sp or "declaration" in low_sp:
        return "affidavit"
    if "terms" in low_sp and "condition" in low_sp:
        return "terms"
    if "publication" in low_sp or "paper" in low_sp or _PUBLICATION_NAME.match(text):
        return "publication"
    if any(p in low_sp for p in _NEWSPAPERS):
        return "publication"
    if "proclamation" in low_sp:
        return "proclamation"
    if "notice" in low_sp:
        return "sale_notice"
    if "tender" in low_sp or low_sp.startswith("nit"):
        return "tender"
    return "unknown"
