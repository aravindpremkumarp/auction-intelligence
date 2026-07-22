"""Annotator per-block re-extract engine choice: _datalab_one_shot_png parses
Datalab output, and crop_and_reextract dispatches to the chosen engine.

Network-free — the HTTP one-shots and the image/crop helpers are monkeypatched.
"""
from __future__ import annotations

import asyncio

import pipeline.reextract as RX


DOC = {
    "block_type": "Document",
    "children": [
        {"block_type": "Page", "bbox": [0, 0, 1000, 1400], "children": [
            {"block_type": "Text", "html": "<p>crop text</p>", "bbox": [10, 10, 90, 40]},
        ]},
    ],
}


def test_datalab_one_shot_parses(monkeypatch):
    import pipeline.datalab_api as DLA
    monkeypatch.setattr(DLA, "run_file", lambda *a, **k: {"json": DOC})
    blocks = RX._datalab_one_shot_png(b"pngbytes", hint_name="x")
    assert blocks and blocks[0]["source"] == "datalab"
    assert blocks[0]["text"] == "crop text"


def _stub_crop(monkeypatch, calls):
    monkeypatch.setattr(RX, "_download_source", lambda url: (b"img", "image"))
    monkeypatch.setattr(RX, "_image_crop_to_png", lambda b, bbox: b"crop")
    monkeypatch.setattr(RX, "_pad_and_upscale_for_ocr", lambda b: b)
    monkeypatch.setattr(RX, "_datalab_one_shot_png",
                        lambda png, **kw: (calls.__setitem__("engine", "datalab"),
                                           [{"label": "Text", "text": "from-datalab",
                                             "bbox": [0, 0, 1, 1], "reading_order": 0}])[1])
    monkeypatch.setattr(RX, "_mineru_one_shot_png",
                        lambda png, **kw: (calls.__setitem__("engine", "mineru"),
                                           [{"label": "Text", "text": "from-mineru",
                                             "bbox": [0, 0, 1, 1], "reading_order": 0}])[1])


def test_crop_and_reextract_routes_to_datalab(monkeypatch):
    calls = {}
    _stub_crop(monkeypatch, calls)
    r = asyncio.run(RX.crop_and_reextract("http://x/n.jpg", 1, [0, 0, 1, 1],
                                          "Text", engine="datalab"))
    assert calls["engine"] == "datalab"
    assert r["text"] == "from-datalab"


def test_crop_and_reextract_routes_to_mineru(monkeypatch):
    calls = {}
    _stub_crop(monkeypatch, calls)
    r = asyncio.run(RX.crop_and_reextract("http://x/n.jpg", 1, [0, 0, 1, 1],
                                          "Text", engine="mineru"))
    assert calls["engine"] == "mineru"
    assert r["text"] == "from-mineru"


def test_crop_and_reextract_unknown_engine_falls_back_to_mineru(monkeypatch):
    calls = {}
    _stub_crop(monkeypatch, calls)
    r = asyncio.run(RX.crop_and_reextract("http://x/n.jpg", 1, [0, 0, 1, 1],
                                          "Text", engine="bogus"))
    assert calls["engine"] == "mineru"
    assert r["text"] == "from-mineru"
