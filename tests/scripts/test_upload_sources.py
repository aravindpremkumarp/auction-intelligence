"""upload_downloads_to_r2 on harvested listings: files are found under
downloads/<source>/, each :Document gets the adapter's role and source, and
a live listing's photo is mirrored under a content-addressed key with the
fingerprint written back to its :Media node. No R2, no Neo4j — both are
monkeypatched, as in tests/test_upload_canonical.py.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import scripts.upload_downloads_to_r2 as up


def _no_r2(monkeypatch, uploads: dict):
    monkeypatch.setattr(up, "lookup_canonical", lambda fn: None)
    monkeypatch.setattr(up.storage, "exists", lambda k: False)
    monkeypatch.setattr(up.storage, "public_url_for", lambda k: f"https://r2/{k}")

    def _upload(path, key, content_type):
        uploads[key] = (Path(path).name, content_type)
        return f"https://r2/{key}"
    monkeypatch.setattr(up.storage, "upload_file", _upload)


def test_locate_local_file_tries_the_source_dir_first(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "DOWNLOADS_DIR", tmp_path)
    monkeypatch.setattr(up, "_DOWNLOAD_SEARCH_DIRS", [tmp_path / "live_properties", tmp_path])
    (tmp_path / "bankeauctions").mkdir()
    (tmp_path / "bankeauctions" / "be-1-tender.pdf").write_bytes(b"%PDF")
    (tmp_path / "legacy.jpg").write_bytes(b"x")
    assert up.locate_local_file("be-1-tender.pdf", "bankeauctions") == tmp_path / "bankeauctions" / "be-1-tender.pdf"
    assert up.locate_local_file("be-1-tender.pdf") is None            # not in a legacy dir
    assert up.locate_local_file("legacy.jpg", "baanknet") == tmp_path / "legacy.jpg"   # falls back


def test_documents_carry_source_and_role_by_position(tmp_path, monkeypatch):
    uploads: dict = {}
    _no_r2(monkeypatch, uploads)
    src = tmp_path / "bankeauctions"
    src.mkdir()
    for n in ("be-1-sale-notice.pdf", "be-1-tender.pdf", "be-1-extra.pdf"):
        (src / n).write_bytes(b"%PDF")
    monkeypatch.setattr(up, "DOWNLOADS_DIR", tmp_path)
    seen: list[dict] = []
    monkeypatch.setattr(up, "upsert_document", lambda **kw: seen.append(kw))

    res = up.UploadResult()
    up.process_auction("be-1", ["be-1-sale-notice.pdf", "be-1-tender.pdf", "be-1-extra.pdf"], dry_run=False, result=res,
                       source="bankeauctions", roles=["sale_notice", "tender"])      # one role short

    assert res.uploaded == 3 and res.graph_updates == 3 and res.errors == 0
    assert [(d["filename"], d["source"], d["doc_role"]) for d in seen] == [
        ("be-1-sale-notice.pdf", "bankeauctions", "sale_notice"),
        ("be-1-tender.pdf", "bankeauctions", "tender"),
        ("be-1-extra.pdf", "bankeauctions", None),
    ]
    assert set(uploads) == {"notices/be-1/be-1-sale-notice.pdf", "notices/be-1/be-1-tender.pdf", "notices/be-1/be-1-extra.pdf"}


def test_upsert_never_overwrites_an_existing_role_or_source():
    on_match = up.UPSERT_DOC_CYPHER.split("ON MATCH")[1]
    assert "doc.source       = coalesce(doc.source,       $source)" in on_match
    assert "doc.doc_role     = coalesce(doc.doc_role,     $doc_role)" in on_match


def test_local_photo_path_uses_the_harvest_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "DOWNLOADS_DIR", tmp_path)
    url = "https://cdn.baanknet.com/Production/Application-Documents/Generic-Instance/Property/Images/2024/07/03/119851/119740.jpg"
    assert up.local_photo_path("baanknet", url) == tmp_path / "baanknet" / "media" / "bn-119740.jpg"
    assert up.local_photo_path(None, "https://x/p.png") == tmp_path / "eauctionsindia" / "media" / "p.png"


def test_photo_is_mirrored_by_content_and_written_back(tmp_path, monkeypatch):
    uploads: dict = {}
    _no_r2(monkeypatch, uploads)
    monkeypatch.setattr(up, "DOWNLOADS_DIR", tmp_path)
    media = tmp_path / "baanknet" / "media"
    media.mkdir(parents=True)
    (media / "bn-119740.jpg").write_bytes(b"\xff\xd8jpeg")
    sha = hashlib.sha256(b"\xff\xd8jpeg").hexdigest()
    writes: list[tuple] = []
    monkeypatch.setattr(up, "run_query", lambda cypher, params=None: writes.append((cypher, params)) or [])

    res = up.UploadResult()
    url = "https://cdn.baanknet.com/x/119851/119740.jpg"
    up.process_photo("bn-359826", "baanknet", url, dry_run=False, result=res)
    up.process_photo("bn-359826", "baanknet", "https://cdn.baanknet.com/x/119851/gone.jpg", dry_run=False, result=res)

    key = f"media/bn-359826/{sha}.jpg"
    assert (res.photos_uploaded, res.photos_missing, res.errors) == (1, 1, 0)
    assert uploads == {key: ("bn-119740.jpg", "image/jpeg")}
    [(cypher, params)] = writes
    assert "MATCH (m:Media {url: $url})" in cypher
    assert params == {"url": url, "sha": sha, "key": key, "public_url": f"https://r2/{key}", "content_type": "image/jpeg"}


def test_photo_dry_run_touches_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(up, "DOWNLOADS_DIR", tmp_path)
    media = tmp_path / "baanknet" / "media"
    media.mkdir(parents=True)
    (media / "bn-1.jpg").write_bytes(b"x")
    monkeypatch.setattr(up.storage, "exists", lambda k: (_ for _ in ()).throw(AssertionError("no R2 in dry-run")))
    monkeypatch.setattr(up, "run_query", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no graph in dry-run")))
    res = up.UploadResult()
    up.process_photo("bn-1", "baanknet", "https://cdn/1.jpg", dry_run=True, result=res)
    assert "would mirror" in capsys.readouterr().out and res.photos_uploaded == 0
