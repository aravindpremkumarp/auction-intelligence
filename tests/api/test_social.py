"""
tests/api/test_social.py
------------------------
End-to-end tests for the staged-content review surface (`/social/*`).

Two things are worth guarding hard here:

1. **Auth.** Every route reads unpublished marketing material, so anonymous and
   non-admin callers must be turned away on all of them — including the asset
   route, which is the one that would otherwise be tempting to leave open (the
   notice-source proxy in api/review is, but its bytes are already public).
2. **Path handling.** The asset route takes a caller-supplied date *and* a
   caller-supplied relative path and turns them into a filesystem read. Every
   traversal shape must come back 404, and no rejected path may ever be
   distinguishable from a merely-absent one.

Batches are synthesised into a tmp dir via `POSTER_OUT_DIR` — the same env var
the Poster writes to — so these never depend on what happens to be committed
under marketing/outputs/.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import auth_header


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def _reset() -> None:
    from api import neo4j_client
    neo4j_client._users.clear()          # type: ignore[attr-defined]
    neo4j_client._social.clear()         # type: ignore[attr-defined]


def _admin_headers(c: TestClient, sub: str = "sub-admin") -> dict:
    """Seed a user via /auth/me, promote it in the stub store, return its header."""
    from api import neo4j_client
    h = auth_header(sub=sub, email=f"{sub}@example.com", name="Boss")
    c.get("/auth/me", headers=h)
    neo4j_client._users[sub]["role"] = "admin"  # type: ignore[attr-defined]
    return h


def _user_headers(c: TestClient, sub: str = "sub-plain") -> dict:
    h = auth_header(sub=sub, email=f"{sub}@example.com", name="Plain")
    c.get("/auth/me", headers=h)
    return h


DATE = "2026-07-15"


def _stage_batch(root: Path, date: str = DATE, *, render: bool = True) -> Path:
    """Write a minimal but structurally real batch: 2 drafts, 2 cards, 3 reels
    (one of them the batch-level stats reel) and a city carousel."""
    d = root / date
    (d / "cards").mkdir(parents=True, exist_ok=True)
    (d / "reels").mkdir(parents=True, exist_ok=True)
    drafts = {
        "stats": {"total_auctions": 2434, "upcoming_auctions": 638,
                  "generated_at": f"{date}T17:45:38.303810Z"},
        "drafts": [
            {"auction_id": "798444", "angle": "price_drop", "hook_mechanism": "contrast",
             "post": "₹31.9L → ₹31L. same karur plot.",
             "pinned_comment": "not legal advice.",
             "hashtags": ["bankauction", "karur"], "alt_text": "deal card: karur plot.",
             "video_title": "karur plot", "location_tag": "Karur",
             "reel_hook": {"line1": "₹31.9L → ₹31L", "line2": "round two."},
             "reel_context_lines": ["reserve just moved."],
             "hook_alternatives": ["the bank wanted ₹31.9L."],
             "source": {"auction_id": "798444", "city": "Karur", "reserve_lakhs": 31.0}},
            {"auction_id": "802076", "angle": "price_drop", "hook_mechanism": "process",
             "post": "₹45.6L → ₹41L. vellore plot.",
             "hashtags": ["reauction"], "source": {"auction_id": "802076", "city": "Vellore"}},
        ],
        "cards": [
            {"draft_index": 0, "auction_id": "carousel",
             "template": "city-carousel-1080x1350", "data": "cards/00-carousel.json",
             "headline": "5 live lots in karur", "slides": 4},
            {"draft_index": 1, "auction_id": "798444",
             "template": "price-drop-1080x1350", "data": "cards/01-798444.json",
             "headline": "₹31.9L → ₹31L"},
            {"draft_index": 2, "auction_id": "802076",
             "template": "price-drop-1080x1350", "data": "cards/02-802076.json",
             "headline": "₹45.6L → ₹41L"},
        ],
        "reels": [
            # No video_key: staged before reels went to R2, or the render failed.
            {"draft_index": 0, "auction_id": "stats",
             "template": "stats-reel-1080x1920", "data": "reels/00-stats.json"},
            {"draft_index": 1, "auction_id": "798444",
             "template": "deal-reel-1080x1920", "data": "reels/01-798444.json",
             "hook": "₹31.9L → ₹31L",
             "video_key": f"marketing-reels/{date}/01-798444.mp4",
             "video_bytes": 2_400_000},
        ],
        "rejected": [],
        "editor_notes": "picked 2 price_drop to cover 2",
    }
    (d / "drafts.json").write_text(json.dumps(drafts), encoding="utf-8")
    for name in ("00-carousel", "01-798444", "02-802076"):
        (d / "cards" / f"{name}.json").write_text('{"headline": "x"}', encoding="utf-8")
    for name in ("00-stats", "01-798444"):
        (d / "reels" / f"{name}.json").write_text('{"hook": "x"}', encoding="utf-8")
    (d / "review.md").write_text("# Content review\n", encoding="utf-8")
    if render:
        rendered = d / "rendered"
        rendered.mkdir(exist_ok=True)
        # Draft 1's card renders to a single PNG; the carousel renders one per
        # slide (render_social.stage_name numbers multi-stage templates).
        # Draft 2's card is deliberately missing — the render step is
        # continue-on-error in the workflow, so this is a real state.
        (rendered / "card-01-798444.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (rendered / "card-00-carousel_01.png").write_bytes(b"\x89PNG\r\n\x1a\nfake1")
        (rendered / "card-00-carousel_02.png").write_bytes(b"\x89PNG\r\n\x1a\nfake2")
    return d


@pytest.fixture
def batch(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("POSTER_OUT_DIR", str(tmp_path))
    _reset()
    return _stage_batch(tmp_path)


REEL_BYTES = b"\x00\x00\x00\x18ftypmp42fake-mp4-payload"


class _FakeBody:
    """Stand-in for boto3's StreamingBody (iter_chunks / read / close)."""

    def __init__(self, blob: bytes) -> None:
        self._blob = blob
        self.closed = False

    def iter_chunks(self, chunk_size: int = 65536):
        for i in range(0, len(self._blob), chunk_size):
            yield self._blob[i:i + chunk_size]

    def read(self) -> bytes:
        return self._blob

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def r2(monkeypatch):
    """Fake private-R2 reads, recording which keys were asked for."""
    from pipeline import storage

    calls: list[str] = []

    def fake_get(key: str):
        calls.append(key)
        return {"Body": _FakeBody(REEL_BYTES), "ContentType": "video/mp4",
                "ContentLength": len(REEL_BYTES)}

    monkeypatch.setattr(storage, "get_private_object", fake_get)
    return calls


