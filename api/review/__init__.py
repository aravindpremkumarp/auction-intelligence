"""
api/review
----------
Admin-only enrichment review surface. Lets a reviewer compare each
AuctionProperty's notice-extracted `description` against the source sales
notice (PDF or OCR markdown) and verify, edit, or unverify it.

Solves the multi_splitter_review.xlsx pain: instead of eyeballing a
spreadsheet next to a separately-opened PDF, the reviewer sees both side by
side in a browser, with one click each to verify or save an edit.
"""
from __future__ import annotations

from api.review.router import router

__all__ = ["router"]
