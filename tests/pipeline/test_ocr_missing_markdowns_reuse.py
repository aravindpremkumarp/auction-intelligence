"""The OCR script hashes what it downloads, so one page is OCR'd once.

The hash is taken from the file already on disk — free next to the provider call
it saves — and a Document that carries one from an earlier run is not re-read.
DB-free: only the disk-touching half is exercised here; the planning arithmetic
lives in pipeline/notice_twins and is tested there.
"""
from __future__ import annotations

import hashlib

import scripts.ocr_missing_markdowns as M


def _stage_files(tmp_path, monkeypatch, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    monkeypatch.setattr(M, "DOWNLOAD_TARGET_DIR", tmp_path)


def test_identical_uploads_hash_alike(tmp_path, monkeypatch):
    page = b"\x89PNG one notice, six names"
    _stage_files(tmp_path, monkeypatch, {"a.jpg": page, "b.jpg": page,
                                         "c.jpg": b"another notice"})
    docs = [{"filename": n, "file_path": f"notices/{n}"}
            for n in ("a.jpg", "b.jpg", "c.jpg")]
    assert M.hash_downloaded(docs) == 3
    assert docs[0]["content_sha256"] == docs[1]["content_sha256"]
    assert docs[0]["content_sha256"] == hashlib.sha256(page).hexdigest()
    assert docs[2]["content_sha256"] != docs[0]["content_sha256"]


def test_an_existing_hash_is_left_alone(tmp_path, monkeypatch):
    _stage_files(tmp_path, monkeypatch, {"a.jpg": b"x"})
    docs = [{"filename": "a.jpg", "file_path": "notices/a.jpg",
             "content_sha256": "from-an-earlier-run"}]
    assert M.hash_downloaded(docs) == 0
    assert docs[0]["content_sha256"] == "from-an-earlier-run"


def test_an_unreadable_file_stays_unhashed_and_is_still_ocrd(tmp_path,
                                                             monkeypatch,
                                                             capsys):
    _stage_files(tmp_path, monkeypatch, {})
    docs = [{"filename": "gone.jpg", "file_path": "notices/gone.jpg"}]
    assert M.hash_downloaded(docs) == 0
    assert "content_sha256" not in docs[0]
    assert "hash failed" in capsys.readouterr().out
    # No hash means its own group with no donor — the pass runs as before.
    to_run, copies = M.plan_reuse(docs, {}, key=M.source_key)
    assert [d["filename"] for d in to_run] == ["gone.jpg"]
    assert copies == []


def test_write_content_hashes_skips_rows_it_cannot_address(monkeypatch):
    sent: list = []
    monkeypatch.setattr(M, "run_query", lambda q, p: sent.append(p) or [])
    n = M.write_content_hashes([
        {"file_path": "notices/a.jpg", "content_sha256": "sha-a"},
        {"file_path": "notices/b.jpg"},                       # never hashed
        {"content_sha256": "sha-c"},                          # no file_path
    ])
    assert n == 1
    assert sent == [{"rows": [{"file_path": "notices/a.jpg", "sha": "sha-a"}]}]
