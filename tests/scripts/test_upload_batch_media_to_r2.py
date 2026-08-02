"""
tests/scripts/test_upload_batch_media_to_r2.py
----------------------------------------------
Unit tests for the batch-media → private-R2 upload step of the content-poster
workflow (card PNGs, carousel slides, reel MP4s).

The thing worth guarding is the write-back. The workflow calls this twice — once
after cards render, once after reels do — and the second call must merge into
what the first recorded rather than replace it. If `media_keys` is lost or
overwritten, the files sit in R2 with nothing pointing at them and /social shows
an empty batch. Uploads are faked; this covers key shape, the merge, partial
renders, and the exit codes the workflow branches on.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline import storage  # noqa: E402
from scripts import upload_batch_media_to_r2 as up  # noqa: E402

DATE = "2026-08-01"
PREFIX = f"marketing-media/{DATE}"


def _batch(tmp_path: Path, *, rendered: list[str] | None = None) -> Path:
    d = tmp_path / DATE
    (d / "cards").mkdir(parents=True)
    (d / "reels").mkdir(parents=True)
    manifest = {
        "stats": {"generated_at": f"{DATE}T10:00:00Z"},
        "drafts": [{"auction_id": "798444", "post": "x"}],
        "cards": [
            {"draft_index": 0, "auction_id": "carousel",
             "template": "city-carousel-1080x1350", "data": "cards/00-carousel.json"},
            {"draft_index": 1, "auction_id": "798444",
             "template": "price-drop-1080x1350", "data": "cards/01-798444.json"},
        ],
        "reels": [
            {"draft_index": 0, "auction_id": "stats",
             "template": "stats-reel-1080x1920", "data": "reels/00-stats.json"},
            {"draft_index": 1, "auction_id": "798444",
             "template": "deal-reel-1080x1920", "data": "reels/01-798444.json"},
        ],
    }
    (d / "drafts.json").write_text(json.dumps(manifest), encoding="utf-8")
    if rendered:
        r = d / "rendered"
        r.mkdir()
        for name in rendered:
            (r / name).write_bytes(b"fake-png-" + name.encode())
    return d


def _renders(tmp_path: Path, stems: list[str]) -> Path:
    r = tmp_path / "renders"
    r.mkdir(exist_ok=True)
    for s in stems:
        (r / f"{s}.mp4").write_bytes(b"fake-mp4-" + s.encode())
    return r


@pytest.fixture
def uploads(monkeypatch):
    """Record upload_file_private calls instead of hitting R2."""
    calls: list[tuple[str, str]] = []

    def fake_upload(local_path, key, content_type=None):
        calls.append((str(local_path), key))
        return key

    monkeypatch.setattr(storage, "upload_file_private", fake_upload)
    monkeypatch.setattr(storage, "_require_private_config", lambda: None)
    monkeypatch.setattr(storage, "_require_config", lambda: None)
    return calls


def _run(batch_dir: Path, monkeypatch, renders_dir: Path | None = None) -> int:
    argv = ["upload_batch_media_to_r2.py", "--batch", str(batch_dir)]
    if renders_dir is not None:
        argv += ["--renders", str(renders_dir)]
    monkeypatch.setattr(sys, "argv", argv)
    return up.main()


def _manifest(batch: Path) -> dict:
    return json.loads((batch / "drafts.json").read_text(encoding="utf-8"))


def test_key_shape() -> None:
    assert storage.batch_media_key(DATE, "rendered/card-01-798444.png") == \
        f"{PREFIX}/rendered/card-01-798444.png"
    assert storage.batch_media_key(DATE, "reels/01-798444.mp4") == \
        f"{PREFIX}/reels/01-798444.mp4"
    # Directory structure survives, but traversal segments are dropped so
    # nothing can escape the batch prefix.
    assert storage.batch_media_key(DATE, "../../etc/passwd").startswith(f"{PREFIX}/")
    assert ".." not in storage.batch_media_key(DATE, "a/../../b")


def test_cards_pass_uploads_pngs_only(tmp_path, monkeypatch, uploads) -> None:
    """The first invocation runs before any reel exists."""
    batch = _batch(tmp_path, rendered=[
        "card-00-carousel_01.png", "card-00-carousel_02.png", "card-01-798444.png"])
    assert _run(batch, monkeypatch) == 0

    assert [k for _, k in uploads] == [
        f"{PREFIX}/rendered/card-00-carousel_01.png",
        f"{PREFIX}/rendered/card-00-carousel_02.png",
        f"{PREFIX}/rendered/card-01-798444.png",
    ]
    keys = _manifest(batch)["media_keys"]
    assert set(keys) == {
        "rendered/card-00-carousel_01.png",
        "rendered/card-00-carousel_02.png",
        "rendered/card-01-798444.png",
    }
    assert all(v > 0 for v in _manifest(batch)["media_bytes"].values())


def test_reels_pass_merges_into_the_cards_pass(tmp_path, monkeypatch, uploads) -> None:
    """The second invocation must not drop what the first recorded."""
    batch = _batch(tmp_path, rendered=["card-01-798444.png"])
    assert _run(batch, monkeypatch) == 0
    assert set(_manifest(batch)["media_keys"]) == {"rendered/card-01-798444.png"}

    renders = _renders(tmp_path, ["00-stats", "01-798444"])
    assert _run(batch, monkeypatch, renders) == 0

    keys = _manifest(batch)["media_keys"]
    assert set(keys) == {
        "rendered/card-01-798444.png",   # still there
        "reels/00-stats.mp4",
        "reels/01-798444.mp4",
    }
    assert keys["reels/01-798444.mp4"] == f"{PREFIX}/reels/01-798444.mp4"


def test_partial_render_uploads_what_exists(tmp_path, monkeypatch, uploads) -> None:
    """Both render steps are continue-on-error, so partial output is normal."""
    batch = _batch(tmp_path, rendered=["card-01-798444.png"])  # carousel never rendered
    renders = _renders(tmp_path, ["01-798444"])                # stats reel never rendered
    assert _run(batch, monkeypatch, renders) == 0

    assert set(_manifest(batch)["media_keys"]) == {
        "rendered/card-01-798444.png", "reels/01-798444.mp4"}


def test_islands_and_review_md_are_not_uploaded(tmp_path, monkeypatch, uploads) -> None:
    """Only rendered media goes to R2 — the text files stay in git."""
    batch = _batch(tmp_path, rendered=["card-01-798444.png"])
    (batch / "review.md").write_text("# review", encoding="utf-8")
    (batch / "cards" / "01-798444.json").write_text("{}", encoding="utf-8")
    assert _run(batch, monkeypatch) == 0

    assert set(_manifest(batch)["media_keys"]) == {"rendered/card-01-798444.png"}
    assert not any("review.md" in k or ".json" in k for _, k in uploads)


def test_no_media_is_a_noop(tmp_path, monkeypatch, uploads) -> None:
    batch = _batch(tmp_path)
    before = (batch / "drafts.json").read_text(encoding="utf-8")
    assert _run(batch, monkeypatch) == 0
    assert uploads == []
    assert (batch / "drafts.json").read_text(encoding="utf-8") == before


def test_unconfigured_r2_exits_2_and_leaves_manifest_alone(tmp_path, monkeypatch) -> None:
    """Exit 2 is what makes the workflow warn instead of failing the run."""
    def boom() -> None:
        raise storage.R2ConfigError("Missing private R2 config: R2_ACCOUNT_ID")

    monkeypatch.setattr(storage, "_require_private_config", boom)
    batch = _batch(tmp_path, rendered=["card-01-798444.png"])
    before = (batch / "drafts.json").read_text(encoding="utf-8")
    assert _run(batch, monkeypatch) == 2
    assert (batch / "drafts.json").read_text(encoding="utf-8") == before


def test_missing_public_config_also_exits_2(tmp_path, monkeypatch) -> None:
    """r2_client() validates the PUBLIC config even for a private-bucket write.

    Without the up-front check this would surface as every upload failing (exit
    1) instead of the clean "not configured" fallback.
    """
    def boom() -> None:
        raise storage.R2ConfigError("Missing R2 config: R2_PUBLIC_BASE_URL")

    monkeypatch.setattr(storage, "_require_private_config", lambda: None)
    monkeypatch.setattr(storage, "_require_config", boom)
    batch = _batch(tmp_path, rendered=["card-01-798444.png"])
    before = (batch / "drafts.json").read_text(encoding="utf-8")
    assert _run(batch, monkeypatch) == 2
    assert (batch / "drafts.json").read_text(encoding="utf-8") == before


def test_every_upload_failing_exits_1(tmp_path, monkeypatch) -> None:
    def boom(local_path, key, content_type=None):
        raise RuntimeError("r2 down")

    monkeypatch.setattr(storage, "_require_private_config", lambda: None)
    monkeypatch.setattr(storage, "_require_config", lambda: None)
    monkeypatch.setattr(storage, "upload_file_private", boom)
    batch = _batch(tmp_path, rendered=["card-01-798444.png"])
    before = (batch / "drafts.json").read_text(encoding="utf-8")
    assert _run(batch, monkeypatch) == 1
    # A failed run must not rewrite the manifest with half-truths.
    assert (batch / "drafts.json").read_text(encoding="utf-8") == before


def test_one_failure_does_not_block_the_rest(tmp_path, monkeypatch) -> None:
    def flaky(local_path, key, content_type=None):
        if "carousel" in key:
            raise RuntimeError("r2 hiccup")
        return key

    monkeypatch.setattr(storage, "_require_private_config", lambda: None)
    monkeypatch.setattr(storage, "_require_config", lambda: None)
    monkeypatch.setattr(storage, "upload_file_private", flaky)
    batch = _batch(tmp_path, rendered=["card-00-carousel_01.png", "card-01-798444.png"])
    assert _run(batch, monkeypatch) == 0

    assert set(_manifest(batch)["media_keys"]) == {"rendered/card-01-798444.png"}


def test_missing_manifest_is_a_noop(tmp_path, monkeypatch, uploads) -> None:
    empty = tmp_path / DATE
    empty.mkdir()
    assert _run(empty, monkeypatch) == 0
    assert uploads == []


def test_rerun_is_idempotent(tmp_path, monkeypatch, uploads) -> None:
    """Same path → same key, so a re-render overwrites instead of accumulating."""
    batch = _batch(tmp_path, rendered=["card-01-798444.png"])
    renders = _renders(tmp_path, ["01-798444"])
    assert _run(batch, monkeypatch, renders) == 0
    first = _manifest(batch)
    assert _run(batch, monkeypatch, renders) == 0
    assert _manifest(batch) == first
    assert len({k for _, k in uploads}) == 2
