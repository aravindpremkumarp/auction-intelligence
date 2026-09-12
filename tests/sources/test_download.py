"""``sources.download`` must never leave a partial file where a complete one goes.

The same truncation cases ``tests/scrapers/test_download_file.py`` holds the
phase2 scraper to, driven through the shared adapter downloader.
"""
from __future__ import annotations

import io
import types
import zipfile

import pytest

from sources import download as dl


class FakeResponse:
    """Streams `chunks`, then optionally dies the way a severed socket does."""

    def __init__(self, chunks, content_length=None, status_code=200, raise_at_end=None):
        self.status_code = status_code
        self._chunks = chunks
        self._raise_at_end = raise_at_end
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def iter_content(self, chunk_size=8192):
        yield from self._chunks
        if self._raise_at_end is not None:
            raise self._raise_at_end


def _serve(monkeypatch, response, seen=None):
    def get(url, **kw):
        if seen is not None:
            seen.append((url, kw))
        return response

    monkeypatch.setattr(dl.http, "get_session", lambda: types.SimpleNamespace(get=get))


def test_complete_download_lands_under_its_real_name(tmp_path, monkeypatch):
    body = b"x" * 8192 + b"tail"
    _serve(monkeypatch, FakeResponse([body[:8192], body[8192:]], content_length=len(body)))

    assert dl.download("https://host/notices/n.jpg", tmp_path) == tmp_path / "n.jpg"
    assert (tmp_path / "n.jpg").read_bytes() == body
    assert not list(tmp_path.glob("*.part"))


def test_short_body_is_rejected_and_leaves_nothing_behind(tmp_path, monkeypatch):
    _serve(monkeypatch, FakeResponse([b"x" * 8192] * 45, content_length=8192 * 60))

    assert dl.download("https://host/notices/n.jpg", tmp_path) is None
    assert not (tmp_path / "n.jpg").exists()
    assert not list(tmp_path.glob("*.part"))


def test_connection_death_mid_stream_leaves_nothing_behind(tmp_path, monkeypatch):
    _serve(monkeypatch, FakeResponse(
        [b"x" * 8192] * 3, content_length=8192 * 60, raise_at_end=ConnectionError("peer reset"),
    ))

    assert dl.download("https://host/notices/n.jpg", tmp_path) is None
    assert not (tmp_path / "n.jpg").exists()
    assert not list(tmp_path.glob("*.part"))


def test_empty_body_is_never_accepted(tmp_path, monkeypatch):
    _serve(monkeypatch, FakeResponse([], content_length=None))
    assert dl.download("https://host/notices/n.jpg", tmp_path) is None
    assert not (tmp_path / "n.jpg").exists()


def test_chunked_response_without_content_length_is_still_accepted(tmp_path, monkeypatch):
    _serve(monkeypatch, FakeResponse([b"z" * 100], content_length=None))
    assert dl.download("https://host/notices/n.jpg", tmp_path) == tmp_path / "n.jpg"


def test_non_200_writes_nothing(tmp_path, monkeypatch):
    _serve(monkeypatch, FakeResponse([b"not found"], status_code=404))
    assert dl.download("https://host/notices/n.jpg", tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_an_existing_file_is_not_re_downloaded(tmp_path, monkeypatch):
    (tmp_path / "n.jpg").write_bytes(b"already here")

    def explode():
        raise AssertionError("network touched for a file already on disk")

    monkeypatch.setattr(dl.http, "get_session", explode)
    assert dl.download("https://host/notices/n.jpg", tmp_path) == tmp_path / "n.jpg"


def test_explicit_filename_and_referer_are_honoured(tmp_path, monkeypatch):
    """bankeauctions' NIT zip refuses a bare GET; the Referer must reach the
    request, and adapters name files themselves so BAANKNET's
    'SALE NOTICE.pdf' cannot collide across listings."""
    seen = []
    _serve(monkeypatch, FakeResponse([b"PK\x03\x04"], content_length=4), seen)

    out = dl.download(
        "https://bankeauctions.com/public/uploads/event_auction/93bb.zip", tmp_path,
        filename="be-237860-nit.zip", referer="https://bankeauctions.com/immovable-land-chennai-237860",
    )
    assert out == tmp_path / "be-237860-nit.zip"
    assert seen[0][1]["headers"] == {"Referer": "https://bankeauctions.com/immovable-land-chennai-237860"}


@pytest.mark.parametrize("url, name", [
    ("https://cdn.baanknet.com/x/y/379330.pdf", "379330.pdf"),
    ("https://cdn.baanknet.com/x/y/379330.pdf?token=1", "379330.pdf"),
    ("https://host/download/12345", "12345.pdf"),
    ("https://host/images/2024/07/03/119851/119740.jpg", "119740.jpg"),
])
def test_filename_from_url(url, name):
    assert dl.filename_from_url(url) == name


def test_extract_zip_members_renames_and_is_idempotent(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Property Details - TEEZLE TELEMATICS.pdf", b"%PDF-1.4 photos")
        z.writestr("Omkara-Dinakaran-Chennai-23-08-2026.pdf", b"%PDF-1.4 paper")
        z.writestr("folder/", b"")
    zpath = tmp_path / "nit.zip"
    zpath.write_bytes(buf.getvalue())

    def rename(member):
        return "be-237860-" + member.lower().replace(" ", "-")

    out = dl.extract_zip_members(zpath, tmp_path / "docs", rename)
    names = sorted(p.name for _, p in out)
    assert names == [
        "be-237860-omkara-dinakaran-chennai-23-08-2026.pdf",
        "be-237860-property-details---teezle-telematics.pdf",
    ]
    assert (tmp_path / "docs" / names[1]).read_bytes() == b"%PDF-1.4 photos"
    assert not list((tmp_path / "docs").glob("*.part"))

    # Second pass rewrites nothing and still reports both members.
    again = dl.extract_zip_members(zpath, tmp_path / "docs", rename)
    assert sorted(p.name for _, p in again) == names