# ── auth ─────────────────────────────────────────────────────────────────────
ROUTES = [
    ("GET", "/social/batches"),
    ("GET", f"/social/batch/{DATE}"),
    ("GET", f"/social/asset/{DATE}/rendered/card-01-798444.png"),
    ("GET", f"/social/bundle/{DATE}/1"),
]


@pytest.mark.parametrize("method,path", ROUTES)
def test_routes_reject_anonymous(batch, method, path) -> None:
    r = _client().request(method, path)
    assert r.status_code == 401, f"{path} -> {r.status_code}"


@pytest.mark.parametrize("method,path", ROUTES)
def test_routes_reject_non_admin(batch, method, path) -> None:
    c = _client()
    r = c.request(method, path, headers=_user_headers(c))
    assert r.status_code == 403, f"{path} -> {r.status_code}"


def test_patch_rejects_anonymous_and_non_admin(batch) -> None:
    c = _client()
    path = f"/social/item/{DATE}/card/01-798444"
    assert c.patch(path, json={"status": "approved"}).status_code == 401
    r = c.patch(path, json={"status": "approved"}, headers=_user_headers(c))
    assert r.status_code == 403


# ── listing ──────────────────────────────────────────────────────────────────
def test_list_batches(batch, tmp_path) -> None:
    _stage_batch(tmp_path, "2026-07-08", render=False)
    c = _client()
    r = c.get("/social/batches", headers=_admin_headers(c))
    assert r.status_code == 200, r.text
    items = r.json()
    assert [b["date"] for b in items] == ["2026-07-15", "2026-07-08"]  # newest first
    b = items[0]
    assert b["n_drafts"] == 2
    assert b["n_cards"] == 2       # the carousel is counted separately
    assert b["n_carousels"] == 1
    assert b["n_reels"] == 2
    assert b["generated_at"].startswith("2026-07-15T")
    assert b["error"] is None
    assert b["statuses"] == {"approved": 0, "rejected": 0, "posted": 0}


def test_directory_without_drafts_json_is_not_a_batch(batch, tmp_path) -> None:
    (tmp_path / "2026-07-01").mkdir()
    (tmp_path / "not-a-date").mkdir()
    c = _client()
    r = c.get("/social/batches", headers=_admin_headers(c))
    assert [b["date"] for b in r.json()] == ["2026-07-15"]


