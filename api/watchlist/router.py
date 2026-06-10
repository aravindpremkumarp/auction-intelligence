"""
api/watchlist/router.py
-----------------------
`/watchlist` endpoints. All require a valid Supabase access token; the
saved-auction set is keyed to the authenticated user's supabase_id.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from api.auth.dependencies import get_current_user
from api.auth.schemas import UserOut
from api.watchlist import repository as repo


router = APIRouter()


@router.get("/watchlist")
def list_watchlist(user: UserOut = Depends(get_current_user)) -> dict:
    return {"ids": repo.list_saved_auction_ids(user.id)}


@router.post("/watchlist/{auction_id}", status_code=204)
def save_auction(
    auction_id: str,
    user: UserOut = Depends(get_current_user),
) -> Response:
    ok = repo.add_saved(user.id, auction_id)
    if not ok:
        raise HTTPException(status_code=404, detail="auction not found")
    return Response(status_code=204)


@router.delete("/watchlist/{auction_id}", status_code=204)
def unsave_auction(
    auction_id: str,
    user: UserOut = Depends(get_current_user),
) -> Response:
    repo.remove_saved(user.id, auction_id)
    return Response(status_code=204)
