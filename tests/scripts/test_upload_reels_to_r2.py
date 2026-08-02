"""
tests/scripts/test_upload_reels_to_r2.py
----------------------------------------
Unit tests for the reel → private-R2 upload step of the content-poster workflow.

The thing worth guarding is the write-back: the workflow commits `drafts.json`
*before* reels render, so if the uploader fails to record `video_key` onto the
manifest rows, the MP4s land in R2 and nothing ever finds them again. Uploads
are faked — this covers key shape, manifest mutation, partial renders, and the
exit codes the workflow branches on.
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
from scripts import upload_reels_to_r2 as up  # noqa: E402

DATE = "2026-08-01"


def _batch(tmp_path: Path, reels: list[dict] | None = None) -> Path:
    d = tmp_path / DATE
    d.mkdir(parents=True)
    manifest = {
        "stats": {"generated_at": f"{DATE}T10:00:00Z"},
        "drafts": [{"auction_id": "798444", "post": "x"}],
        "cards": [{"draft_index": 1, "auction_id": "798444",
                   "template": "price-drop-1080x1350", "data": "cards/01-798444.json"}],
        "reels": reels if reels is not None else [
            {"draft_index": 0, "auction_id": "stats",
             "template": "stats-reel-1080x1920", "data": "reels/00-stats.json"},
            {"draft_index": 1, "auction_id": "798444",
             "template": "deal-reel-1080x1920", "data": "reels/01-798444.json"},
        ],
    }
    (d / "drafts.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


def _renders(tmp_path: Path, stems: list[str]) -> Path:
    r = tmp_path / "renders"
    r.mkdir()
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


def _run(batch_dir: Path, renders_dir: Path, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", [
        "upload_reels_to_r2.py", "--batch", str(batch_dir), "--renders", str(renders_dir),
    ])
    return up.main()


def test_key_shape() -> None:
    assert storage.reel_object_key("2026-08-01", "01-798444") == \
        "marketing-reels/2026-08-01/01-798444.mp4"
    # Path-unsafe input can't escape the prefix.
    assert "/" not in storage.reel_object_key("2026-08-01", "../../etc/passwd").split("/")[-1]
    assert storage.reel_object_key("2026-08-01", "../x").startswith("marketing-reels/2026-08-01/")


def test_uploads_and_records_keys(tmp_path, monkeypatch, uploads) -> None:
    batch = _batch(tmp_path)
    renders = _renders(tmp_path, ["00-stats", "01-798444"])
    assert _run(batch, renders, monkeypatch) == 0

    assert [k for _, k in uploads] == [
        f"marketing-reels/{DATE}/00-stats.mp4",
        f"marketing-reels/{DATE}/01-798444.mp4",
    ]
    data = json.loads((batch / "drafts.json").read_text(encoding="utf-8"))
    keys = [r.get("video_key") for r in data["reels"]]
    assert keys == [f"marketing-reels/{DATE}/00-stats.mp4",
                    f"marketing-reels/{DATE}/01-798444.mp4"]
    assert all(r["video_bytes"] > 0 for r in data["reels"])
    # Nothing else in the manifest is disturbed.
    assert data["cards"][0]["data"] == "cards/01-798444.json"
    assert data["drafts"][0]["auction_id"] == "798444"


def test_partial_render_uploads_what_exists(tmp_path, monkeypatch, uploads) -> None:
    """Reel rendering is continue-on-error, so a half-rendered batch is normal."""
    batch = _batch(tmp_path)
    renders = _renders(tmp_path, ["01-798444"])  # stats reel never rendered
    assert _run(batch, renders, monkeypatch) == 0

    data = json.loads((batch / "drafts.json").read_text(encoding="utf-8"))
    assert data["reels"][0].get("video_key") is None
    assert data["reels"][1]["video_key"] == f"marketing-reels/{DATE}/01-798444.mp4"


def test_no_renders_is_a_noop(tmp_path, monkeypatch, uploads) -> None:
    batch = _batch(tmp_path)
    renders = _renders(tmp_path, [])
    before = (batch / "drafts.json").read_text(encoding="utf-8")
    assert _run(batch, renders, monkeypatch) == 0
    assert uploads == []
    assert (batch / "drafts.json").read_text(encoding="utf-8") == before


def test_unconfigured_r2_exits_2_and_leaves_manifest_alone(tmp_path, monkeypatch) -> None:
    """Exit 2 is what makes the workflow fall back to the 14-day artifact."""
    def boom() -> None:
        raise storage.R2ConfigError("Missing private R2 config: R2_ACCOUNT_ID")

    monkeypatch.setattr(storage, "_require_private_config", boom)
    batch = _batch(tmp_path)
    renders = _renders(tmp_path, ["01-798444"])
    before = (batch / "drafts.json").read_text(encoding="utf-8")
    assert _run(batch, renders, monkeypatch) == 2
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
    batch = _batch(tmp_path)
    before = (batch / "drafts.json").read_text(encoding="utf-8")
    assert _run(batch, _renders(tmp_path, ["01-798444"]), monkeypatch) == 2
    assert (batch / "drafts.json").read_text(encoding="utf-8") == before


def test_every_upload_failing_exits_1(tmp_path, monkeypatch) -> None:
    def boom(local_path, key, content_type=None):
        raise RuntimeError("r2 down")

    monkeypatch.setattr(storage, "_require_private_config", lambda: None)
    monkeypatch.setattr(storage, "_require_config", lambda: None)
    monkeypatch.setattr(storage, "upload_file_private", boom)
    batch = _batch(tmp_path)
    renders = _renders(tmp_path, ["01-798444"])
    before = (batch / "drafts.json").read_text(encoding="utf-8")
    assert _run(batch, renders, monkeypatch) == 1
    # A failed run must not rewrite the manifest with half-truths.
    assert (batch / "drafts.json").read_text(encoding="utf-8") == before


def test_one_failure_does_not_block_the_rest(tmp_path, monkeypatch) -> None:
    def flaky(local_path, key, content_type=None):
        if "00-stats" in key:
            raise RuntimeError("r2 hiccup")
        return key

    monkeypatch.setattr(storage, "_require_private_config", lambda: None)
    monkeypatch.setattr(storage, "_require_config", lambda: None)
    monkeypatch.setattr(storage, "upload_file_private", flaky)
    batch = _batch(tmp_path)
    renders = _renders(tmp_path, ["00-stats", "01-798444"])
    assert _run(batch, renders, monkeypatch) == 0

    data = json.loads((batch / "drafts.json").read_text(encoding="utf-8"))
    assert data["reels"][0].get("video_key") is None
    assert data["reels"][1]["video_key"] == f"marketing-reels/{DATE}/01-798444.mp4"


def test_missing_manifest_is_a_noop(tmp_path, monkeypatch, uploads) -> None:
    empty = tmp_path / DATE
    empty.mkdir()
    assert _run(empty, _renders(tmp_path, ["01-798444"]), monkeypatch) == 0
    assert uploads == []


def test_rerun_is_idempotent(tmp_path, monkeypatch, uploads) -> None:
    """Same stem → same key, so a re-render overwrites instead of accumulating."""
    batch = _batch(tmp_path)
    renders = _renders(tmp_path, ["01-798444"])
    assert _run(batch, renders, monkeypatch) == 0
    first = json.loads((batch / "drafts.json").read_text(encoding="utf-8"))
    assert _run(batch, renders, monkeypatch) == 0
    second = json.loads((batch / "drafts.json").read_text(encoding="utf-8"))
    assert first == second
    assert len({k for _, k in uploads}) == 1
