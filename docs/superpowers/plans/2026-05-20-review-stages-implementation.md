# Review queue: stages, uniform statuses, range filters — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the `/review` queue UI into three pipeline stages (classification → markdown → description) with uniform pending/verified/edited statuses, from/to score + notice-type filters, and property/sales-notice sub-groups inside each stage.

**Architecture:** Additive API changes first (accept new status values + new params alongside existing), then a single frontend pass that swaps the toolbar and routing, then a cleanup pass that retires legacy enum values. This keeps every commit green and reversible.

**Tech Stack:** FastAPI + Pydantic (router), Neo4j + Cypher (queries), vanilla JS + HTML (single-file frontend at `web/review.html`), pytest (API tests).

**Spec:** [docs/superpowers/specs/2026-05-20-review-stages-design.md](../specs/2026-05-20-review-stages-design.md)

**Line numbers are pre-change snapshots.** Each task lists the line ranges as they exist at the *start* of the plan. After earlier tasks land, line numbers in later tasks will have shifted — use the surrounding code anchors (function names, neighboring tags) to locate the right spot.

---

## File map

| File | Role | Change shape |
|---|---|---|
| `api/review/router.py` | FastAPI endpoints + Pydantic models | Add `notice_type`, `confidence_max`, `score_max` query params. Extend status enums (additive). Add 2 new endpoints. Update stats response models. |
| `api/review/queries.py` | Cypher gateway | New WHERE-clause helpers for uniform status + notice-type filter. Two new `list_*_by_property` functions. |
| `web/review.html` | Single-file frontend | Toolbar restructure (stage tabs, sub-group pills, score from/to inputs, notice-type pills). `queueState` refactor. Hash router update. Two new render functions. |
| `tests/api/test_review_classification.py` | Existing tests | Add cases for new status values, `confidence_max`, `notice_type`, `by-property` endpoint. |
| `tests/api/test_review_markdown.py` | **New** test file | Mirror the classification test patterns for markdown endpoints. |

---

## Phase 1 — Backend: additive API changes

### Task 1: Add uniform status enum to classification queue

**Files:**
- Modify: `api/review/queries.py:372` (status type), `:375-411` (`list_classification_queue` status branches)
- Modify: `api/review/router.py:441` (endpoint status enum)
- Test: `tests/api/test_review_classification.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/api/test_review_classification.py`:

```python
def test_classifications_accepts_uniform_status_values(client) -> None:
    """status=verified, status=edited must be accepted (uniform model).

    Existing `disagreement` still works (additive, not breaking)."""
    _ensure_admin_user()
    for s in ("pending", "verified", "edited", "all", "disagreement"):
        r = client.get(f"/review/classifications?status={s}", headers=_admin_header())
        assert r.status_code == 200, f"status={s} rejected: {r.text}"
```

- [ ] **Step 2: Run test, expect fail**

```
pytest tests/api/test_review_classification.py::test_classifications_accepts_uniform_status_values -v
```
Expected: FAIL — `status=edited` returns 422 (not in Literal enum).

- [ ] **Step 3: Extend `ClassificationStatus` type and add `edited` branch**

In `api/review/queries.py:372`, change:
```python
ClassificationStatus = Literal["pending", "disagreement", "verified", "all"]
```
to:
```python
ClassificationStatus = Literal[
    "pending", "verified", "edited", "all",
    "disagreement",  # legacy — retire in Task 16
]
```

In `api/review/queries.py:402-411`, replace the status branches:
```python
    where = ["d.notice_type IS NOT NULL"]
    if status == "pending":
        where.append("d.notice_type_verified_at IS NULL")
    elif status == "verified":
        where.append("d.notice_type_verified_at IS NOT NULL")
        where.append("coalesce(d.notice_type_overridden, false) = false")
    elif status == "edited":
        where.append("d.notice_type_verified_at IS NOT NULL")
        where.append("coalesce(d.notice_type_overridden, false) = true")
    elif status == "disagreement":
        where.append("d.notice_type_verified_at IS NULL")
        where.append("d.notice_type_classifier_pred IS NOT NULL")
        where.append("d.notice_type <> d.notice_type_classifier_pred")
    # "all" → no extra filter
```

- [ ] **Step 4: Extend the endpoint's `Literal` enum**

In `api/review/router.py:441`, change:
```python
    status: Literal["pending", "disagreement", "verified", "all"] = "pending",
```
to:
```python
    status: Literal[
        "pending", "verified", "edited", "all", "disagreement",
    ] = "pending",
```

- [ ] **Step 5: Run test, expect pass**

```
pytest tests/api/test_review_classification.py::test_classifications_accepts_uniform_status_values -v
```
Expected: PASS.

- [ ] **Step 6: Run the whole file to confirm no regression**

```
pytest tests/api/test_review_classification.py -v
```
Expected: all pass.

- [ ] **Step 7: Commit**

```
git add api/review/queries.py api/review/router.py tests/api/test_review_classification.py
git commit -m "feat(review): add uniform pending/verified/edited statuses to classification queue"
```

---

### Task 2: Add uniform status enum to markdown queue

**Files:**
- Modify: `api/review/queries.py:670-688` (`_markdown_where`)
- Modify: `api/review/router.py:509` (endpoint status enum)
- Create: `tests/api/test_review_markdown.py`

- [ ] **Step 1: Create the markdown test file with a failing test**

Create `tests/api/test_review_markdown.py`:

```python
"""Smoke + behavior tests for the markdown review endpoints."""
from __future__ import annotations
from datetime import datetime, timezone

import pytest


def _admin_header() -> dict[str, str]:
    from tests.api.conftest import auth_header  # type: ignore
    return auth_header(sub="admin-sub", email="admin@example.com")


def _ensure_admin_user() -> None:
    from api.neo4j_client import _users  # type: ignore[attr-defined]
    _users["admin-sub"] = {
        "supabase_id": "admin-sub",
        "email": "admin@example.com",
        "name": "Admin",
        "role": "admin",
        "enabled": True,
        "created_at": datetime.now(timezone.utc),
        "last_login_at": datetime.now(timezone.utc),
    }


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


def test_markdown_accepts_uniform_status_values(client) -> None:
    _ensure_admin_user()
    for s in ("pending", "verified", "edited", "all", "good", "bad", "unscored"):
        r = client.get(f"/review/markdown?status={s}", headers=_admin_header())
        assert r.status_code == 200, f"status={s} rejected: {r.text}"
```

- [ ] **Step 2: Run test, expect fail**

```
pytest tests/api/test_review_markdown.py::test_markdown_accepts_uniform_status_values -v
```
Expected: FAIL — `status=verified` and `status=edited` return 422.

- [ ] **Step 3: Update `MarkdownStatus` and `_markdown_where`**

Find `MarkdownStatus` in `api/review/queries.py` (just above `_markdown_where`, around line 668). Replace its definition:
```python
MarkdownStatus = Literal[
    "pending", "verified", "edited", "all",
    "good", "bad", "unscored",  # legacy — retire in Task 16
]
```

In `api/review/queries.py:670-688`, replace `_markdown_where`:
```python
def _markdown_where(status: MarkdownStatus, score_min: float | None) -> tuple[list[str], dict]:
    where = ["d.markdown IS NOT NULL", "d.markdown <> ''"]
    params: dict = {}
    if status == "pending":
        where.append("d.markdown_verified_at IS NULL")
    elif status == "verified":
        where.append("d.markdown_verified_at IS NOT NULL")
        where.append("d.markdown_quality = 'good'")
    elif status == "edited":
        where.append("d.markdown_verified_at IS NOT NULL")
        where.append("d.markdown_quality = 'bad'")
    elif status == "good":
        where.append("d.markdown_verified_at IS NOT NULL")
        where.append("d.markdown_quality = 'good'")
    elif status == "bad":
        where.append("d.markdown_verified_at IS NOT NULL")
        where.append("d.markdown_quality = 'bad'")
    elif status == "unscored":
        where.append("d.markdown_quality_score IS NULL")
    # "all" → no extra filter
    if score_min is not None:
        where.append("d.markdown_quality_score IS NOT NULL")
        where.append("d.markdown_quality_score >= $score_min")
        params["score_min"] = float(score_min)
    return where, params
```

- [ ] **Step 4: Extend the endpoint's `Literal` enum**

In `api/review/router.py:509`, change:
```python
    status: Literal["pending", "good", "bad", "unscored", "all"] = "pending",
```
to:
```python
    status: Literal[
        "pending", "verified", "edited", "all",
        "good", "bad", "unscored",
    ] = "pending",
```

- [ ] **Step 5: Run test, expect pass**

```
pytest tests/api/test_review_markdown.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```
git add api/review/queries.py api/review/router.py tests/api/test_review_markdown.py
git commit -m "feat(review): add uniform pending/verified/edited statuses to markdown queue"
```

---

### Task 3: Add `confidence_max` to classification queue + bulk-confirm

**Files:**
- Modify: `api/review/queries.py:375-468` (`list_classification_queue`), `:556-…` (`auto_confirm_classifications`)
- Modify: `api/review/router.py:439-457` (queue endpoint), `:460-471` (bulk-confirm), models `BulkConfirmBody`
- Test: `tests/api/test_review_classification.py`

- [ ] **Step 1: Add failing test for `confidence_max`**

Append to `tests/api/test_review_classification.py`:
```python
def test_classifications_accepts_confidence_max(client) -> None:
    _ensure_admin_user()
    r = client.get(
        "/review/classifications?confidence_min=0.5&confidence_max=0.9",
        headers=_admin_header(),
    )
    assert r.status_code == 200
