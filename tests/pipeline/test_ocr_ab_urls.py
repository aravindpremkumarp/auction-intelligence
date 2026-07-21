"""scripts.ocr_ab URL input handling: _read_url_list parsing + collect_files
routing through the download path. Network-free — _download_url is stubbed.
"""
from __future__ import annotations

from pathlib import Path

import scripts.ocr_ab as AB


class _Args:
    """Minimal stand-in for the argparse Namespace collect_files reads."""
    def __init__(self, **kw):
        self.files = None
        self.urls = None
        self.urls_file = None
        self.dir = None
        self.from_worklist = False
        self.missing_only = False
        self.limit = None
        for k, v in kw.items():
            setattr(self, k, v)


def test_read_url_list_parses_and_dedupes(tmp_path):
    f = tmp_path / "u.txt"
    f.write_text("https://a/x.pdf\n# comment\nhttps://b/y.jpg, https://a/x.pdf\n\n",
                 encoding="utf-8")
    args = _Args(urls=["https://c/z.png"], urls_file=str(f))
    assert AB._read_url_list(args) == [
        "https://c/z.png", "https://a/x.pdf", "https://b/y.jpg",
    ]


def test_read_url_list_splits_single_line_space_separated(tmp_path):
    # The bug the workflow hit: workflow_dispatch inputs are single-line, so a
    # "one per line" paste arrives as one space-joined line. Must still split.
    f = tmp_path / "u.txt"
    f.write_text("https://a/1.jpg https://a/2.png  https://a/3.pdf\n", encoding="utf-8")
    args = _Args(urls_file=str(f))
    assert AB._read_url_list(args) == [
        "https://a/1.jpg", "https://a/2.png", "https://a/3.pdf",
    ]


def test_read_url_list_comment_line_does_not_leak_tokens(tmp_path):
    f = tmp_path / "u.txt"
    f.write_text("# these are notices\nhttps://a/1.jpg\n", encoding="utf-8")
    assert AB._read_url_list(_Args(urls_file=str(f))) == ["https://a/1.jpg"]


def test_collect_files_routes_urls(monkeypatch):
    seen = []

    def fake_dl(url, idx):
        seen.append((url, idx))
        return {"filename": f"{idx:03d}.pdf", "file_path": url,
                "disk_path": Path(f"/tmp/{idx:03d}.pdf")}

    monkeypatch.setattr(AB, "_download_url", fake_dl)
    items = AB.collect_files(_Args(urls=["https://a/1.pdf", "https://a/2.pdf"]))
    assert [it["file_path"] for it in items] == ["https://a/1.pdf", "https://a/2.pdf"]
    assert seen == [("https://a/1.pdf", 0), ("https://a/2.pdf", 1)]


def test_collect_files_url_limit(monkeypatch):
    monkeypatch.setattr(AB, "_download_url",
                        lambda url, idx: {"filename": "n", "file_path": url,
                                          "disk_path": Path("/tmp/x")})
    items = AB.collect_files(_Args(urls=["https://a/1", "https://a/2", "https://a/3"],
                                   limit=2))
    assert len(items) == 2


def test_collect_files_skips_failed_download(monkeypatch):
    monkeypatch.setattr(AB, "_download_url",
                        lambda url, idx: None if "bad" in url
                        else {"filename": "n", "file_path": url,
                              "disk_path": Path("/tmp/x")})
    items = AB.collect_files(_Args(urls=["https://a/bad", "https://a/good"]))
    assert [it["file_path"] for it in items] == ["https://a/good"]
