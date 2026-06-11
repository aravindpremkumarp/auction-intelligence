"""`search_auctions` defaults to future-only — past auctions must not surface
as 'best match' just because the user forgot to pass starts_after. Opt-in
via include_past=True for genuine retrospective questions."""
from __future__ import annotations


def _patch_run_query(monkeypatch, *, total_count: int = 0):
    calls: list[tuple[str, dict]] = []
    state = {"call": 0}

    def fake_run_query(cypher: str, params: dict | None = None):
        calls.append((cypher, dict(params or {})))
        state["call"] += 1
        if state["call"] == 1:
            return [{"total_count": total_count}]
        return []

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_query", fake_run_query)
    monkeypatch.setattr(
        ct, "run_read_query",
        lambda cypher, params=None, timeout=10.0, max_rows=200: fake_run_query(cypher, params),
    )
    return calls


def test_default_excludes_past_auctions(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    search_auctions(city="Chennai", limit=0)

    cypher, params = calls[0]
    assert "a.auction_start_dt >= $starts_after" in cypher
    assert "starts_after" in params  # auto-set to now()


def test_explicit_starts_after_is_preserved(monkeypatch) -> None:
    """Caller-supplied starts_after wins over the now() default. Naive
    inputs are promoted to UTC so the param matches the stored ZONED
    DATETIME on the column side."""
    from datetime import datetime, timezone
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    search_auctions(starts_after=datetime(2020, 1, 1), limit=0)
    _, params = calls[0]
    assert params["starts_after"] == datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_include_past_opt_in_disables_now_floor(monkeypatch) -> None:
    """include_past=True drops the implicit now() floor so retrospective
    queries (e.g. 'how many auctions happened last year') work."""
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    search_auctions(city="Chennai", limit=0, include_past=True)
    cypher, params = calls[0]
    # No date floor when include_past=True and caller didn't pass one.
    assert "a.auction_start_dt >= $starts_after" not in cypher
    assert "starts_after" not in params
