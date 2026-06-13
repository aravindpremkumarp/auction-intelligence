"""
api/alerts/service.py
---------------------
Pure deadline-classification logic for property alerts. No I/O — the
repository fetches rows, this module turns them into alert objects. Kept
side-effect-free so the threshold bucketing (the one place a timezone or
boundary bug would hide) is exhaustively unit-testable.

Thresholds are evaluated on the exact remaining time, so the boundaries are
deterministic:
  - 0  <  seconds_left ≤ 1 day   → "urgent"
  - 1  <  seconds_left ≤ 3 days  → "soon"
  - 3  <  seconds_left ≤ 7 days  → "upcoming"
  - seconds_left ≤ 0 (passed) or > 7 days → no alert

`application_deadline_dt` is a ZONED Neo4j datetime; comparing it against a
UTC `now` is correct regardless of the stored offset (both are absolute
instants). Naive deadlines are promoted to UTC to match how the rest of the
graph was written (see cypher_tools._aware).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

_DAY_SECONDS = 86_400

# severity -> (max age in days, inclusive). Ordered most-urgent first; the
# first bucket whose ceiling the property satisfies wins, so each property
# yields exactly one alert.
_THRESHOLDS: tuple[tuple[str, int], ...] = (
    ("urgent", 1),
    ("soon", 3),
    ("upcoming", 7),
)
_MAX_THRESHOLD_DAYS = _THRESHOLDS[-1][1]


def _to_aware(deadline: datetime | str | None) -> datetime | None:
    """Coerce a deadline (datetime or ISO string) to a tz-aware datetime.

    Returns None for null, empty, or unparseable input so the caller can
    simply skip the property. Naive datetimes are anchored to UTC.
    """
    if deadline is None:
        return None
    dt: datetime | None = None
    if isinstance(deadline, datetime):
        dt = deadline
    elif isinstance(deadline, str):
        s = deadline.strip()
        if not s:
            return None
        # Neo4j toString() emits e.g. "2026-06-20T17:00:00Z" or with offset.
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def classify_deadline(
    deadline: datetime | str | None, now: datetime
) -> dict | None:
    """Classify one deadline into an alert payload, or None if it doesn't
    warrant one (missing/unparseable, already passed, or further than the
    widest threshold away).

    Returns {severity, days_left, message} on a hit. `days_left` is whole
    days remaining (floored), so a deadline 2.9 days out reads as 2.
    """
    dt = _to_aware(deadline)
    if dt is None:
        return None
    seconds_left = (dt - now).total_seconds()
    if seconds_left <= 0:
        return None  # already passed (or exactly now) — nothing to act on
    if seconds_left > _MAX_THRESHOLD_DAYS * _DAY_SECONDS:
        return None
    severity = next(
        name for name, days in _THRESHOLDS if seconds_left <= days * _DAY_SECONDS
    )
    days_left = math.floor(seconds_left / _DAY_SECONDS)
    return {
        "severity": severity,
        "days_left": days_left,
        "message": _message(seconds_left, days_left),
    }


def _message(seconds_left: float, days_left: int) -> str:
    if seconds_left <= _DAY_SECONDS:
        return "Bid deadline is within 24 hours"
    if days_left <= 1:
        return "Bid deadline is in 1 day"
    return f"Bid deadline is in {days_left} days"


def build_alerts(rows: list[dict], now: datetime | None = None) -> list[dict]:
    """Turn raw saved/looked-up auction rows into sorted alert objects.

    Each input row is expected to carry `auction_id`, `title`, `city`, and
    `deadline` (datetime or ISO string). Rows without a qualifying deadline
    are dropped. Output is sorted most-urgent first (soonest deadline).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    alerts: list[dict] = []
    for row in rows or []:
        auction_id = row.get("auction_id")
        if not auction_id:
            continue
        verdict = classify_deadline(row.get("deadline"), now)
        if verdict is None:
            continue
        alerts.append(
            {
                "auction_id": auction_id,
                "title": row.get("title"),
                "city": row.get("city"),
                "deadline": _deadline_iso(row.get("deadline")),
                "type": "deadline",
                "severity": verdict["severity"],
                "days_left": verdict["days_left"],
                "message": verdict["message"],
            }
        )
    alerts.sort(key=lambda a: a["days_left"])
    return alerts


def _deadline_iso(deadline: datetime | str | None) -> str | None:
    dt = _to_aware(deadline)
    return dt.isoformat() if dt else None
