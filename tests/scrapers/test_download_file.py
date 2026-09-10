"""`download_file` must never leave a partial download where a complete one goes.

Two notices in the live corpus reached R2 as headless JPEGs — 368,640 and
1,220,608 bytes, exactly 45 and 149 whole 8192-byte chunks. `iter_content`
ends normally when a connection drops mid-transfer, so the loop could not tell
a severed body from a finished one, and the bytes were written straight to the
final name. The `os.path.exists` skip then adopted that stump on every later
run, and the upload path gates on key existence alone, so it shipped.

These tests drive the truncation case directly rather than the happy path:
a short read must leave NOTHING behind that a later run would mistake for a
download.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# The module imports the Selenium helper at import time; nothing here touches
# a browser, so a stub keeps the test off undetected_chromedriver.
sys.modules.setdefault("utils", types.ModuleType("utils"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scrapers"))

import phase2_scrape_details as p2  # noqa: E402


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


@pytest.fixture
def download_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(p2, "DOWNLOAD_DIR", str(tmp_path))
    return tmp_path


def _serve(monkeypatch, response):
    session = types.SimpleNamespace(get=lambda *a, **kw: response)
    monkeypatch.setattr(p2, "get_dl_session", lambda: session)


def test_complete_download_lands_under_its_real_name(download_dir, monkeypatch):
    body = b"x" * 8192 + b"tail"
    _serve(monkeypatch, FakeResponse([body[:8192], body[8192:]], content_length=len(body)))

    assert p2.download_file("https://host/notices/n.jpg") == "n.jpg"
    assert (download_dir / "n.jpg").read_bytes() == body
    # No scratch file survives a success.
    assert not list(download_dir.glob("*.part"))


def test_short_body_is_rejected_and_leaves_nothing_behind(download_dir, monkeypatch):
    """The exact corpus failure: the stream ends early but without an error.

    45 chunks arrive of a file the server said was longer. The old code wrote
    those to `n.jpg` and returned it as a success.
    """
    _serve(monkeypatch, FakeResponse([b"x" * 8192] * 45, content_length=8192 * 60))

    assert p2.download_file("https://host/notices/n.jpg") is None
    assert not (download_dir / "n.jpg").exists()
    assert not list(download_dir.glob("*.part"))


def test_connection_death_mid_stream_leaves_nothing_behind(download_dir, monkeypatch):
    """Same truncation, surfaced as an exception instead of a short read.

    `except Exception: pass` swallowed this, but the partial file was already
    on disk by the time it fired.
    """
    _serve(monkeypatch, FakeResponse(
        [b"x" * 8192] * 3,
        content_length=8192 * 60,
        raise_at_end=ConnectionError("peer reset"),
    ))

    assert p2.download_file("https://host/notices/n.jpg") is None
    assert not (download_dir / "n.jpg").exists()
    assert not list(download_dir.glob("*.part"))


def test_a_rejected_download_does_not_poison_the_next_run(download_dir, monkeypatch):
    """The compounding half of the bug.

    A truncated file under the final name made `os.path.exists` report the
    notice as already downloaded, so it was never re-fetched — which is why
    both bad files sat in R2 indefinitely. A failed attempt must leave the
    next attempt free to succeed.
    """
    _serve(monkeypatch, FakeResponse([b"x" * 8192], content_length=8192 * 60))
    assert p2.download_file("https://host/notices/n.jpg") is None

    body = b"y" * 4096
    _serve(monkeypatch, FakeResponse([body], content_length=len(body)))
    assert p2.download_file("https://host/notices/n.jpg") == "n.jpg"
    assert (download_dir / "n.jpg").read_bytes() == body


def test_empty_body_is_never_accepted(download_dir, monkeypatch):
    """A 0-byte notice is always a failure, Content-Length or not."""
    _serve(monkeypatch, FakeResponse([], content_length=None))

    assert p2.download_file("https://host/notices/n.jpg") is None
    assert not (download_dir / "n.jpg").exists()


def test_chunked_response_without_content_length_is_still_accepted(download_dir, monkeypatch):
    """No length header means no way to verify — keep the old behaviour rather
    than dropping notices the server streams chunked."""
    _serve(monkeypatch, FakeResponse([b"z" * 100], content_length=None))

    assert p2.download_file("https://host/notices/n.jpg") == "n.jpg"
    assert (download_dir / "n.jpg").read_bytes() == b"z" * 100


def test_an_existing_file_is_not_re_downloaded(download_dir, monkeypatch):
    (download_dir / "n.jpg").write_bytes(b"already here")

    def explode():
        raise AssertionError("network touched for a file already on disk")

    monkeypatch.setattr(p2, "get_dl_session", explode)
    assert p2.download_file("https://host/notices/n.jpg") == "n.jpg"


def test_non_200_writes_nothing(download_dir, monkeypatch):
    _serve(monkeypatch, FakeResponse([b"not found"], status_code=404))

    assert p2.download_file("https://host/notices/n.jpg") is None
    assert not (download_dir / "n.jpg").exists()
    assert not list(download_dir.glob("*.part"))


def test_part_files_of_concurrent_attempts_do_not_collide(download_dir, monkeypatch):
    """N_WORKERS threads can race on one URL; a shared scratch name would let
    two writers interleave into a single corrupt file."""
    import threading

    names = []
    real_replace = p2.os.replace

    def capture(src, dst):
        names.append(src)
        real_replace(src, dst)

    monkeypatch.setattr(p2.os, "replace", capture)
    _serve(monkeypatch, FakeResponse([b"a" * 10], content_length=10))

    p2.download_file("https://host/notices/n.jpg")
    (download_dir / "n.jpg").unlink()

    done = threading.Event()

    def other():
        p2.download_file("https://host/notices/n.jpg")
        done.set()

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert done.is_set()
    assert len(names) == 2 and names[0] != names[1]
