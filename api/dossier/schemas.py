"""
api/dossier/schemas.py
----------------------
Request models for the dossier router. Responses are returned as plain dicts
(matching the watchlist/conversations routers) so the checklist structure from
``api.dossier.checklist`` can flow through unchanged.

A dossier attaches to a property that may NOT be in the public graph (Premise
4): either an existing scraped ``:AuctionProperty`` (by ``auction_id``) or a
user-created off-graph ``:UserProperty`` (free-text survey/sub-registrar/
address). Exactly one must be supplied at creation.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class UserPropertyIn(BaseModel):
    """An off-graph property a user wants to vet (not in our scraped graph)."""
    label: str = Field(min_length=1, max_length=200)
    survey_no: str | None = Field(default=None, max_length=120)
    sub_registrar: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=500)


class DossierCreateIn(BaseModel):
    """Create a dossier for either an on-graph auction or an off-graph property.

    Provide exactly one of ``auction_id`` or ``user_property``.
    """
    title: str | None = Field(default=None, max_length=200)
    auction_id: str | None = Field(default=None, max_length=200)
    user_property: UserPropertyIn | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "DossierCreateIn":
        has_auction = bool(self.auction_id)
        has_user_prop = self.user_property is not None
        if has_auction == has_user_prop:
            raise ValueError(
                "provide exactly one of 'auction_id' or 'user_property'"
            )
        return self
