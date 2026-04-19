"""
tests/test_storage.py
---------------------
Unit coverage for pipeline.storage — the R2 (Cloudflare) upload helpers
used by scripts/upload_downloads_to_r2.py.

These tests never hit the network; the boto3 client is monkey-patched
with an in-memory fake that records put/head calls.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _seed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://pub-xyz.r2.dev")


@pytest.fixture
def storage_module(monkeypatch):
    """Reload pipeline.storage with env seeded and a fresh lru_cache."""
    _seed_env(monkeypatch)
    if "pipeline.storage" in sys.modules:
        del sys.modules["pipeline.storage"]
    import pipeline.storage as storage  # noqa: WPS433
    storage.r2_client.cache_clear()
    return storage


def _install_fake_client(storage_module, monkeypatch) -> MagicMock:
    client = MagicMock()
    storage_module.r2_client.cache_clear()
    monkeypatch.setattr(storage_module, "r2_client", lambda: client)
    return client


def test_object_key_is_deterministic_and_safe(storage_module):
    # Identical inputs map to identical keys.
    assert storage_module.object_key("AUC-1", "notice.pdf") == "notices/AUC-1/notice.pdf"
    # Unsafe characters collapse to underscores.
    key = storage_module.object_key("AUC/1", "my notice (final).pdf")
    assert key.startswith("notices/AUC_1/")
    assert " " not in key and "(" not in key


def test_guess_content_type(storage_module):
    assert storage_module.guess_content_type("foo.pdf") == "application/pdf"
    assert storage_module.guess_content_type("FOO.PDF") == "application/pdf"
    assert storage_module.guess_content_type("pic.jpg") == "image/jpeg"
    assert storage_module.guess_content_type("pic.jpeg") == "image/jpeg"
    assert storage_module.guess_content_type("pic.png") == "image/png"
    # Unknown / extension-less filenames fall back to application/octet-stream.
    assert storage_module.guess_content_type("notes") == "application/octet-stream"
    assert storage_module.guess_content_type("file.q7z9x") == "application/octet-stream"


def test_doc_type_from_content_type(storage_module):
    assert storage_module.doc_type_from_content_type("application/pdf") == "pdf"
    assert storage_module.doc_type_from_content_type("image/jpeg") == "image"
    assert storage_module.doc_type_from_content_type("image/png") == "image"
    assert storage_module.doc_type_from_content_type("text/plain") == "other"


def test_public_url_for_uses_configured_base(storage_module):
    assert storage_module.public_url_for("notices/a/b.pdf") == \
        "https://pub-xyz.r2.dev/notices/a/b.pdf"


def test_upload_file_puts_object_and_returns_public_url(tmp_path, storage_module, monkeypatch):
    client = _install_fake_client(storage_module, monkeypatch)
    local = tmp_path / "notice.pdf"
    local.write_bytes(b"%PDF-1.4 fake")
    key = storage_module.object_key("AUC-9", local.name)

    url = storage_module.upload_file(local, key)

    assert url == f"https://pub-xyz.r2.dev/{key}"
    client.put_object.assert_called_once()
    kwargs = client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "bucket"
    assert kwargs["Key"] == key
    assert kwargs["ContentType"] == "application/pdf"


def test_upload_file_respects_explicit_content_type(tmp_path, storage_module, monkeypatch):
    client = _install_fake_client(storage_module, monkeypatch)
    local = tmp_path / "notice.bin"
    local.write_bytes(b"raw")
    key = storage_module.object_key("AUC-9", local.name)

    storage_module.upload_file(local, key, content_type="image/png")

    assert client.put_object.call_args.kwargs["ContentType"] == "image/png"


def test_upload_file_missing_path_raises(tmp_path, storage_module, monkeypatch):
    _install_fake_client(storage_module, monkeypatch)
    with pytest.raises(FileNotFoundError):
        storage_module.upload_file(tmp_path / "nope.pdf", "notices/x/nope.pdf")


def test_exists_true_when_head_succeeds(storage_module, monkeypatch):
    client = _install_fake_client(storage_module, monkeypatch)
    client.head_object.return_value = {"ContentLength": 1}
    assert storage_module.exists("notices/a/x.pdf") is True


def test_exists_false_on_404(storage_module, monkeypatch):
    # Import inside to match real import path.
    from botocore.exceptions import ClientError
    client = _install_fake_client(storage_module, monkeypatch)
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )
    assert storage_module.exists("notices/a/missing.pdf") is False


def test_exists_reraises_on_other_errors(storage_module, monkeypatch):
    from botocore.exceptions import ClientError
    client = _install_fake_client(storage_module, monkeypatch)
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "500", "Message": "Boom"}}, "HeadObject"
    )
    with pytest.raises(ClientError):
        storage_module.exists("notices/a/x.pdf")


def test_missing_env_raises_configured_error(monkeypatch):
    # Clear every R2 var and reload the module.
    for var in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "R2_BUCKET", "R2_PUBLIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    if "pipeline.storage" in sys.modules:
        del sys.modules["pipeline.storage"]
    import pipeline.storage as storage
    storage.r2_client.cache_clear()

    with pytest.raises(storage.R2ConfigError):
        storage.public_url_for("notices/x.pdf")