```

- [ ] **Step 2: Run test, expect fail**

```
pytest tests/api/test_review_classification.py::test_classifications_accepts_confidence_max -v
```
Expected: FAIL — 422 (`confidence_max` is an unknown query param under strict-mode if applicable). If FastAPI silently ignores it, the test still drives the implementation.

- [ ] **Step 3: Add `confidence_max` to `list_classification_queue` signature + WHERE clause**

In `api/review/queries.py`, change the signature of `list_classification_queue` (around line 375) to:
```python
def list_classification_queue(
    status: ClassificationStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    agrees_only: bool = False,
) -> dict:
```

Right after the existing `if confidence_min is not None:` block (~line 417), add:
```python
    if confidence_max is not None:
        where.append("coalesce(d.notice_type_confidence, 0.0) <= $confidence_max")
        params["confidence_max"] = float(confidence_max)
```

- [ ] **Step 4: Add `confidence_max` to the queue endpoint**

In `api/review/router.py:439-457`, change `review_classifications` to accept and forward `confidence_max`:
```python
@router.get("/classifications", response_model=ClassificationQueueOut)
async def review_classifications(
    status: Literal[
        "pending", "verified", "edited", "all", "disagreement",
    ] = "pending",
    q_search: str | None = Query(default=None, alias="q", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
    confidence_min: float | None = Query(default=None, ge=0.0, le=1.0),
    confidence_max: float | None = Query(default=None, ge=0.0, le=1.0),
    agrees_only: bool = Query(default=False),
    _admin: UserOut = Depends(get_current_admin),
) -> ClassificationQueueOut:
    result = q.list_classification_queue(
        status=status, q=q_search, page=page, size=size,
        confidence_min=confidence_min, confidence_max=confidence_max,
        agrees_only=agrees_only,
    )
    rows = [ClassificationRow(**r) for r in result["rows"]]
    return ClassificationQueueOut(
        page=result["page"], size=result["size"],
        total=result["total"], rows=rows,
    )
```

- [ ] **Step 5: Add `confidence_max` to `BulkConfirmBody` + `auto_confirm_classifications`**

In `api/review/router.py:177-180`, change `BulkConfirmBody`:
```python
class BulkConfirmBody(BaseModel):
    confidence_min: float = Field(ge=0.0, le=1.0)
    confidence_max: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: str | None = Field(default=None, max_length=2000)
    dry_run: bool = False
```

Locate `auto_confirm_classifications` in `api/review/queries.py` (around line 556). Update its signature and Cypher:
```python
def auto_confirm_classifications(
    confidence_min: float,
    by_email: str,
    notes: str | None = None,
    dry_run: bool = False,
    confidence_max: float = 1.0,
) -> dict:
```
Wherever the function adds `WHERE coalesce(d.notice_type_confidence, 0.0) >= $min_conf`, also add the upper bound:
```
AND coalesce(d.notice_type_confidence, 0.0) <= $max_conf
```
And include `"max_conf": float(confidence_max)` in the params dict.

In `api/review/router.py:460-471`, forward `confidence_max`:
```python
@router.post("/classifications/bulk-confirm", response_model=BulkConfirmResult)
async def review_bulk_confirm(
    body: BulkConfirmBody,
    admin: UserOut = Depends(get_current_admin),
) -> BulkConfirmResult:
    result = q.auto_confirm_classifications(
        confidence_min=body.confidence_min,
        confidence_max=body.confidence_max,
        by_email=admin.email,
        notes=body.notes,
        dry_run=body.dry_run,
    )
    return BulkConfirmResult(**result)
```

- [ ] **Step 6: Run tests, expect pass**

```
pytest tests/api/test_review_classification.py -v
```
Expected: all pass.

- [ ] **Step 7: Commit**

```
git add api/review/queries.py api/review/router.py tests/api/test_review_classification.py
git commit -m "feat(review): add confidence_max filter to classification queue + bulk-confirm"
```

---

### Task 4: Add `score_max` to markdown queue + bulk-confirm

**Files:**
- Modify: `api/review/queries.py:670-688` (`_markdown_where`), `:731-789` (`list_markdown_queue`), `:830-…` (`auto_confirm_markdown`)
- Modify: `api/review/router.py:228-232` (`MarkdownBulkConfirmBody`), `:507-523` (queue), `:526-537` (bulk-confirm)
- Test: `tests/api/test_review_markdown.py`

- [ ] **Step 1: Failing test**

Append to `tests/api/test_review_markdown.py`:
```python
def test_markdown_accepts_score_max(client) -> None:
    _ensure_admin_user()
    r = client.get(
        "/review/markdown?score_min=50&score_max=80",
        headers=_admin_header(),
    )
    assert r.status_code == 200
```

- [ ] **Step 2: Run, expect fail**

```
pytest tests/api/test_review_markdown.py::test_markdown_accepts_score_max -v
```
Expected: FAIL.

- [ ] **Step 3: Add `score_max` to `_markdown_where`**

In `api/review/queries.py`, change the helper signature:
```python
def _markdown_where(
    status: MarkdownStatus,
    score_min: float | None,
    score_max: float | None = None,
) -> tuple[list[str], dict]:
```

Right after the `score_min` block, add:
```python
    if score_max is not None:
        where.append("d.markdown_quality_score IS NOT NULL")
        where.append("d.markdown_quality_score <= $score_max")
        params["score_max"] = float(score_max)
```

- [ ] **Step 4: Thread `score_max` through `list_markdown_queue`**

In `api/review/queries.py:731-789`, change `list_markdown_queue`:
```python
def list_markdown_queue(
    status: MarkdownStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    score_min: float | None = None,
    score_max: float | None = None,
) -> dict:
```
and update the call to `_markdown_where`:
```python
    where, params = _markdown_where(status, score_min, score_max)
```

- [ ] **Step 5: Thread `score_max` through `auto_confirm_markdown`**

In `api/review/queries.py:830-…` (`auto_confirm_markdown`), update the signature and both Cypher branches (dry-run + real) to add the upper bound:
```python
def auto_confirm_markdown(
    score_min: float,
    by_email: str,
    notes: str | None = None,
    dry_run: bool = False,
    score_max: float = 100.0,
) -> dict:
    params = {
        "min": float(score_min),
        "max": float(score_max),
        "by": by_email,
        "notes": notes,
    }
```
Each Cypher WHERE that currently has `AND d.markdown_quality_score >= $min` needs an extra line:
```
  AND d.markdown_quality_score <= $max
```

- [ ] **Step 6: Update the endpoints**

In `api/review/router.py:228-232`, change `MarkdownBulkConfirmBody`:
```python
class MarkdownBulkConfirmBody(BaseModel):
    score_min: float = Field(ge=0.0, le=100.0)
    score_max: float = Field(default=100.0, ge=0.0, le=100.0)
    notes: str | None = Field(default=None, max_length=2000)
    dry_run: bool = False
```

In `api/review/router.py:507-523`, change the queue endpoint:
```python
@router.get("/markdown", response_model=MarkdownQueueOut)
async def review_markdown_queue(
    status: Literal[
        "pending", "verified", "edited", "all",
        "good", "bad", "unscored",
    ] = "pending",
    q_search: str | None = Query(default=None, alias="q", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
    score_min: float | None = Query(default=None, ge=0.0, le=100.0),
    score_max: float | None = Query(default=None, ge=0.0, le=100.0),
    _admin: UserOut = Depends(get_current_admin),
) -> MarkdownQueueOut:
    result = q.list_markdown_queue(
        status=status, q=q_search, page=page, size=size,
        score_min=score_min, score_max=score_max,
    )
    rows = [MarkdownRow(**r) for r in result["rows"]]
    return MarkdownQueueOut(
        page=result["page"], size=result["size"],
        total=result["total"], rows=rows,
    )
```

In `api/review/router.py:526-537`, change `review_markdown_bulk_confirm` to forward `score_max`:
```python
    result = q.auto_confirm_markdown(
        score_min=body.score_min,
        score_max=body.score_max,
        by_email=admin.email,
        notes=body.notes,
        dry_run=body.dry_run,
    )
```

- [ ] **Step 7: Run tests, expect pass**

```
pytest tests/api/test_review_markdown.py -v
```
Expected: PASS.

- [ ] **Step 8: Commit**

```
git add api/review/queries.py api/review/router.py tests/api/test_review_markdown.py
git commit -m "feat(review): add score_max filter to markdown queue + bulk-confirm"
```

---

### Task 5: Add `notice_type` filter to every queue + stats endpoint

**Files:**
- Modify: `api/review/queries.py` — `list_queue`, `list_notice_queue`, `list_classification_queue`, `list_markdown_queue`, `stats`, `notice_stats` (if present), `classification_stats`, `markdown_stats`
- Modify: `api/review/router.py` — the six endpoints' signatures
- Test: both test files

- [ ] **Step 1: Failing tests for notice_type acceptance**

Append to `tests/api/test_review_classification.py`:
```python
def test_classifications_accepts_notice_type(client) -> None:
    _ensure_admin_user()
    for nt in ("all", "single", "multi", "unclassified"):
        r = client.get(f"/review/classifications?notice_type={nt}", headers=_admin_header())
        assert r.status_code == 200, f"notice_type={nt} rejected: {r.text}"
```

Append to `tests/api/test_review_markdown.py`:
```python
def test_markdown_accepts_notice_type(client) -> None:
    _ensure_admin_user()
    for nt in ("all", "single", "multi", "unclassified"):
        r = client.get(f"/review/markdown?notice_type={nt}", headers=_admin_header())
        assert r.status_code == 200, f"notice_type={nt} rejected: {r.text}"
```

- [ ] **Step 2: Run tests, expect fail**

```
pytest tests/api/test_review_classification.py::test_classifications_accepts_notice_type tests/api/test_review_markdown.py::test_markdown_accepts_notice_type -v
```
Expected: FAIL (or silently ignored — implement anyway).

- [ ] **Step 3: Add a shared `_notice_type_clause` helper**

At the top of `api/review/queries.py` (after the `ReviewStatus` literal, before `_sort_properties_by_markdown`), add:

```python
NoticeTypeFilter = Literal["all", "single", "multi", "unclassified"]


def _notice_type_clause(notice_type: NoticeTypeFilter | None,
                        alias: str = "d") -> str | None:
    """Return a Cypher snippet to filter on `<alias>.notice_type`, or None
    when no filter applies. `alias` lets callers reuse the helper whether
    the Document is bound as `d` or via a path like `(a)-[:HAS_DOCUMENT]->(d)`."""
    if notice_type in (None, "all"):
        return None
    if notice_type == "single":
        return f"{alias}.notice_type = 'single'"
    if notice_type == "multi":
        return f"{alias}.notice_type = 'multi'"
    if notice_type == "unclassified":
        return f"{alias}.notice_type IS NULL"
    raise ValueError(f"unknown notice_type filter: {notice_type!r}")
```

- [ ] **Step 4: Thread `notice_type` through every list/stats function**

For each of these queries in `api/review/queries.py`, add a `notice_type: NoticeTypeFilter | None = None` keyword arg and an extra WHERE-clause append driven by `_notice_type_clause`:

- `list_queue` (around :40): the property → notice join already lets us reference the Document via the existing OPTIONAL MATCH. Add a non-optional MATCH or filter via subquery — but the cheaper move is: add `OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(d:Document)` to the existing query and filter on `d.notice_type` only when the caller passed `notice_type`. If the caller passed `notice_type`, switch the join to a non-OPTIONAL MATCH so unclassified-filtered properties get dropped correctly. Concretely, refactor the function so it accepts `notice_type` and threads it into both the page query and the count query.
- `list_notice_queue`: notice rows already bind `d`, just append the clause to `where`.
- `list_classification_queue` (:375): bind `d`, append clause.
- `list_markdown_queue` (:731): same.
- `stats` (description), `classification_stats` (:471), `markdown_stats` (:691): each adds the clause to its single Cypher WHERE.

For `_markdown_where`, extend it to take `notice_type` so both queue + stats share the gate:
```python
def _markdown_where(
    status: MarkdownStatus,
    score_min: float | None,
    score_max: float | None = None,
    notice_type: NoticeTypeFilter | None = None,
) -> tuple[list[str], dict]:
    where = ["d.markdown IS NOT NULL", "d.markdown <> ''"]
    params: dict = {}
    # ... existing status branches ...
    # ... existing score_min / score_max blocks ...
    clause = _notice_type_clause(notice_type, alias="d")
    if clause:
        where.append(clause)
    return where, params
```

- [ ] **Step 5: Add `notice_type` query param to the six endpoints**

In `api/review/router.py`, every queue/stats endpoint takes:
```python
notice_type: Literal["all", "single", "multi", "unclassified"] = Query(default="all"),
```
and forwards it to the matching `queries.py` function. Apply to:
- `review_stats` (`:342`)
- `review_queue` (`:351`)
- `review_notices` (`:369`)
- `review_classifications` (`:439`)
- `review_classification_stats` (`:474`)
- `review_markdown_queue` (`:507`)
- `review_markdown_stats` (`:499`)

- [ ] **Step 6: Run tests, expect pass**

```
pytest tests/api/test_review_classification.py tests/api/test_review_markdown.py -v
```
Expected: all pass.

- [ ] **Step 7: Commit**

```
git add api/review/queries.py api/review/router.py tests/api/test_review_classification.py tests/api/test_review_markdown.py
git commit -m "feat(review): add notice_type filter to every queue + stats endpoint"
```

---

### Task 6: Uniform stats shape

**Files:**
- Modify: `api/review/router.py` — `ClassificationStats`, `MarkdownStats` Pydantic models, the three stats endpoints
- Modify: `api/review/queries.py` — `classification_stats`, `markdown_stats`
- Test: both test files

The spec requires every stats endpoint to expose `{ pending, verified, edited, total }`. The existing description-stage stats already match. Add the new fields to classification + markdown stats responses, keep the old fields for back-compat (UI will drop them in Task 11).

- [ ] **Step 1: Failing test for classification stats new fields**

Append to `tests/api/test_review_classification.py`:
```python
def test_classifications_stats_includes_edited(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/classifications/stats", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert "edited" in body
    assert "verified" in body
    assert "pending" in body
    assert "total" in body
```

Append the analogous test to `tests/api/test_review_markdown.py`:
```python
def test_markdown_stats_includes_edited(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/markdown/stats", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert "edited" in body
    assert "verified" in body
    assert "pending" in body
    assert "total" in body
```

- [ ] **Step 2: Run tests, expect fail**

```
pytest tests/api/test_review_classification.py::test_classifications_stats_includes_edited tests/api/test_review_markdown.py::test_markdown_stats_includes_edited -v
```
Expected: FAIL (`edited` missing).

- [ ] **Step 3: Extend `ClassificationStats` and the Cypher**

In `api/review/router.py:155-159`, change `ClassificationStats`:
```python
class ClassificationStats(BaseModel):
    total: int
    pending: int
    verified: int
    edited: int
    disagreement: int = 0  # legacy — drop in Task 16
```

In `api/review/queries.py:471-493`, change the Cypher in `classification_stats` to compute `verified` (overridden=false) and `edited` (overridden=true) separately:
```python
def classification_stats(
    notice_type: NoticeTypeFilter | None = None,
) -> dict:
    nt_clause = _notice_type_clause(notice_type, alias="d")
    nt_where = f" AND {nt_clause}" if nt_clause else ""
    rows = run_read_query(f"""
        MATCH (d:Document)
        WHERE d.notice_type IS NOT NULL{nt_where}
        RETURN
          count(*) AS total,
          sum(CASE WHEN d.notice_type_verified_at IS NULL THEN 1 ELSE 0 END) AS pending,
          sum(CASE WHEN d.notice_type_verified_at IS NOT NULL
                    AND coalesce(d.notice_type_overridden, false) = false
                   THEN 1 ELSE 0 END) AS verified,
          sum(CASE WHEN d.notice_type_verified_at IS NOT NULL
                    AND coalesce(d.notice_type_overridden, false) = true
                   THEN 1 ELSE 0 END) AS edited,
          sum(CASE WHEN d.notice_type_verified_at IS NULL
                    AND d.notice_type_classifier_pred IS NOT NULL
                    AND d.notice_type <> d.notice_type_classifier_pred
                   THEN 1 ELSE 0 END) AS disagreement
    """, max_rows=1)
    if not rows:
        return {"total": 0, "pending": 0, "verified": 0, "edited": 0, "disagreement": 0}
    r = rows[0]
    return {
        "total":        int(r.get("total") or 0),
        "pending":      int(r.get("pending") or 0),
        "verified":     int(r.get("verified") or 0),
        "edited":       int(r.get("edited") or 0),
        "disagreement": int(r.get("disagreement") or 0),
    }
```

- [ ] **Step 4: Extend `MarkdownStats` and the Cypher**

In `api/review/router.py:214-220`, change `MarkdownStats`:
```python
class MarkdownStats(BaseModel):
    total: int
    pending: int
    verified: int
    edited: int
    good: int = 0       # legacy — drop in Task 16
    bad: int = 0        # legacy — drop in Task 16
    unscored: int = 0   # legacy — drop in Task 16
    auto_confirmable: int = 0
```

In `api/review/queries.py:691-728`, change `markdown_stats` to add `verified` (quality='good') and `edited` (quality='bad'):
```python
def markdown_stats(
    score_min: float = 70.0,
    score_max: float = 100.0,
    notice_type: NoticeTypeFilter | None = None,
) -> dict:
    nt_clause = _notice_type_clause(notice_type, alias="d")
    nt_where = f" AND {nt_clause}" if nt_clause else ""
    rows = run_read_query(
        f"""
        MATCH (d:Document)
        WHERE d.markdown IS NOT NULL AND d.markdown <> ''{nt_where}
        RETURN
          count(*) AS total,
          sum(CASE WHEN d.markdown_verified_at IS NULL THEN 1 ELSE 0 END) AS pending,
          sum(CASE WHEN d.markdown_verified_at IS NOT NULL
                    AND d.markdown_quality = 'good' THEN 1 ELSE 0 END) AS verified,
          sum(CASE WHEN d.markdown_verified_at IS NOT NULL
                    AND d.markdown_quality = 'bad' THEN 1 ELSE 0 END) AS edited,
          sum(CASE WHEN d.markdown_verified_at IS NOT NULL
                    AND d.markdown_quality = 'good' THEN 1 ELSE 0 END) AS good,
          sum(CASE WHEN d.markdown_verified_at IS NOT NULL
                    AND d.markdown_quality = 'bad' THEN 1 ELSE 0 END) AS bad,
          sum(CASE WHEN d.markdown_quality_score IS NULL THEN 1 ELSE 0 END) AS unscored,
          sum(CASE WHEN d.markdown_verified_at IS NULL
                    AND d.markdown_quality_score IS NOT NULL
                    AND d.markdown_quality_score >= $score_min
                    AND d.markdown_quality_score <= $score_max
                   THEN 1 ELSE 0 END) AS auto_confirmable
        """,
        {"score_min": float(score_min), "score_max": float(score_max)},
        max_rows=1,
    )
    if not rows:
        return {
            "total": 0, "pending": 0, "verified": 0, "edited": 0,
            "good": 0, "bad": 0, "unscored": 0, "auto_confirmable": 0,
        }
    r = rows[0]
    return {
        "total":            int(r.get("total") or 0),
        "pending":          int(r.get("pending") or 0),
        "verified":         int(r.get("verified") or 0),
        "edited":           int(r.get("edited") or 0),
        "good":             int(r.get("good") or 0),
        "bad":              int(r.get("bad") or 0),
        "unscored":         int(r.get("unscored") or 0),
        "auto_confirmable": int(r.get("auto_confirmable") or 0),
    }
```

- [ ] **Step 5: Update the stats endpoints to forward `score_max` and `notice_type`**

In `api/review/router.py:499-504` (`review_markdown_stats`):
```python
@router.get("/markdown/stats", response_model=MarkdownStats)
async def review_markdown_stats(
    score_min: float = Query(default=70.0, ge=0.0, le=100.0),
    score_max: float = Query(default=100.0, ge=0.0, le=100.0),
    notice_type: Literal["all", "single", "multi", "unclassified"] = Query(default="all"),
    _admin: UserOut = Depends(get_current_admin),
) -> MarkdownStats:
    return MarkdownStats(**q.markdown_stats(
        score_min=score_min, score_max=score_max,
        notice_type=notice_type if notice_type != "all" else None,
    ))
```

In `api/review/router.py:474-478` (`review_classification_stats`):
```python
@router.get("/classifications/stats", response_model=ClassificationStats)
async def review_classification_stats(
    notice_type: Literal["all", "single", "multi", "unclassified"] = Query(default="all"),
    _admin: UserOut = Depends(get_current_admin),
) -> ClassificationStats:
    return ClassificationStats(**q.classification_stats(
        notice_type=notice_type if notice_type != "all" else None,
    ))
```

- [ ] **Step 6: Run tests**

```
pytest tests/api/test_review_classification.py tests/api/test_review_markdown.py -v
```
Expected: all pass.

- [ ] **Step 7: Commit**

```
git add api/review/queries.py api/review/router.py tests/api/test_review_classification.py tests/api/test_review_markdown.py
git commit -m "feat(review): unify stats response shape to {pending,verified,edited,total}"
```

---

## Phase 2 — Backend: by-property views

### Task 7: New `GET /review/classifications/by-property` endpoint

**Files:**
- Modify: `api/review/queries.py` — new function `list_classification_queue_by_property`
- Modify: `api/review/router.py` — new Pydantic model + endpoint
- Test: `tests/api/test_review_classification.py`

- [ ] **Step 1: Failing test**

Append:
```python
def test_classifications_by_property_routes_registered(client) -> None:
    from api.main import app
    paths = {r.path for r in app.routes}
    assert "/review/classifications/by-property" in paths


def test_classifications_by_property_returns_empty(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/classifications/by-property", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["rows"] == []
```

- [ ] **Step 2: Run, expect fail**

```
pytest tests/api/test_review_classification.py -v -k by_property
```
Expected: FAIL (404 — route not registered).

- [ ] **Step 3: Add the queries.py function**

Add to `api/review/queries.py` (after `list_classification_queue`):

```python
def list_classification_queue_by_property(
    status: ClassificationStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Return one row per AuctionProperty, projected with its Document's
    classification status. Filters mirror the by-document queue."""
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    skip = (page - 1) * size

    where = ["d.notice_type IS NOT NULL"]
    params: dict = {"skip": skip, "size": size}

    if status == "pending":
        where.append("d.notice_type_verified_at IS NULL")
    elif status == "verified":
        where.append("d.notice_type_verified_at IS NOT NULL")
        where.append("coalesce(d.notice_type_overridden, false) = false")
    elif status == "edited":
        where.append("d.notice_type_verified_at IS NOT NULL")
        where.append("coalesce(d.notice_type_overridden, false) = true")
    # "all" → no extra filter

    if confidence_min is not None:
        where.append("coalesce(d.notice_type_confidence, 0.0) >= $confidence_min")
        params["confidence_min"] = float(confidence_min)
    if confidence_max is not None:
        where.append("coalesce(d.notice_type_confidence, 0.0) <= $confidence_max")
        params["confidence_max"] = float(confidence_max)

    nt_clause = _notice_type_clause(notice_type, alias="d")
    if nt_clause:
        where.append(nt_clause)

    if q:
        where.append(
            "(toLower(coalesce(a.title, '')) CONTAINS toLower($q) "
            "OR toLower(coalesce(d.filename, '')) CONTAINS toLower($q))"
        )
        params["q"] = q

    if date_from:
        where.append("a.auction_start_dt >= date($date_from)")
        params["date_from"] = date_from
    if date_to:
        where.append("a.auction_start_dt <= date($date_to)")
        params["date_to"] = date_to

    where_clause = " AND ".join(where)
    cypher = f"""
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE {where_clause}
        RETURN a.auction_id                       AS auction_id,
               a.title                            AS title,
               toString(a.auction_start_dt)       AS auction_start,
               a.reserve_price                    AS reserve_price,
               d.filename                         AS notice_filename,
               d.notice_type                      AS notice_type,
               d.notice_type_confidence           AS notice_type_confidence,
               coalesce(d.notice_type_overridden, false) AS overridden,
               (d.notice_type_verified_at IS NOT NULL) AS verified,
               toString(d.notice_type_verified_at) AS verified_at
        ORDER BY verified ASC,
                 coalesce(a.auction_start_dt, date('9999-12-31')) ASC,
                 a.title ASC
        SKIP $skip LIMIT $size
    """
    rows = run_read_query(cypher, params, max_rows=size, timeout=30.0)

    count_cypher = f"""
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE {where_clause}
        RETURN count(a) AS total
    """
    count_rows = run_read_query(count_cypher, params, max_rows=1, timeout=30.0)
    total = count_rows[0]["total"] if count_rows else 0
    return {"page": page, "size": size, "total": total, "rows": rows}
```

- [ ] **Step 4: Add the Pydantic models + endpoint**

In `api/review/router.py`, near the other classification models, add:

```python
class ClassificationPropertyRow(BaseModel):
    auction_id: str
    title: str | None = None
    auction_start: str | None = None
    reserve_price: float | None = None
    notice_filename: str | None = None
    notice_type: str | None = None
    notice_type_confidence: float | None = None
    overridden: bool = False
    verified: bool = False
    verified_at: str | None = None


class ClassificationPropertyQueueOut(BaseModel):
    page: int
    size: int
    total: int
    rows: list[ClassificationPropertyRow]
```

Then add the endpoint (place it next to `review_classifications`):

```python
@router.get(
    "/classifications/by-property",
    response_model=ClassificationPropertyQueueOut,
)
async def review_classifications_by_property(
    status: Literal["pending", "verified", "edited", "all"] = "pending",
    q_search: str | None = Query(default=None, alias="q", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=100, ge=1, le=200),
    confidence_min: float | None = Query(default=None, ge=0.0, le=1.0),
    confidence_max: float | None = Query(default=None, ge=0.0, le=1.0),
    notice_type: Literal["all", "single", "multi", "unclassified"] = Query(default="all"),
    date_from: str | None = Query(default=None, max_length=20),
    date_to: str | None = Query(default=None, max_length=20),
    _admin: UserOut = Depends(get_current_admin),
) -> ClassificationPropertyQueueOut:
    result = q.list_classification_queue_by_property(
        status=status, q=q_search, page=page, size=size,
        confidence_min=confidence_min, confidence_max=confidence_max,
        notice_type=notice_type if notice_type != "all" else None,
        date_from=date_from, date_to=date_to,
    )
    rows = [ClassificationPropertyRow(**_row_to_str(r)) for r in result["rows"]]
    return ClassificationPropertyQueueOut(
        page=result["page"], size=result["size"],
        total=result["total"], rows=rows,
    )
```

- [ ] **Step 5: Run, expect pass**

```
pytest tests/api/test_review_classification.py -v -k by_property
```
Expected: PASS.

- [ ] **Step 6: Commit**

```
git add api/review/queries.py api/review/router.py tests/api/test_review_classification.py
git commit -m "feat(review): add /review/classifications/by-property endpoint"
```

---

### Task 8: New `GET /review/markdown/by-property` endpoint

**Files:**
- Modify: `api/review/queries.py` — new `list_markdown_queue_by_property`
- Modify: `api/review/router.py` — new model + endpoint
- Test: `tests/api/test_review_markdown.py`

- [ ] **Step 1: Failing test**

Append to `tests/api/test_review_markdown.py`:
```python
def test_markdown_by_property_routes_registered(client) -> None:
    from api.main import app
    paths = {r.path for r in app.routes}
    assert "/review/markdown/by-property" in paths


def test_markdown_by_property_returns_empty(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/markdown/by-property", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["rows"] == []
```

- [ ] **Step 2: Run, expect fail**

```
pytest tests/api/test_review_markdown.py -v -k by_property
```
Expected: FAIL (404).

- [ ] **Step 3: Add the queries function**

In `api/review/queries.py`, after `list_markdown_queue`:

```python
def list_markdown_queue_by_property(
    status: MarkdownStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    score_min: float | None = None,
    score_max: float | None = None,
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """One row per AuctionProperty projected with its Document's
    markdown-quality status."""
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    skip = (page - 1) * size

    where = ["d.markdown IS NOT NULL", "d.markdown <> ''"]
    params: dict = {"skip": skip, "size": size}

    if status == "pending":
        where.append("d.markdown_verified_at IS NULL")
    elif status == "verified":
        where.append("d.markdown_verified_at IS NOT NULL")
        where.append("d.markdown_quality = 'good'")
    elif status == "edited":
        where.append("d.markdown_verified_at IS NOT NULL")
        where.append("d.markdown_quality = 'bad'")
    # "all" → no extra filter

    if score_min is not None:
        where.append("d.markdown_quality_score IS NOT NULL")
        where.append("d.markdown_quality_score >= $score_min")
        params["score_min"] = float(score_min)
    if score_max is not None:
        where.append("d.markdown_quality_score IS NOT NULL")
        where.append("d.markdown_quality_score <= $score_max")
        params["score_max"] = float(score_max)

    nt_clause = _notice_type_clause(notice_type, alias="d")
    if nt_clause:
        where.append(nt_clause)

    if q:
        where.append(
            "(toLower(coalesce(a.title, '')) CONTAINS toLower($q) "
            "OR toLower(coalesce(d.filename, '')) CONTAINS toLower($q))"
        )
        params["q"] = q

    if date_from:
        where.append("a.auction_start_dt >= date($date_from)")
        params["date_from"] = date_from
    if date_to:
        where.append("a.auction_start_dt <= date($date_to)")
        params["date_to"] = date_to

    where_clause = " AND ".join(where)
    cypher = f"""
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE {where_clause}
        RETURN a.auction_id                       AS auction_id,
               a.title                            AS title,
               toString(a.auction_start_dt)       AS auction_start,
               a.reserve_price                    AS reserve_price,
               d.filename                         AS notice_filename,
               d.notice_type                      AS notice_type,
               d.markdown_quality_score           AS score,
               d.markdown_quality                 AS quality,
               (d.markdown_verified_at IS NOT NULL) AS verified,
               toString(d.markdown_verified_at)   AS verified_at
        ORDER BY verified ASC,
                 coalesce(d.markdown_quality_score, -1.0) ASC,
                 a.title ASC
        SKIP $skip LIMIT $size
    """
    rows = run_read_query(cypher, params, max_rows=size, timeout=30.0)
    count_cypher = f"""
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE {where_clause}
        RETURN count(a) AS total
    """
    count_rows = run_read_query(count_cypher, params, max_rows=1, timeout=30.0)
    total = count_rows[0]["total"] if count_rows else 0
    return {"page": page, "size": size, "total": total, "rows": rows}
```

- [ ] **Step 4: Add model + endpoint**

In `api/review/router.py`, near other markdown models, add:
```python
class MarkdownPropertyRow(BaseModel):
    auction_id: str
    title: str | None = None
    auction_start: str | None = None
    reserve_price: float | None = None
    notice_filename: str | None = None
    notice_type: str | None = None
    score: float | None = None
    quality: Literal["good", "bad"] | None = None
    verified: bool = False
    verified_at: str | None = None


class MarkdownPropertyQueueOut(BaseModel):
    page: int
    size: int
    total: int
    rows: list[MarkdownPropertyRow]
```

Then the endpoint:
```python
@router.get(
    "/markdown/by-property",
    response_model=MarkdownPropertyQueueOut,
)
async def review_markdown_by_property(
    status: Literal["pending", "verified", "edited", "all"] = "pending",
    q_search: str | None = Query(default=None, alias="q", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=100, ge=1, le=200),
    score_min: float | None = Query(default=None, ge=0.0, le=100.0),
    score_max: float | None = Query(default=None, ge=0.0, le=100.0),
    notice_type: Literal["all", "single", "multi", "unclassified"] = Query(default="all"),
    date_from: str | None = Query(default=None, max_length=20),
    date_to: str | None = Query(default=None, max_length=20),
    _admin: UserOut = Depends(get_current_admin),
) -> MarkdownPropertyQueueOut:
    result = q.list_markdown_queue_by_property(
        status=status, q=q_search, page=page, size=size,
        score_min=score_min, score_max=score_max,
        notice_type=notice_type if notice_type != "all" else None,
        date_from=date_from, date_to=date_to,
    )
    rows = [MarkdownPropertyRow(**_row_to_str(r)) for r in result["rows"]]
    return MarkdownPropertyQueueOut(
        page=result["page"], size=result["size"],
        total=result["total"], rows=rows,
    )
```

- [ ] **Step 5: Run, expect pass**

```
pytest tests/api/test_review_markdown.py -v -k by_property
```
Expected: PASS.

- [ ] **Step 6: Commit**

```
git add api/review/queries.py api/review/router.py tests/api/test_review_markdown.py
git commit -m "feat(review): add /review/markdown/by-property endpoint"
```

---

## Phase 3 — Frontend: toolbar restructure

> **Note:** frontend tasks have no automated tests. After each task, **manually load `web/review.html`** in the browser (or whatever local-dev mechanism the project uses) and confirm the toolbar renders and the named pills are clickable. Capture a screenshot before committing if practical.

### Task 9: Replace flat group pills with stage tabs + sub-group pills

**File:**
- Modify: `web/review.html:319-358` (the toolbar markup)

- [ ] **Step 1: Replace the group/status rows**

Locate `web/review.html:319-358` (the top of `<section id="screen-queue">`). Replace lines 320-358 with:

```html
    <div class="actions">
      <span>Stage:</span>
      <span class="group-toggle" id="stage-tabs">
        <button class="pill" data-stage="classification">classification</button>
        <button class="pill" data-stage="markdown">markdown</button>
        <button class="pill" data-stage="description">description</button>
      </span>
    </div>
    <div class="actions">
      <span>Group:</span>
      <span class="group-toggle" id="group-tabs">
        <button class="pill" data-group="property">property</button>
        <button class="pill" data-group="notice">sales notice</button>
      </span>
      <span style="width:14px"></span>
      <span>Status:</span>
      <span id="status-buttons" class="group-toggle">
        <button class="pill" data-status="pending">pending</button>
        <button class="pill" data-status="verified">verified</button>
        <button class="pill" data-status="edited">edited</button>
        <button class="pill" data-status="all">all</button>
      </span>
      <span id="cview-toggle" class="group-toggle hidden">
        <span style="margin:0 4px; color: var(--muted);">·</span>
        <span>View:</span>
        <button class="pill" data-cview="gallery">gallery</button>
        <button class="pill" data-cview="cards">cards</button>
      </span>
      <input type="search" id="q" placeholder="search title or borrower">
      <button id="reload" class="primary">Reload</button>
    </div>
    <div class="actions">
      <span>Auction date:</span>
      <label class="meta" style="display:flex; gap:6px; align-items:center;">
        from <input type="date" id="date-from" style="padding:4px 6px; border:1.5px solid var(--ink);">
      </label>
      <label class="meta" style="display:flex; gap:6px; align-items:center;">
        to <input type="date" id="date-to" style="padding:4px 6px; border:1.5px solid var(--ink);">
      </label>
      <button id="date-clear" class="ghost" title="show all dates">clear dates</button>
      <span class="meta" id="date-hint" style="color: var(--muted);"></span>
    </div>
    <div class="actions" id="score-bar">
      <span>Score:</span>
      <label class="meta" style="display:flex; gap:6px; align-items:center;">
        from <input type="number" id="score-from" min="0" max="100" step="1" value="0"
                    style="width:60px; padding:4px 6px; border:1.5px solid var(--ink);">
      </label>
      <label class="meta" style="display:flex; gap:6px; align-items:center;">
        to <input type="number" id="score-to" min="0" max="100" step="1" value="100"
                  style="width:60px; padding:4px 6px; border:1.5px solid var(--ink);">
      </label>
      <button id="score-clear" class="ghost" title="reset to 0–100">clear</button>
      <span class="meta" id="score-hint" style="color: var(--muted);"></span>
    </div>
    <div class="actions" id="notice-type-bar">
      <span>Notice type:</span>
      <span class="group-toggle" id="notice-type-tabs">
        <button class="pill" data-ntype="all">all</button>
        <button class="pill" data-ntype="single">single</button>
        <button class="pill" data-ntype="multi">multi</button>
        <button class="pill" data-ntype="unclassified">unclassified</button>
      </span>
    </div>
    <div id="bulk-confirm-bar" class="actions hidden">
      <span class="meta" id="bulk-confirm-hint" style="color: var(--muted);">verifies pending items in the current filtered range</span>
      <span style="flex:1"></span>
      <button id="bulk-confirm-btn" class="primary" disabled>Confirm all 0 in range</button>
    </div>
```

This deletes the old `auto-confirm-bar` and `md-confirm-bar` sliders + the three status-buttons-{desc,class,md} rows; a single `bulk-confirm-bar` replaces both.

- [ ] **Step 2: Manually verify the toolbar renders**

Open `web/review.html` in the browser. Confirm: three stage pills visible, two group pills, four status pills, a score row with two number inputs defaulting to 0 / 100, and four notice-type pills. Existing scripts will be broken — that's fine, we wire them up in the next task. The browser console will show errors referencing the removed IDs; that's expected.

- [ ] **Step 3: Commit**

```
git add web/review.html
git commit -m "feat(review): toolbar markup for stage/group/status/score/notice-type rows"
```

---

### Task 10: Refactor `queueState` and `syncStatusButtons`

**File:**
- Modify: `web/review.html:646-706` (`queueState` declaration + `syncStatusButtons`)

- [ ] **Step 1: Replace `queueState`**

Locate `web/review.html:652-661` (the `queueState = { … }` block) and replace with:

```javascript
  let queueState = {
    stage: 'description',         // 'classification' | 'markdown' | 'description'
    group: 'property',            // 'property' | 'notice'
    status: 'pending',            // 'pending' | 'verified' | 'edited' | 'all'
    cview: (savedCView === 'cards' ? 'cards' : 'gallery'),
    noticeType: 'all',            // 'all' | 'single' | 'multi' | 'unclassified'
    q: '',
    dateFrom: todayISO(), dateTo: '',
    scoreFrom: 0, scoreTo: 100,
    page: 1,
  };
  // Per-stage default group (remembers reviewer's last choice in localStorage).
  const DEFAULT_GROUP_FOR_STAGE = {
    classification: 'notice',
    markdown: 'notice',
    description: 'property',
  };
```

Also remove the now-dead `autoConfMin` and `mdScoreThreshold` keys (they're gone in the snippet above) and the `mdStatsAutoConfirmable` global (line 662).

- [ ] **Step 2: Replace `syncStatusButtons`**

In `web/review.html:668-706`, replace `syncStatusButtons` with:

```javascript
  function syncStatusButtons() {
    $$('button.pill[data-stage]').forEach(b => {
      b.classList.toggle('on', b.getAttribute('data-stage') === queueState.stage);
    });
    $$('button.pill[data-group]').forEach(b => {
      b.classList.toggle('on', b.getAttribute('data-group') === queueState.group);
    });
    $$('button.pill[data-status]').forEach(b => {
      b.classList.toggle('on', b.getAttribute('data-status') === queueState.status);
    });
    $$('button.pill[data-ntype]').forEach(b => {
      b.classList.toggle('on', b.getAttribute('data-ntype') === queueState.noticeType);
    });
    $$('button.pill[data-cview]').forEach(b => {
      b.classList.toggle('on', b.getAttribute('data-cview') === queueState.cview);
    });

    const isClass = queueState.stage === 'classification';
    const isMd = queueState.stage === 'markdown';
    const isDesc = queueState.stage === 'description';
    const isProperty = queueState.group === 'property';
    const isNotice = queueState.group === 'notice';

    // Body panels — description reuses the legacy IDs; classification + markdown
    // now have an extra "by-property" panel that we'll wire up in later tasks.
    $('queue-by-property').classList.toggle('hidden', !(isDesc && isProperty));
    $('queue-by-notice').classList.toggle('hidden', !(isDesc && isNotice));
    $('queue-by-classification').classList.toggle('hidden',
        !(isClass && isNotice && queueState.cview === 'cards'));
    $('queue-by-classification-gallery').classList.toggle('hidden',
        !(isClass && isNotice && queueState.cview === 'gallery'));
    $('queue-by-markdown').classList.toggle('hidden', !(isMd && isNotice));

    // The classification stage has a gallery/cards toggle, but only when
    // looking at the sales-notice sub-group.
    $('cview-toggle').classList.toggle('hidden', !(isClass && isNotice));

    // Score row visibility — description has no score.
    $('score-bar').classList.toggle('hidden', isDesc);

    // Bulk-confirm only in classification + markdown.
    $('bulk-confirm-bar').classList.toggle('hidden', isDesc);
  }
```

- [ ] **Step 3: Update `loadQueue` to route on `stage` + `group`**

Replace `loadQueue` (`web/review.html:719-743`):

```javascript
  async function loadQueue(append = false) {
    if (!append) {
      queueState.page = 1;
      queueLoadedCount = 0;
      queueTotal = 0;
    }
    syncStatusButtons();
    syncDateInputs();
    syncScoreInputs();
    if (queueState.stage === 'classification') {
      if (queueState.group === 'property') {
        await loadClassificationByPropertyQueue(append);      // added in Task 14
      } else if (queueState.cview === 'gallery') {
        await loadGalleryQueue(append);
      } else {
        await loadClassificationQueue(append);
      }
    } else if (queueState.stage === 'markdown') {
      if (queueState.group === 'property') {
        await loadMarkdownByPropertyQueue(append);            // added in Task 14
      } else {
        await loadMarkdownQueue(append);
      }
    } else {
      // description stage
      if (queueState.group === 'notice') {
        await loadNoticeQueue(append);
      } else {
        await loadPropertyQueue(append);
      }
    }
    updateLoadMore();
    updateBulkConfirmButton();
  }
```

For now, define empty stubs so switching to a not-yet-implemented panel doesn't throw. Task 14 replaces these with real implementations:
```javascript
  async function loadClassificationByPropertyQueue(append) {
    // Implemented in Task 14.
  }
  async function loadMarkdownByPropertyQueue(append) {
    // Implemented in Task 14.
  }
  function syncScoreInputs() {
    $('score-from').value = String(queueState.scoreFrom);
    $('score-to').value = String(queueState.scoreTo);
    const def = queueState.scoreFrom === 0 && queueState.scoreTo === 100;
    $('score-hint').textContent = def ? '(no score filter)' : '';
  }
  function updateBulkConfirmButton() {
    const btn = $('bulk-confirm-btn');
    if (!btn) return;
    const active = queueState.stage === 'classification' || queueState.stage === 'markdown';
    const n = active ? (queueTotal || 0) : 0;
    btn.textContent = `Confirm all ${n} in range`;
    btn.disabled = !active || n === 0 || queueState.status !== 'pending';
  }
```

The two `// TODO Task 14` stubs are removed in Task 14.

- [ ] **Step 4: Remove the dead helpers `updateAutoConfirmButton` and `updateMdConfirmButton`**

Delete `updateAutoConfirmButton` (`web/review.html:745-752`) and `updateMdConfirmButton` (`web/review.html:754-761`). Search the file for remaining call sites and replace each with `updateBulkConfirmButton()`.

- [ ] **Step 5: Manually verify the page boots without console errors**

Reload `web/review.html`. Switch between stage pills — the body panels should hide/show correctly. Description→property shows the existing property table; description→notice shows the existing card list; markdown→notice shows the markdown queue; classification→notice shows gallery or cards depending on toggle. Property views inside classification + markdown are empty (those are wired in Task 14).

- [ ] **Step 6: Commit**

```
git add web/review.html
git commit -m "feat(review): refactor queueState and panel routing for stage/group model"
```

---

### Task 11: Wire up pill handlers, hash router, and notice-type filter

**File:**
- Modify: `web/review.html` — the click handlers and hash parser (search for `addEventListener('click'`, `parseHash`, `route`, the `data-group` handler block)

- [ ] **Step 1: Replace the existing group/status click handlers**

Find the click handlers wired off `button.pill[data-group]`, `data-status`, `data-cstatus`, `data-mdstatus` (search for `data-group` near line 1660-1700; there are several handler blocks). Replace ALL of those handler blocks with:

```javascript
  $$('button.pill[data-stage]').forEach(b => {
    b.addEventListener('click', () => {
      const next = b.getAttribute('data-stage');
      if (queueState.stage === next) return;
      queueState.stage = next;
      queueState.group = DEFAULT_GROUP_FOR_STAGE[next] || 'property';
      try {
        const saved = localStorage.getItem('reviewSubgroup:' + next);
        if (saved) queueState.group = saved;
      } catch {}
      writeHash();
      loadStats(); loadQueue();
    });
  });

  $$('button.pill[data-group]').forEach(b => {
    b.addEventListener('click', () => {
      const next = b.getAttribute('data-group');
      if (queueState.group === next) return;
      queueState.group = next;
      try { localStorage.setItem('reviewSubgroup:' + queueState.stage, next); } catch {}
      writeHash();
      loadQueue();
    });
  });

  $$('button.pill[data-status]').forEach(b => {
    b.addEventListener('click', () => {
      const next = b.getAttribute('data-status');
      if (queueState.status === next) return;
      queueState.status = next;
      writeHash();
      loadQueue();
    });
  });

  $$('button.pill[data-ntype]').forEach(b => {
    b.addEventListener('click', () => {
      const next = b.getAttribute('data-ntype');
      if (queueState.noticeType === next) return;
      queueState.noticeType = next;
      writeHash();
      loadStats(); loadQueue();
    });
  });

  $$('button.pill[data-cview]').forEach(b => {
    b.addEventListener('click', () => {
      const next = b.getAttribute('data-cview');
      if (queueState.cview === next) return;
      queueState.cview = next;
      try { localStorage.setItem('cView', next); } catch {}
      writeHash();
      loadQueue();
    });
  });
```

- [ ] **Step 2: Add score-from/to input handlers**

Add (near the date input handlers, search for `$('date-from').addEventListener`):

```javascript
  $('score-from').addEventListener('change', (e) => {
    const v = parseInt(e.target.value, 10);
    queueState.scoreFrom = isNaN(v) ? 0 : Math.max(0, Math.min(100, v));
    writeHash();
    loadStats(); loadQueue();
  });
  $('score-to').addEventListener('change', (e) => {
    const v = parseInt(e.target.value, 10);
    queueState.scoreTo = isNaN(v) ? 100 : Math.max(0, Math.min(100, v));
    writeHash();
    loadStats(); loadQueue();
  });
  $('score-clear').addEventListener('click', () => {
    queueState.scoreFrom = 0;
    queueState.scoreTo = 100;
    writeHash();
    loadStats(); loadQueue();
  });
```

- [ ] **Step 3: Replace `parseHash` and add `writeHash`**

Find `parseHash` (around line 576). Replace it and add a `writeHash`:

```javascript
  function parseHash() {
    const h = (location.hash || '').replace(/^#/, '');
    if (h.startsWith('detail/'))  return { screen: 'detail',   id: decodeURIComponent(h.slice('detail/'.length)) };
    if (h.startsWith('notice/'))  return { screen: 'annotator', filename: decodeURIComponent(h.slice('notice/'.length)) };
    if (h.startsWith('gallery/')) return { screen: 'queue', gallery: decodeURIComponent(h.slice('gallery/'.length)) };

    // New hash format: kv-pairs separated by `&`.
    const p = new URLSearchParams(h);
    if (p.has('stage')) {
      queueState.stage      = p.get('stage')      || queueState.stage;
      queueState.group      = p.get('group')      || queueState.group;
      queueState.status     = p.get('status')     || queueState.status;
      queueState.noticeType = p.get('notice_type')|| queueState.noticeType;
      queueState.dateFrom   = p.get('date_from')  ?? queueState.dateFrom;
      queueState.dateTo     = p.get('date_to')    ?? queueState.dateTo;
      const sf = parseInt(p.get('score_from'), 10);
      const st = parseInt(p.get('score_to'),   10);
      if (!isNaN(sf)) queueState.scoreFrom = sf;
      if (!isNaN(st)) queueState.scoreTo   = st;
    }
    return { screen: 'queue' };
  }

  function writeHash() {
    const p = new URLSearchParams();
    p.set('stage', queueState.stage);
    p.set('group', queueState.group);
    p.set('status', queueState.status);
    p.set('notice_type', queueState.noticeType);
    if (queueState.dateFrom) p.set('date_from', queueState.dateFrom);
    if (queueState.dateTo)   p.set('date_to',   queueState.dateTo);
    if (queueState.scoreFrom !== 0)   p.set('score_from', String(queueState.scoreFrom));
    if (queueState.scoreTo   !== 100) p.set('score_to',   String(queueState.scoreTo));
    const next = '#' + p.toString();
    if (location.hash !== next) {
      history.replaceState(null, '', next);
    }
  }
```

- [ ] **Step 4: Update the queue request builders to send `notice_type` and `score_*`**

Find `appendDateParams` (around line 570) and add a sibling helper, then ensure every queue/stats fetch calls both:

```javascript
  function appendCommonParams(params) {
    if (queueState.dateFrom) params.set('date_from', queueState.dateFrom);
    if (queueState.dateTo)   params.set('date_to',   queueState.dateTo);
    if (queueState.noticeType && queueState.noticeType !== 'all') {
      params.set('notice_type', queueState.noticeType);
    }
  }
  function appendScoreParams(params) {
    // The classification API takes 0–1 floats; markdown API takes 0–100.
    if (queueState.stage === 'classification') {
      if (queueState.scoreFrom !== 0)   params.set('confidence_min', String(queueState.scoreFrom / 100));
      if (queueState.scoreTo   !== 100) params.set('confidence_max', String(queueState.scoreTo   / 100));
    } else if (queueState.stage === 'markdown') {
      if (queueState.scoreFrom !== 0)   params.set('score_min', String(queueState.scoreFrom));
      if (queueState.scoreTo   !== 100) params.set('score_max', String(queueState.scoreTo));
    }
  }
```

Then call `appendCommonParams(params)` and (for classification + markdown queue/stats) `appendScoreParams(params)` in every `URLSearchParams`-builder near the queue/stats fetches (search for `URLSearchParams`).

Existing call sites of `appendDateParams` can stay; just add `appendCommonParams` and `appendScoreParams` calls next to them. After the migration, `appendDateParams` is a subset of `appendCommonParams` — collapse the call sites to one helper.

- [ ] **Step 5: Manually verify**

Reload. Click each pill — the URL hash should update, panels should swap, score inputs should accept values and trigger a reload. The `notice_type` pill row should also act. The browser network tab should show `notice_type=multi` etc. being sent.

- [ ] **Step 6: Commit**

```
git add web/review.html
git commit -m "feat(review): pill handlers + hash router + score from/to + notice-type filter"
```

---

### Task 12: Wire bulk-confirm to send min + max

**File:**
- Modify: `web/review.html` — search for `auto-conf-bulk` and `md-conf-bulk`

- [ ] **Step 1: Add the unified click handler**

Find the existing `auto-conf-bulk` click handler (search `'auto-conf-bulk'`). Replace BOTH the classification + markdown bulk-confirm click handlers (the legacy IDs may have been deleted in Task 9 — if so, just add this one):

```javascript
  $('bulk-confirm-btn').addEventListener('click', async () => {
    const stage = queueState.stage;
    if (stage !== 'classification' && stage !== 'markdown') return;

    const url = API + (stage === 'classification'
      ? '/review/classifications/bulk-confirm'
      : '/review/markdown/bulk-confirm');

    const body = stage === 'classification'
      ? {
          confidence_min: queueState.scoreFrom / 100,
          confidence_max: queueState.scoreTo   / 100,
          dry_run: false,
        }
      : {
          score_min: queueState.scoreFrom,
          score_max: queueState.scoreTo,
          dry_run: false,
        };

    const r = await authFetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) { alert('bulk-confirm failed'); return; }
    const out = await r.json();
    await loadStats();
    await loadQueue();
    alert(`Confirmed ${out.count} ${stage} items.`);
  });
```

- [ ] **Step 2: Delete the old slider listeners**

Search for `auto-conf-slider` and `md-conf-slider`. Delete the entire `addEventListener('input', …)` blocks for both. Also delete any remaining references to `queueState.autoConfMin` and `queueState.mdScoreThreshold`.

- [ ] **Step 3: Manually verify**

Reload, pick classification stage with status=pending. The "Confirm all N in range" button should show the current pending count. Score inputs to a narrow range — the queue + button count should both shrink. Clicking the button should POST with `confidence_min` and `confidence_max` in the body (verify in the network tab) — but DO NOT actually click confirm against real data unless that's the intent.

- [ ] **Step 4: Commit**

```
git add web/review.html
git commit -m "feat(review): unify bulk-confirm to send {min,max} via the score from/to range"
```

---

### Task 13: Unified stats rendering across stages

**File:**
- Modify: `web/review.html` — `loadStats` and `loadMarkdownStats` (around lines 612-643)

- [ ] **Step 1: Replace `loadStats` with a single function that routes on stage**

Replace lines 611-643 with:

```javascript
  async function loadStats() {
    const params = new URLSearchParams();
    appendCommonParams(params);
    let url;
    if (queueState.stage === 'classification') {
      url = API + '/review/classifications/stats';
    } else if (queueState.stage === 'markdown') {
      url = API + '/review/markdown/stats';
      appendScoreParams(params);  // markdown stats accept score_min/max
    } else {
      url = API + '/review/stats';
    }
    if (params.toString()) url += '?' + params.toString();
    const r = await authFetch(url);
    if (!r.ok) { $('stats').textContent = ''; return; }
    const s = await r.json();
    $('stats').innerHTML = `
      <span class="pill warn">${s.pending} pending</span>
      <span class="pill good">${s.verified} verified</span>
      <span class="pill good">${s.edited} edited</span>
      <span class="pill off">${s.total} total</span>
    `;
  }
```

Delete the now-unreferenced `loadMarkdownStats` function and the `mdStatsAutoConfirmable` global if any callers remain.

- [ ] **Step 2: Manually verify**

Reload, cycle through all three stages — the stats pill row should show 4 pills (pending, verified, edited, total) for each.

- [ ] **Step 3: Commit**

```
git add web/review.html
git commit -m "feat(review): uniform 4-pill stats rendering across all stages"
```

---

### Task 14: Render the new by-property views

**File:**
- Modify: `web/review.html` — the stub `loadClassificationByPropertyQueue` + `loadMarkdownByPropertyQueue` from Task 10 + add render helpers

- [ ] **Step 1: Add a DOM panel for each by-property view**

In `web/review.html`, locate the body-panel block (`<div id="queue-by-property">` through `<div id="queue-by-markdown">` — was lines 388-408 pre-change). Insert two more sibling divs immediately after the existing `<div id="queue-by-markdown" …></div>`:

```html
    <div id="queue-by-classification-by-property" class="table-scroll hidden">
      <table>
        <thead>
          <tr>
            <th>Title</th>
            <th>Auction date</th>
            <th>Reserve (₹)</th>
            <th>Notice</th>
            <th>Predicted type</th>
            <th>Confidence</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="queue-by-classification-by-property-rows"></tbody>
      </table>
    </div>
    <div id="queue-by-markdown-by-property" class="table-scroll hidden">
      <table>
        <thead>
          <tr>
            <th>Title</th>
            <th>Auction date</th>
            <th>Reserve (₹)</th>
            <th>Notice</th>
            <th>Markdown score</th>
            <th>Quality</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="queue-by-markdown-by-property-rows"></tbody>
      </table>
    </div>
```

- [ ] **Step 2: Update `syncStatusButtons` to toggle the new panels**

In the `syncStatusButtons` body, after the existing panel toggles, add:

```javascript
    $('queue-by-classification-by-property').classList.toggle('hidden',
        !(isClass && isProperty));
    $('queue-by-markdown-by-property').classList.toggle('hidden',
        !(isMd && isProperty));
```

- [ ] **Step 3: Implement `loadClassificationByPropertyQueue`**

Replace the stub from Task 10 with:

```javascript
  async function loadClassificationByPropertyQueue(append) {
    const tbody = $('queue-by-classification-by-property-rows');
    if (!append) tbody.innerHTML = '<tr><td colspan="7"><div class="empty">loading…</div></td></tr>';
    const params = new URLSearchParams({
      status: queueState.status,
      page: String(queueState.page),
      size: String(PAGE_SIZE_PROPERTY),
    });
    if (queueState.q) params.set('q', queueState.q);
    appendCommonParams(params);
    appendScoreParams(params);
    const r = await authFetch(API + '/review/classifications/by-property?' + params.toString());
    if (!r.ok) { tbody.innerHTML = '<tr><td colspan="7"><div class="empty">error</div></td></tr>'; return; }
    const data = await r.json();
    queueTotal = data.total;
    if (!append) tbody.innerHTML = '';
    for (const row of data.rows) {
      const tr = document.createElement('tr');
      tr.className = row.verified ? (row.overridden ? 'edited' : 'verified') : 'pending';
      const statusLabel = row.verified ? (row.overridden ? 'edited' : 'verified') : 'pending';
      const conf = row.notice_type_confidence == null ? '—' : (Math.round(row.notice_type_confidence * 100) + '%');
      const noticeLink = row.notice_filename
        ? `<a href="#notice/${encodeURIComponent(row.notice_filename)}">${row.notice_filename}</a>`
        : '—';
      tr.innerHTML = `
        <td><a href="#detail/${encodeURIComponent(row.auction_id)}">${row.title || row.auction_id}</a></td>
        <td>${row.auction_start || '—'}</td>
        <td>${row.reserve_price == null ? '—' : row.reserve_price.toLocaleString('en-IN')}</td>
        <td>${noticeLink}</td>
        <td>${row.notice_type || '—'}</td>
        <td>${conf}</td>
        <td>${statusLabel}</td>
      `;
      tbody.appendChild(tr);
    }
    queueLoadedCount += data.rows.length;
  }
```

- [ ] **Step 4: Implement `loadMarkdownByPropertyQueue`**

```javascript
  async function loadMarkdownByPropertyQueue(append) {
    const tbody = $('queue-by-markdown-by-property-rows');
    if (!append) tbody.innerHTML = '<tr><td colspan="7"><div class="empty">loading…</div></td></tr>';
    const params = new URLSearchParams({
      status: queueState.status,
      page: String(queueState.page),
      size: String(PAGE_SIZE_PROPERTY),
    });
    if (queueState.q) params.set('q', queueState.q);
    appendCommonParams(params);
    appendScoreParams(params);
    const r = await authFetch(API + '/review/markdown/by-property?' + params.toString());
    if (!r.ok) { tbody.innerHTML = '<tr><td colspan="7"><div class="empty">error</div></td></tr>'; return; }
    const data = await r.json();
    queueTotal = data.total;
    if (!append) tbody.innerHTML = '';
    for (const row of data.rows) {
      const tr = document.createElement('tr');
      const statusLabel = row.verified
        ? (row.quality === 'bad' ? 'edited' : 'verified')
        : 'pending';
      tr.className = statusLabel;
      const noticeLink = row.notice_filename
        ? `<a href="#notice/${encodeURIComponent(row.notice_filename)}">${row.notice_filename}</a>`
        : '—';
      tr.innerHTML = `
        <td><a href="#detail/${encodeURIComponent(row.auction_id)}">${row.title || row.auction_id}</a></td>
        <td>${row.auction_start || '—'}</td>
        <td>${row.reserve_price == null ? '—' : row.reserve_price.toLocaleString('en-IN')}</td>
        <td>${noticeLink}</td>
        <td>${row.score == null ? '—' : Math.round(row.score)}</td>
        <td>${row.quality || '—'}</td>
        <td>${statusLabel}</td>
      `;
      tbody.appendChild(tr);
    }
    queueLoadedCount += data.rows.length;
  }
```

- [ ] **Step 5: Manually verify**

Reload. Switch to classification → property; the new property-row table should fetch + render. Same for markdown → property. Both should respect status/date/score/notice-type filters.

- [ ] **Step 6: Commit**

```
git add web/review.html
git commit -m "feat(review): by-property views inside classification + markdown stages"
```

---

## Phase 4 — Cleanup

### Task 15: Retire legacy enum values + dead UI code

**Files:**
- Modify: `api/review/queries.py` — drop legacy `disagreement`, `good`, `bad`, `unscored` branches from `list_classification_queue`, `list_markdown_queue`, `_markdown_where`
- Modify: `api/review/router.py` — drop legacy values from the `Literal` enums; drop legacy fields from `ClassificationStats` + `MarkdownStats`
- Modify: `tests/api/test_review_classification.py` + `tests/api/test_review_markdown.py` — drop legacy assertions; assert that legacy values now return 422
- Modify: `web/review.html` — final search for any leftover refs to `cstatus`, `mdstatus`, `autoConfMin`, `mdScoreThreshold`, `auto-conf-bulk`, `md-conf-bulk`

- [ ] **Step 1: Add failing tests for legacy rejection**

Append to `tests/api/test_review_classification.py`:
```python
def test_classifications_rejects_legacy_status(client) -> None:
    _ensure_admin_user()
    for s in ("disagreement", "auto-confirm"):
        r = client.get(f"/review/classifications?status={s}", headers=_admin_header())
        assert r.status_code == 422, f"status={s} should be rejected"
```

Append to `tests/api/test_review_markdown.py`:
```python
def test_markdown_rejects_legacy_status(client) -> None:
    _ensure_admin_user()
    for s in ("good", "bad", "unscored"):
        r = client.get(f"/review/markdown?status={s}", headers=_admin_header())
        assert r.status_code == 422, f"status={s} should be rejected"
```

Also remove the parts of `test_classifications_accepts_uniform_status_values` and `test_markdown_accepts_uniform_status_values` that asserted the legacy values still succeed.

- [ ] **Step 2: Run, expect fail**

```
pytest tests/api/test_review_classification.py tests/api/test_review_markdown.py -v -k "rejects_legacy or accepts_uniform"
```
Expected: FAIL — legacy values are still accepted.

- [ ] **Step 3: Drop legacy enum values**

In `api/review/queries.py`, change `ClassificationStatus` back to the four canonical values:
```python
ClassificationStatus = Literal["pending", "verified", "edited", "all"]
```
Drop the `elif status == "disagreement"` branch in `list_classification_queue`.

Change `MarkdownStatus` to:
```python
MarkdownStatus = Literal["pending", "verified", "edited", "all"]
```
In `_markdown_where`, drop the `good`, `bad`, `unscored` branches.

In `api/review/router.py`, every endpoint's `Literal[…]` for these statuses becomes the canonical four-value form. Drop:
- `disagreement` from `review_classifications`
- `good`, `bad`, `unscored` from `review_markdown_queue`

- [ ] **Step 4: Drop legacy stats fields**

`api/review/router.py` — change `ClassificationStats`:
```python
class ClassificationStats(BaseModel):
    total: int
    pending: int
    verified: int
    edited: int
```
And `MarkdownStats`:
```python
class MarkdownStats(BaseModel):
    total: int
    pending: int
    verified: int
    edited: int
    auto_confirmable: int = 0
```

In `api/review/queries.py`, drop the `disagreement` line from `classification_stats`'s RETURN. Drop `good`, `bad`, `unscored` from `markdown_stats`'s RETURN.

- [ ] **Step 5: Final grep for dead UI references**

```
grep -n "cstatus\|mdstatus\|autoConfMin\|mdScoreThreshold\|auto-conf-bulk\|md-conf-bulk\|loadMarkdownStats\|updateAutoConfirmButton\|updateMdConfirmButton" web/review.html
```
Expected: no output. Delete any remaining hits.

- [ ] **Step 6: Run tests**

```
pytest tests/api/test_review_classification.py tests/api/test_review_markdown.py -v
```
Expected: all pass (incl. the new "rejects_legacy" tests).

- [ ] **Step 7: Manually verify the UI is unbroken**

Reload, exercise all three stages, all four statuses, both groups, the score from/to, the notice-type pills, the bulk-confirm button. Console should be clean.

- [ ] **Step 8: Commit**

```
git add api/review/queries.py api/review/router.py tests/api/test_review_classification.py tests/api/test_review_markdown.py web/review.html
git commit -m "chore(review): retire legacy status values + slider remnants"
```

---

## Phase 5 — Smoke test the full flow

### Task 16: End-to-end UI walkthrough

- [ ] **Step 1: Pull a fresh page**

Open `web/review.html` with a clean cache. Verify the default landing state:
- Stage = `description`
- Group = `property`
- Status = `pending`
- Notice type = `all`
- Date from = today; date to = empty
- Score row hidden (description stage has no score)

- [ ] **Step 2: Walk each stage × group combination**

For each cell in the 3×2 stage×group grid, verify: panel switches, count refreshes, status pills are correctly highlighted, network requests carry the expected params.

Capture a screenshot of each stage's default view (6 screenshots) — these are evidence the design lines up with what shipped.

- [ ] **Step 3: Test filter interactions**

- Set date from/to to a narrow window — counts shrink across stages.
- Set score from/to to e.g. 70 to 100 — classification + markdown stages narrow; description ignores.
- Set notice-type to single — counts further narrow across all stages.
- Click bulk-confirm in classification with a known-empty range — button should show 0 and be disabled.

- [ ] **Step 4: Verify hash routing**

- Reload the page. The hash should reflect current state and restore it.
- Manually edit the hash (`#stage=markdown&group=property`) and reload — UI must land on the right stage+group.

- [ ] **Step 5: No commit needed**

This is a verification task — no code change. If anything failed, file follow-up commits under whatever earlier task it belongs to.

---

## Self-review checklist (run after writing every task)

- [ ] Every spec requirement maps to a task. Specifically:
  - Stages-as-tabs / sub-groups → Tasks 9, 10, 11
  - Uniform pending/verified/edited statuses → Tasks 1, 2, 6, 15
  - Score from/to range → Tasks 3, 4, 9, 11, 13
  - Notice-type filter → Task 5
  - Bulk-confirm range-based → Tasks 3, 4, 12
  - By-property views in classification + markdown → Tasks 7, 8, 14
  - URL hash routing → Task 11
  - localStorage per-stage subgroup → Task 11
  - Status mapping table (overridden flag, markdown_quality) → Tasks 1, 2, 6, 15
- [ ] No "TODO" in step bodies — the only `// TODO Task 14` markers are removed in Task 14.
- [ ] Method/property names stay consistent: `queueState.scoreFrom` / `scoreTo`, `appendCommonParams`, `appendScoreParams`, `writeHash`, `loadClassificationByPropertyQueue`, `loadMarkdownByPropertyQueue`, `updateBulkConfirmButton`.
- [ ] API param names match between frontend and backend: `confidence_max` (0–1), `score_max` (0–100), `notice_type` (string enum).
- [ ] Tests exist for every new endpoint and every new query param.
