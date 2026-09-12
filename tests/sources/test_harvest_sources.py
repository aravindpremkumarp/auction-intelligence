"""The harvest CLI's plumbing, driven with a fake adapter — no network.

What it must get right: raw records are appended verbatim, listings are
rewritten, a bundle is unpacked and replaced by its members, found/missing
lists reflect the disk, photos are mirrored only for live listings, and a
source that yields nothing fails the run.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from scripts import harvest_sources as hs
from sources.base import DocRef, Listing, MediaRef


class FakeAdapter:
    name = "fake"
    id_prefix = "fk-"
    source_rank = 9
    session = None

    def __init__(self, raws, listings):
        self._raws, self._listings = raws, listings
        self.fetched: list[str] = []

    def harvest(self, *, limit=None):
        for i, raw in enumerate(self._raws):
            if limit is not None and i >= limit:
                return
            yield raw

    def normalize(self, raw):
        return self._listings.get(raw["id"])

    def fetch_document(self, ref, dest_dir):
        self.fetched.append(ref.filename)
        if ref.doc_role == "missing":
            return None
        p = Path(dest_dir) / ref.filename
        p.parent.mkdir(parents=True, exist_ok=True)
        if ref.doc_role == "bundle":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("Sale Proclamation dated 23.08.2026.pdf", b"%PDF")
                z.writestr("Omkara-Dinakaran-Chennai-23-08-2026.pdf", b"%PDF")
            p.write_bytes(buf.getvalue())
        else:
            p.write_bytes(b"%PDF")
        return p

    def expand_bundle(self, ref, zip_path, dest_dir):
        from sources.download import extract_zip_members
        from sources.normalize import doc_role_for
        members = extract_zip_members(zip_path, dest_dir, lambda n: "fk-1-" + n.lower().replace(" ", "-"))
        return [DocRef(url=f"{ref.url}#{n}", filename=p.name, label=n, doc_role=doc_role_for(n)) for n, p in members]


def _listing(aid, *, docs=(), media=(), status="live"):
    return Listing(source="fake", source_id=aid, auction_id=f"fk-{aid}", source_url="https://x/" + aid, source_rank=9,
                   documents=list(docs), media=list(media), auction_status=status)


def test_run_writes_raw_listings_and_unpacks_bundles(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hs, "download", lambda url, d, **kw: calls.append(url) or (Path(d) / "photo.jpg"))

    raws = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    listings = {
        "1": _listing("1", docs=[DocRef(url="https://x/nit.zip", filename="fk-1-nit.zip", doc_role="bundle"),
                                 DocRef(url="https://x/t.pdf", filename="fk-1-tender.pdf", doc_role="tender"),
                                 DocRef(url="https://x/gone.pdf", filename="fk-1-gone.pdf", doc_role="missing")],
                      media=[MediaRef(url="https://cdn/p1.jpg", kind="image", is_main=True),
                             MediaRef(url="https://cdn/v.mp4", kind="video")]),
        "2": _listing("2", media=[MediaRef(url="https://cdn/p2.jpg", kind="image")], status="ended"),
        # "3" → normalize returns None → dropped
    }
    ad = FakeAdapter(raws, listings)
    [s] = hs.run(sources=["fake"], data_dir=tmp_path / "data", downloads_dir=tmp_path / "dl", adapters={"fake": ad})

    assert (s.harvested, s.listings, s.dropped) == (3, 2, 1)
    assert (s.documents_fetched, s.documents_failed, s.bundle_members) == (2, 1, 2)
    assert (s.media_fetched, s.media_failed) == (1, 0)
    assert calls == ["https://cdn/p1.jpg"]          # live listing's image only; no video; not the ended one

    raw_files = list((tmp_path / "data" / "raw" / "fake").glob("*.jsonl"))
    assert len(raw_files) == 1
    raw_lines = [json.loads(line) for line in raw_files[0].read_text().splitlines()]
    assert [r["raw"]["id"] for r in raw_lines] == ["1", "2", "3"] and raw_lines[0]["source"] == "fake"

    rows = [json.loads(line) for line in (tmp_path / "data" / "listings" / "fake.jsonl").read_text().splitlines()]
    assert [r["auction_id"] for r in rows] == ["fk-1", "fk-2"]
    docs = rows[0]["documents"]
    # the bundle is gone, its members are in, roles routed by file name
    assert [(d["filename"], d["doc_role"]) for d in docs] == [
        ("fk-1-sale-proclamation-dated-23.08.2026.pdf", "proclamation"),
        ("fk-1-omkara-dinakaran-chennai-23-08-2026.pdf", "publication"),
        ("fk-1-tender.pdf", "tender"),
        ("fk-1-gone.pdf", "missing"),
    ]
    assert rows[0]["downloads_missing"] == ["fk-1-gone.pdf"] and rows[0]["downloads_complete"] is False
    assert rows[1]["downloads_complete"] is True


def test_raw_layer_appends_and_listings_rewrite(tmp_path):
    ad = FakeAdapter([{"id": "1"}], {"1": _listing("1")})
    hs.run(sources=["fake"], data_dir=tmp_path / "data", downloads_dir=tmp_path / "dl", adapters={"fake": ad}, do_download=False)
    hs.run(sources=["fake"], data_dir=tmp_path / "data", downloads_dir=tmp_path / "dl", adapters={"fake": ad}, do_download=False)
    raw = next((tmp_path / "data" / "raw" / "fake").glob("*.jsonl")).read_text().splitlines()
    rows = (tmp_path / "data" / "listings" / "fake.jsonl").read_text().splitlines()
    assert len(raw) == 2 and len(rows) == 1


def test_main_fails_when_a_source_yields_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "ADAPTERS", {"fake": "x:y"})
    monkeypatch.setattr(hs, "get_adapter", lambda name, **kw: FakeAdapter([], {}))
    rc = hs.main(["--source", "fake", "--data-dir", str(tmp_path / "d"), "--downloads-dir", str(tmp_path / "dl")])
    assert rc == 1


def test_main_rejects_unknown_source(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        hs.main(["--source", "nope"])
