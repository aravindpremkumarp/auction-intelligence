"""Unit tests for the deadline-alert classifier (api/alerts/service.py).

Pure logic, no I/O — this is where a timezone or boundary bug would hide, so
the 7/3/1-day thresholds, past/null handling, and tz coercion are pinned
exhaustively.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.alerts.service import build_alerts, classify_deadline

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


def _in(**kw) -> datetime:
    return NOW + timedelta(**kw)


@pytest.mark.parametrize(
    "deadline, severity",
    [
        (_in(hours=1), "urgent"),       # < 1 day
        (_in(days=1), "urgent"),        # exactly 1 day → urgent (inclusive)
        (_in(days=1, seconds=1), "soon"),   # just past 1 day → soon
        (_in(days=3), "soon"),          # exactly 3 days → soon (inclusive)
        (_in(days=3, seconds=1), "upcoming"),  # just past 3 days → upcoming
        (_in(days=7), "upcoming"),      # exactly 7 days → upcoming (inclusive)
    ],
)
def test_threshold_boundaries(deadline: datetime, severity: str) -> None:
    verdict = classify_deadline(deadline, NOW)
    assert verdict is not None
    assert verdict["severity"] == severity


@pytest.mark.parametrize(
    "deadline",
    [
        _in(days=7, seconds=1),   # just past 7 days → no alert
        _in(days=30),             # far future → no alert
        _in(seconds=-1),          # already passed → no alert
        _in(days=-2),             # passed → no alert
        NOW,                      # exactly now → no alert (not actionable)
    ],
)
def test_no_alert_outside_window(deadline: datetime) -> None:
    assert classify_deadline(deadline, NOW) is None


@pytest.mark.parametrize("deadline", [None, "", "   ", "not-a-date", 12345])
def test_unparseable_deadline_is_none(deadline) -> None:
    assert classify_deadline(deadline, NOW) is None


def test_iso_string_and_naive_are_parsed_as_utc() -> None:
    # Neo4j toString() form with a Z suffix.
    assert classify_deadline("2026-06-16T12:00:00Z", NOW)["severity"] == "soon"
    # Naive ISO string is anchored to UTC, so it matches the tz-aware NOW.
    assert classify_deadline("2026-06-16T12:00:00", NOW)["severity"] == "soon"


def test_days_left_is_floored() -> None:
    # 2.9 days out reads as 2 whole days remaining.
    v = classify_deadline(_in(days=2, hours=21), NOW)
    assert v["days_left"] == 2
    assert v["message"] == "Bid deadline is in 2 days"


def test_within_24h_message() -> None:
    v = classify_deadline(_in(hours=5), NOW)
    assert v["severity"] == "urgent"
    assert v["message"] == "Bid deadline is within 24 hours"


def test_build_alerts_filters_sorts_and_shapes() -> None:
    rows = [
        {"auction_id": "a-far", "title": "Far", "city": "Chennai",
         "deadline": _in(days=30).isoformat()},
        {"auction_id": "a-soon", "title": "Soon", "city": "Madurai",
         "deadline": _in(days=2).isoformat()},
        {"auction_id": "a-urgent", "title": "Urgent", "city": "Salem",
         "deadline": _in(hours=10).isoformat()},
        {"auction_id": "a-past", "title": "Past", "city": "Trichy",
         "deadline": _in(days=-1).isoformat()},
        {"auction_id": "a-null", "title": "NoDate", "city": "Erode",
         "deadline": None},
        {"title": "NoId", "deadline": _in(days=1).isoformat()},  # dropped: no id
    ]
    alerts = build_alerts(rows, now=NOW)
    # Only the two in-window properties survive, soonest first.
    assert [a["auction_id"] for a in alerts] == ["a-urgent", "a-soon"]
    first = alerts[0]
    assert first["type"] == "deadline"
    assert first["severity"] == "urgent"
    assert first["city"] == "Salem"
    assert first["deadline"] is not None


def test_build_alerts_defaults_now_and_handles_empty() -> None:
    assert build_alerts([]) == []
    assert build_alerts(None) == []
