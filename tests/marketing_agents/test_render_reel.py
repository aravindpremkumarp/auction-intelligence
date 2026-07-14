"""Unit tests for the reel renderer's pure logic (no subprocess, no render).

The actual `hyperframes render` path is exercised by the CI workflow and by
running `python marketing/render_reel.py` locally — see the module docstring.
"""

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "render_reel", REPO / "marketing" / "render_reel.py")
render_reel = importlib.util.module_from_spec(spec)
sys.modules["render_reel"] = render_reel
spec.loader.exec_module(render_reel)


class TestStageProject:
    ISLAND = json.dumps({
        "angle": "cheapest",
        "hook": {"line1": "₹7.5L", "line2": "cheapest in chennai."},
        "context_lines": ["a bank listed it", "here's the number"],
        "money": {"previous_reserve_price": None, "reserve_price": 750000,
                  "drop_pct": None},
        "facts": {"title": "Plot", "city": "Chennai", "bank": "SBI",
                  "asset_type": "Plot", "emd": 75000,
                  "auction_date": "24 Jul 2026", "days_left": 9},
        "endcard": {"question": "would you bid?", "save_line": "",
                    "url": "auctionscope.in"},
        "honesty_line": "Reserve, EMD and date from the bank's auction notice.",
    })

    def test_stages_index_lib_and_sidecar(self, tmp_path):
        stage = tmp_path / "proj"
        render_reel.stage_project("deal-reel-1080x1920", self.ISLAND, stage)
        assert (stage / "index.html").exists()
        assert (stage / "lib" / "gsap.min.js").exists()
        assert (stage / "lib" / "fonts" / "bricolage-grotesque-latin.woff2").exists()
        assert (stage / "index.motion.json").exists()  # assertions travel along

    def test_island_is_substituted(self, tmp_path):
        stage = tmp_path / "proj"
        render_reel.stage_project("deal-reel-1080x1920", self.ISLAND, stage)
        html = (stage / "index.html").read_text(encoding="utf-8")
        m = render_reel.DATA_ISLAND.search(html)
        island = json.loads(m.group(0).split(">", 1)[1].rsplit("<", 1)[0])
        # The island is fully replaced (the DOM keeps harmless placeholder
        # text that the runtime JS overwrites before the timeline registers).
        assert island["hook"]["line2"] == "cheapest in chennai."
        assert island["facts"]["city"] == "Chennai"

    def test_no_island_keeps_template_sample(self, tmp_path):
        stage = tmp_path / "proj"
        render_reel.stage_project("deal-reel-1080x1920", None, stage)
        html = (stage / "index.html").read_text(encoding="utf-8")
        assert "nobody bid." in html  # design-check mode


class TestManifestJobs:
    def test_reads_reel_rows_with_templates(self, tmp_path):
        (tmp_path / "reels").mkdir()
        manifest = {
            "reels": [
                {"draft_index": 0, "auction_id": "stats",
                 "template": "stats-reel-1080x1920", "data": "reels/00-stats.json"},
                {"draft_index": 1, "auction_id": "X1",
                 "template": "deal-reel-1080x1920", "data": "reels/01-X1.json"},
            ]
        }
        mp = tmp_path / "drafts.json"
        mp.write_text(json.dumps(manifest), encoding="utf-8")
        jobs = render_reel.manifest_jobs(mp)
        assert [(t, s) for t, _, s in jobs] == [
            ("stats-reel-1080x1920", "00-stats"),
            ("deal-reel-1080x1920", "01-X1"),
        ]
        assert jobs[1][1] == tmp_path / "reels/01-X1.json"

    def test_empty_manifest_yields_no_jobs(self, tmp_path):
        mp = tmp_path / "drafts.json"
        mp.write_text(json.dumps({"drafts": []}), encoding="utf-8")
        assert render_reel.manifest_jobs(mp) == []


class TestCliResolution:
    def test_prefers_local_pinned_install(self):
        cmd = render_reel.hyperframes_cmd()
        # Either the local videos/ install (dev sandbox) or the pinned npx
        # fallback — never an unpinned `npx hyperframes`.
        if cmd[0] == "node":
            assert cmd[1].endswith("videos/node_modules/.bin/hyperframes")
        else:
            assert cmd[:2] == ["npx", "-y"]
            assert cmd[2] == f"hyperframes@{render_reel.HYPERFRAMES_VERSION}"