def test_malformed_drafts_json_is_listed_with_error(batch, tmp_path) -> None:
    bad = tmp_path / "2026-07-02"
    bad.mkdir()
    (bad / "drafts.json").write_text("{not json", encoding="utf-8")
    c = _client()
    r = c.get("/social/batches", headers=_admin_headers(c))
    items = {b["date"]: b for b in r.json()}
    # The broken batch surfaces its error, and the good one is unaffected.
    assert items["2026-07-02"]["error"]
    assert items["2026-07-15"]["error"] is None
    assert items["2026-07-15"]["n_drafts"] == 2


# ── batch detail ─────────────────────────────────────────────────────────────
def test_batch_detail_shape(batch) -> None:
    c = _client()
    r = c.get(f"/social/batch/{DATE}", headers=_admin_headers(c))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["editor_notes"].startswith("picked 2")
    assert len(body["drafts"]) == 2

    d1 = body["drafts"][0]
    assert d1["draft_index"] == 1
    assert d1["hashtags"] == ["bankauction", "karur"]
    assert d1["source"]["city"] == "Karur"           # grounded facts pass through
    assert d1["reel_hook"]["line1"].endswith("31L")
    kinds = {a["kind"]: a for a in d1["artifacts"]}
    assert set(kinds) == {"card", "reel"}
    assert kinds["card"]["png_paths"] == ["rendered/card-01-798444.png"]
    assert kinds["card"]["png_available"] is True
    assert kinds["card"]["status"] == "pending"      # no node stored yet
    assert kinds["reel"]["hook"].endswith("31L")
    assert kinds["reel"]["png_available"] is False   # reels have no committed render
    assert kinds["reel"]["video_available"] is True  # its mp4 is on R2
    assert kinds["reel"]["video_bytes"] == 2_400_000
    # The R2 key is an internal pointer — it must not reach the client.
    assert "video_key" not in kinds["reel"]

    # Batch-level artifacts belong to no draft.
    orphans = {(a["kind"], a["stem"]) for a in body["orphan_artifacts"]}
    assert orphans == {("carousel", "00-carousel"), ("reel", "00-stats")}


def test_missing_render_reports_unavailable_not_error(batch) -> None:
    c = _client()
    r = c.get(f"/social/batch/{DATE}", headers=_admin_headers(c))
    card = [a for a in r.json()["drafts"][1]["artifacts"] if a["kind"] == "card"][0]
    assert card["png_available"] is False
    assert card["png_paths"] == []


def test_carousel_exposes_every_slide(batch) -> None:
    c = _client()
    r = c.get(f"/social/batch/{DATE}", headers=_admin_headers(c))
    carousel = [a for a in r.json()["orphan_artifacts"] if a["kind"] == "carousel"][0]
    assert carousel["png_paths"] == [
        "rendered/card-00-carousel_01.png",
        "rendered/card-00-carousel_02.png",
    ]


@pytest.mark.parametrize("date", ["2026-07-99", "2026-7-15", "../etc", "2026-07-16"])
def test_unknown_or_malformed_batch_date_404s(batch, date) -> None:
    c = _client()
    r = c.get(f"/social/batch/{date}", headers=_admin_headers(c))
    assert r.status_code == 404, f"{date} -> {r.status_code}"


