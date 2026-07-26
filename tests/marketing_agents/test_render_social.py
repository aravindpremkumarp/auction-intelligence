"""Unit test for render_social.render_staged — the batch card renderer used by
the content-poster workflow. The actual browser render (render()) is faked, so
this pins the manifest-driven logic: every staged card is rendered to a UNIQUE
filename (plain --template would overwrite same-template cards).
"""
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "render_social", REPO / "marketing" / "render_social.py")
render_social = importlib.util.module_from_spec(spec)
sys.modules["render_social"] = render_social
spec.loader.exec_module(render_social)   # imports clean now that playwright is lazy


def _stage(tmp_path: Path) -> Path:
    date_dir = tmp_path / "2026-07-15"
    (date_dir / "cards").mkdir(parents=True)
    # Two cards on the SAME template — the collision case unique names fix.
    for name in ("01-A1.json", "02-A2.json"):
        (date_dir / "cards" / name).write_text("{}", encoding="utf-8")
    manifest = {"cards": [
        {"draft_index": 1, "auction_id": "A1", "template": "deal-of-the-day-1080",
         "data": "cards/01-A1.json", "headline": "x"},
        {"draft_index": 2, "auction_id": "A2", "template": "deal-of-the-day-1080",
         "data": "cards/02-A2.json", "headline": "y"},
    ]}
    (date_dir / "drafts.json").write_text(json.dumps(manifest), encoding="utf-8")
    return date_dir


def test_render_staged_renders_each_card_to_unique_name(tmp_path, monkeypatch):
    calls = []

    def fake_render(template, data_file, out_dir, out_name=None):
        calls.append({"template": template, "data": data_file,
                      "out_dir": str(out_dir), "out_name": out_name})
        return [out_dir / out_name]

    monkeypatch.setattr(render_social, "render", fake_render)
    date_dir = _stage(tmp_path)
    render_social.render_staged(date_dir)

    assert len(calls) == 2
    # Unique, index-and-id based names → no same-template overwrite.
    assert calls[0]["out_name"] == "card-01-A1.png"
    assert calls[1]["out_name"] == "card-02-A2.png"
    # Rendered into <date_dir>/rendered/, from the manifest's data paths.
    assert calls[0]["out_dir"].endswith("2026-07-15/rendered")
    assert calls[0]["data"].endswith("cards/01-A1.json")


def test_render_staged_includes_the_carousel(tmp_path, monkeypatch):
    """The carousel is just another `cards` row — render_staged needs no special
    case, it only has to pass the name through for the multi-slide render."""
    calls = []

    def fake_render(template, data_file, out_dir, out_name=None):
        calls.append(out_name)
        return [out_dir / (out_name or "x.png")]

    monkeypatch.setattr(render_social, "render", fake_render)
    date_dir = _stage(tmp_path)
    manifest = json.loads((date_dir / "drafts.json").read_text())
    manifest["cards"].insert(0, {
        "draft_index": 0, "auction_id": "carousel",
        "template": "city-carousel-1080x1350",
        "data": "cards/00-carousel.json", "headline": "z", "slides": 7})
    (date_dir / "cards" / "00-carousel.json").write_text("{}", encoding="utf-8")
    (date_dir / "drafts.json").write_text(json.dumps(manifest), encoding="utf-8")

    render_social.render_staged(date_dir)
    assert calls[0] == "card-00-carousel.png"


class TestStageName:
    """One .stage → one PNG. The carousel is the only multi-stage template, and
    it must not collapse its slides onto a single filename."""

    def test_named_single_stage_card_keeps_its_exact_name(self):
        assert render_social.stage_name(
            "deal-of-the-day-1080", "card-01-A1.png", 1, multi=False) == "card-01-A1.png"

    def test_named_multi_stage_carousel_numbers_every_slide(self):
        names = [render_social.stage_name("city-carousel-1080x1350",
                                          "card-00-carousel.png", i, multi=True)
                 for i in (1, 2, 7)]
        assert names == ["card-00-carousel_01.png", "card-00-carousel_02.png",
                         "card-00-carousel_07.png"]

    def test_unnamed_falls_back_to_the_template_stem(self):
        assert render_social.stage_name("price-drop-1080x1350", None, 1, multi=False) \
            == "price-drop-1080x1350.png"
        assert render_social.stage_name("city-carousel-1080x1350", None, 3, multi=True) \
            == "city-carousel-1080x1350_03.png"


def test_render_staged_no_cards_is_noop(tmp_path):
    date_dir = tmp_path / "2026-07-15"
    date_dir.mkdir()
    (date_dir / "drafts.json").write_text('{"cards": []}', encoding="utf-8")
    assert render_social.render_staged(date_dir) == []
