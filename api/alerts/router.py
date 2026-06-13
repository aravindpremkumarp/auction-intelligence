"""
api/alerts/router.py
--------------------
`/alerts` endpoints powering the in-app deadline-alert bell.

  - `GET /alerts` — alerts for the authenticated user's saved properties,
    resolved server-side. Returns an empty list for anonymous callers (the
    bell simply shows nothing) rather than 401, so the SPA can call it
    unconditionally.
  - `POST /alerts` — alerts for an explicit id set in the body. This is the
    anonymous path (the client sends its localStorage watchlist, since a GET
    can't carry a body); an authenticated caller with an empty body falls
    back to their saved set.

Registered always-on (not auth-gated) so the anonymous POST path works even
when the watchlist router is disabled.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.alerts import repository as repo
from api.alerts.service import build_alerts
from api.auth import get_optional_user
from api.auth.schemas import UserOut

router = APIRouter()


class Alert(BaseModel):
    auction_id: str
    title: str | None = None
    city: str | None = None
    deadline: str | None = None
    type: str = "deadline"
    severity: str
    days_left: int
    message: str


class AlertsOut(BaseModel):
    alerts: list[Alert] = []
    count: int = 0


class AlertsByIdsIn(BaseModel):
    auction_ids: list[str] = Field(default_factory=list)


def _packaged(rows: list[dict]) -> AlertsOut:
    alerts = build_alerts(rows, now=datetime.now(timezone.utc))
    return AlertsOut(alerts=alerts, count=len(alerts))


@router.get("/alerts", response_model=AlertsOut)
async def list_alerts(user: UserOut | None = Depends(get_optional_user)) -> AlertsOut:
    if user is None:
        return AlertsOut(alerts=[], count=0)
    rows = await repo.deadlines_for_saved(user.id)
    return _packaged(rows)


@router.post("/alerts", response_model=AlertsOut)
async def list_alerts_for_ids(
    body: AlertsByIdsIn,
    user: UserOut | None = Depends(get_optional_user),
) -> AlertsOut:
    if not body.auction_ids and user is not None:
        rows = await repo.deadlines_for_saved(user.id)
    else:
        rows = await repo.deadlines_for_ids(body.auction_ids)
    return _packaged(rows)