# ── status round-trip ────────────────────────────────────────────────────────
def test_status_set_and_read_back(batch) -> None:
    c = _client()
    h = _admin_headers(c)
    r = c.patch(f"/social/item/{DATE}/card/01-798444",
                json={"status": "approved", "note": "ship it"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["updated_by_email"] == "sub-admin@example.com"

    body = c.get(f"/social/batch/{DATE}", headers=h).json()
    card = [a for a in body["drafts"][0]["artifacts"] if a["kind"] == "card"][0]
    assert card["status"] == "approved"
    assert card["note"] == "ship it"
    assert card["updated_at"]
    # Its sibling reel is untouched — status is per artifact, not per draft.
    reel = [a for a in body["drafts"][0]["artifacts"] if a["kind"] == "reel"][0]
    assert reel["status"] == "pending"


def test_posted_status_carries_url(batch) -> None:
    c = _client()
    h = _admin_headers(c)
    c.patch(f"/social/item/{DATE}/reel/01-798444",
            json={"status": "posted", "posted_url": "https://instagram.com/p/abc"},
            headers=h)
    body = c.get(f"/social/batch/{DATE}", headers=h).json()
    reel = [a for a in body["drafts"][0]["artifacts"] if a["kind"] == "reel"][0]
    assert reel["status"] == "posted"
    assert reel["posted_url"] == "https://instagram.com/p/abc"


def test_repeated_set_updates_in_place(batch) -> None:
    from api import neo4j_client
    c = _client()
    h = _admin_headers(c)
    for status in ("approved", "rejected", "posted"):
        c.patch(f"/social/item/{DATE}/card/01-798444", json={"status": status}, headers=h)
    assert len(neo4j_client._social) == 1  # type: ignore[attr-defined]
    body = c.get(f"/social/batch/{DATE}", headers=h).json()
    card = [a for a in body["drafts"][0]["artifacts"] if a["kind"] == "card"][0]
    assert card["status"] == "posted"


def test_clearing_to_pending_removes_the_node(batch) -> None:
    from api import neo4j_client
    c = _client()
    h = _admin_headers(c)
    c.patch(f"/social/item/{DATE}/card/01-798444", json={"status": "approved"}, headers=h)
    assert len(neo4j_client._social) == 1  # type: ignore[attr-defined]

    r = c.patch(f"/social/item/{DATE}/card/01-798444", json={"status": "pending"}, headers=h)
    assert r.status_code == 200
    assert neo4j_client._social == {}  # type: ignore[attr-defined]
    body = c.get(f"/social/batch/{DATE}", headers=h).json()
    card = [a for a in body["drafts"][0]["artifacts"] if a["kind"] == "card"][0]
    assert card["status"] == "pending"


def test_rollup_appears_on_the_batch_list(batch) -> None:
    c = _client()
    h = _admin_headers(c)
    c.patch(f"/social/item/{DATE}/card/01-798444", json={"status": "approved"}, headers=h)
    c.patch(f"/social/item/{DATE}/reel/01-798444", json={"status": "rejected"}, headers=h)
    items = c.get("/social/batches", headers=h).json()
    assert items[0]["statuses"] == {"approved": 1, "rejected": 1, "posted": 0}


def test_status_for_unknown_artifact_404s(batch) -> None:
    c = _client()
    h = _admin_headers(c)
    # Right batch, stem that was never staged.
    r = c.patch(f"/social/item/{DATE}/card/99-nope", json={"status": "approved"}, headers=h)
    assert r.status_code == 404
    # Right stem, wrong kind.
    r = c.patch(f"/social/item/{DATE}/reel/02-802076", json={"status": "approved"}, headers=h)
    assert r.status_code == 404


def test_invalid_status_is_rejected(batch) -> None:
    c = _client()
    r = c.patch(f"/social/item/{DATE}/card/01-798444",
                json={"status": "published"}, headers=_admin_headers(c))
    assert r.status_code == 422


# ── assets ───────────────────────────────────────────────────────────────────
def test_asset_served(batch) -> None:
    c = _client()
    h = _admin_headers(c)
    r = c.get(f"/social/asset/{DATE}/rendered/card-01-798444.png", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert "content-disposition" not in r.headers  # inline by default (blob preview)

    r = c.get(f"/social/asset/{DATE}/cards/01-798444.json", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


def test_asset_download_flag_sets_attachment(batch) -> None:
    c = _client()
    r = c.get(f"/social/asset/{DATE}/rendered/card-01-798444.png?download=1",
              headers=_admin_headers(c))
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="card-01-798444.png"'


@pytest.mark.parametrize("relpath", [
    "../../api/main.py",
    "..%2f..%2fapi%2fmain.py",
    "rendered/../../../api/main.py",
    "/etc/passwd",
    "review.md/../../../CLAUDE.md",
])
def test_asset_traversal_is_refused(batch, relpath) -> None:
    c = _client()
    r = c.get(f"/social/asset/{DATE}/{relpath}", headers=_admin_headers(c))
    assert r.status_code == 404, f"{relpath} -> {r.status_code}"
    assert "main.py" not in r.text and "passwd" not in r.text


def test_asset_type_allowlist(batch, tmp_path) -> None:
    """A file inside the batch that isn't a staged artifact type is still refused —
    the guard is an allowlist, so a stray .py in the batch dir can't be read."""
    (tmp_path / DATE / "sneaky.py").write_text("SECRET = 1", encoding="utf-8")
    c = _client()
    r = c.get(f"/social/asset/{DATE}/sneaky.py", headers=_admin_headers(c))
    assert r.status_code == 404
    assert "SECRET" not in r.text


def test_asset_missing_file_404s(batch) -> None:
    c = _client()
    r = c.get(f"/social/asset/{DATE}/rendered/card-02-802076.png",
              headers=_admin_headers(c))
    assert r.status_code == 404


def test_asset_bad_date_404s(batch) -> None:
    c = _client()
    r = c.get("/social/asset/not-a-date/review.md", headers=_admin_headers(c))
    assert r.status_code == 404


# ── reel video ───────────────────────────────────────────────────────────────
def test_reel_streams_from_private_r2(batch, r2) -> None:
    c = _client()
    r = c.get(f"/social/reel/{DATE}/01-798444", headers=_admin_headers(c))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"
    assert r.content == REEL_BYTES
    assert r2 == [f"marketing-reels/{DATE}/01-798444.mp4"]


def test_reel_requires_admin(batch, r2) -> None:
    c = _client()
    path = f"/social/reel/{DATE}/01-798444"
    assert c.get(path).status_code == 401
    assert c.get(path, headers=_user_headers(c)).status_code == 403
    assert r2 == []  # never touched storage for an unauthorised caller


def test_reel_without_uploaded_mp4_404s(batch, r2) -> None:
    """The stats reel exists in the batch but has no video_key."""
    c = _client()
    r = c.get(f"/social/reel/{DATE}/00-stats", headers=_admin_headers(c))
    assert r.status_code == 404
    assert r2 == []


def test_reel_unknown_stem_or_batch_404s(batch, r2) -> None:
    c = _client()
    h = _admin_headers(c)
    assert c.get(f"/social/reel/{DATE}/99-nope", headers=h).status_code == 404
    assert c.get("/social/reel/2026-07-16/01-798444", headers=h).status_code == 404
    assert c.get("/social/reel/not-a-date/01-798444", headers=h).status_code == 404


def test_reel_asks_only_for_a_reel_not_a_card(batch, r2) -> None:
    """A card stem must not be servable through the video route."""
    c = _client()
    r = c.get(f"/social/reel/{DATE}/01-798444.png", headers=_admin_headers(c))
    assert r.status_code == 404


def test_missing_r2_object_is_404_not_500(batch, monkeypatch) -> None:
    from botocore.exceptions import ClientError

    from pipeline import storage

    def boom(key: str):
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    monkeypatch.setattr(storage, "get_private_object", boom)
    c = _client()
    r = c.get(f"/social/reel/{DATE}/01-798444", headers=_admin_headers(c))
    assert r.status_code == 404


def test_unconfigured_r2_is_503(batch, monkeypatch) -> None:
    from pipeline import storage

    def boom(key: str):
        raise storage.R2ConfigError("Missing private R2 config: R2_ACCOUNT_ID")

    monkeypatch.setattr(storage, "get_private_object", boom)
    c = _client()
    r = c.get(f"/social/reel/{DATE}/01-798444", headers=_admin_headers(c))
    assert r.status_code == 503


# ── bundle ───────────────────────────────────────────────────────────────────
def test_bundle_is_a_valid_zip(batch, r2) -> None:
    c = _client()
    r = c.get(f"/social/bundle/{DATE}/1", headers=_admin_headers(c))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "2026-07-15-draft-01-798444.zip" in r.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
        assert "caption.txt" in names
        assert "cards/01-798444.json" in names
        assert "reels/01-798444.json" in names
        assert "rendered/card-01-798444.png" in names
        # The MP4 you actually need to post the reel — pulled from R2, not disk.
        assert "reels/01-798444.mp4" in names
        assert zf.read("reels/01-798444.mp4") == REEL_BYTES
        caption = zf.read("caption.txt").decode("utf-8")
    assert "same karur plot" in caption
    assert "#bankauction #karur" in caption
    assert "not legal advice." in caption


def test_bundle_survives_a_missing_reel_object(batch, monkeypatch) -> None:
    """R2 down or the object gone must not cost the reviewer the whole zip."""
    from pipeline import storage

    def boom(key: str):
        raise RuntimeError("r2 unreachable")

    monkeypatch.setattr(storage, "get_private_object", boom)
    c = _client()
    r = c.get(f"/social/bundle/{DATE}/1", headers=_admin_headers(c))
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert "caption.txt" in names
    assert "rendered/card-01-798444.png" in names
    assert not any(n.endswith(".mp4") for n in names)


def test_bundle_skips_missing_renders(batch, r2) -> None:
    """Draft 2 has no rendered PNG — the bundle still builds, minus the image."""
    c = _client()
    r = c.get(f"/social/bundle/{DATE}/2", headers=_admin_headers(c))
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert "cards/02-802076.json" in names
    assert not any(n.startswith("rendered/") for n in names)


def test_bundle_unknown_draft_404s(batch) -> None:
    c = _client()
    h = _admin_headers(c)
    assert c.get(f"/social/bundle/{DATE}/99", headers=h).status_code == 404
    assert c.get("/social/bundle/2026-07-16/1", headers=h).status_code == 404


# ── page route ───────────────────────────────────────────────────────────────
def test_social_page_served() -> None:
    r = _client().get("/social")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # The real page, not the SPA shell.
    assert "social" in r.text.lower()
    assert "/social/batches" in r.text
