"""
tests/marketing/test_channel_research.py
----------------------------------------
Pure-logic coverage for marketing/research/ — the Instagram/X channel
research CLI. No network, no instaloader/tweepy needed (both are imported
lazily by the modules under test).

Not part of the CI gate (pytest testpaths is tests/api).
Run: pytest tests/marketing tests/test_storage.py -q
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from marketing.research import schema  # noqa: E402
from marketing.research.cli import DEFAULT_OUT_DIR, resolve_out_dir  # noqa: E402
from marketing.research.instagram import _post_to_research, parse_shortcode  # noqa: E402
from marketing.research.twitter import parse_tweet_id, pick_video_variant  # noqa: E402


class TestParseShortcode:
    @pytest.mark.parametrize("url", [
        "https://www.instagram.com/p/Cxyz12_-abc/",
        "https://www.instagram.com/reel/Cxyz12_-abc/?utm_source=ig_web_copy_link",
        "https://instagram.com/reels/Cxyz12_-abc",
        "https://www.instagram.com/tv/Cxyz12_-abc/?hl=en",
    ])
    def test_url_shapes(self, url):
        assert parse_shortcode(url) == "Cxyz12_-abc"

    def test_bare_shortcode_passes_through(self):
        assert parse_shortcode("DIoqZc9JPWd") == "DIoqZc9JPWd"
        assert parse_shortcode("  DIoqZc9JPWd  ") == "DIoqZc9JPWd"

    @pytest.mark.parametrize("bad", [
        "https://www.instagram.com/natgeo/",   # profile, not a post
        "https://example.com/reel/abc123",
        "not a url at all",
        "",
    ])
    def test_garbage_raises(self, bad):
        with pytest.raises(ValueError):
            parse_shortcode(bad)


class TestParseTweetId:
    @pytest.mark.parametrize("url", [
        "https://x.com/SpaceX/status/1914374267606585757",
        "https://twitter.com/SpaceX/status/1914374267606585757?s=20",
        "https://mobile.twitter.com/SpaceX/statuses/1914374267606585757",
        "x.com/SpaceX/status/1914374267606585757/photo/1",
    ])
    def test_url_shapes(self, url):
        assert parse_tweet_id(url) == "1914374267606585757"

    def test_bare_id_passes_through(self):
        assert parse_tweet_id("1914374267606585757") == "1914374267606585757"

    @pytest.mark.parametrize("bad", ["https://x.com/SpaceX", "abc", ""])
    def test_garbage_raises(self, bad):
        with pytest.raises(ValueError):
            parse_tweet_id(bad)


class TestTopNComments:
    def test_sorts_desc_and_caps_at_n(self):
        comments = [
            SimpleNamespace(text="low", likes_count=1),
            SimpleNamespace(text="high", likes_count=50),
            SimpleNamespace(text="mid", likes_count=10),
            SimpleNamespace(text="zero", likes_count=0),
        ]
        top = schema.top_n_comments(comments, n=3)
        assert [c["text"] for c in top] == ["high", "mid", "low"]

    def test_missing_likes_count_counts_as_zero(self):
        # The attribute is not reliably present on instaloader comments —
        # the original notebook crashed here.
        comments = [SimpleNamespace(text="no likes attr"),
                    SimpleNamespace(text="liked", likes_count=2),
                    SimpleNamespace(text="none likes", likes_count=None)]
        top = schema.top_n_comments(comments)
        assert [c["likes"] for c in top] == [2, 0, 0]

    def test_accepts_dicts_from_the_x_path(self):
        replies = [{"username": "a", "text": "t1", "likes": 5},
                   {"username": "b", "text": "t2", "likes": 9}]
        top = schema.top_n_comments(replies)
        assert top[0] == {"username": "b", "text": "t2", "likes": 9}

    def test_owner_username_extracted(self):
        c = SimpleNamespace(text="hi", likes_count=1,
                            owner=SimpleNamespace(username="someone"))
        assert schema.top_n_comments([c])[0]["username"] == "someone"

    def test_empty_input(self):
        assert schema.top_n_comments([]) == []


class TestPickVideoVariant:
    def test_highest_bitrate_mp4_wins(self):
        variants = [
            {"content_type": "video/mp4", "bit_rate": 632000, "url": "u-low"},
            {"content_type": "application/x-mpegURL", "url": "u-m3u8"},
            {"content_type": "video/mp4", "bit_rate": 2176000, "url": "u-high"},
        ]
        assert pick_video_variant(variants) == "u-high"

    def test_m3u8_only_returns_none(self):
        assert pick_video_variant([{"content_type": "application/x-mpegURL", "url": "u"}]) is None

    def test_empty_and_none(self):
        assert pick_video_variant([]) is None
        assert pick_video_variant(None) is None

    def test_missing_bit_rate_tolerated(self):
        assert pick_video_variant([{"content_type": "video/mp4", "url": "u"}]) == "u"


def _fake_ig_post(**overrides):
    defaults = dict(
        mediaid=123, shortcode="Cxyz", caption="hello", likes=10,
        video_view_count=99, comments=4, is_video=True,
        date_utc=None, video_url="https://cdn/vid.mp4", url="https://cdn/pic.jpg",
        get_comments=lambda: iter([SimpleNamespace(text="top", likes_count=7)]),
    )
    defaults.update(overrides)
    ns = SimpleNamespace(**{k: v for k, v in defaults.items() if k != "get_comments"})
    ns.get_comments = defaults["get_comments"]
    return ns


class TestPostToResearch:
    def test_maps_fields(self):
        rp = _post_to_research(_fake_ig_post(), "chan", media_dir=None, max_comments=10)
        assert rp.platform == "instagram"
        assert rp.channel == "chan"
        assert rp.post_id == "123"
        assert rp.url == "https://www.instagram.com/p/Cxyz/"
        assert rp.likes == 10 and rp.views == 99 and rp.comments_count == 4
        assert rp.media_type == "video"
        assert rp.top_comments == [{"username": "", "text": "top", "likes": 7}]
        assert rp.fetch_status == "ok" and rp.error == ""

    def test_none_caption_becomes_empty_string(self):
        rp = _post_to_research(_fake_ig_post(caption=None), "chan", media_dir=None, max_comments=0)
        assert rp.caption == ""

    def test_comment_failure_is_partial_not_fatal(self):
        post = _fake_ig_post()
        post.get_comments = lambda: (_ for _ in ()).throw(RuntimeError("403 login_required"))
        rp = _post_to_research(post, "chan", media_dir=None, max_comments=10)
        assert rp.fetch_status == "partial"
        assert rp.top_comments == []
        assert "403" in rp.error
        assert rp.likes == 10  # metadata kept

    def test_max_comments_zero_skips_fetch(self):
        post = _fake_ig_post()
        post.get_comments = lambda: (_ for _ in ()).throw(AssertionError("must not be called"))
        rp = _post_to_research(post, "chan", media_dir=None, max_comments=0)
        assert rp.fetch_status == "ok"


class TestWriteOutputs:
    def _posts(self):
        return [schema.ResearchPost(
            platform="instagram", channel="chan", post_id="1", shortcode="abc",
            caption="cap", likes=5, top_comments=[{"username": "u", "text": "t", "likes": 3}],
            extra={"k": 1},
        )]

    def test_csv_header_and_json_encoding(self, tmp_path):
        schema.write_outputs(tmp_path, self._posts(), {"run": "meta"})
        with (tmp_path / "posts.csv").open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert list(rows[0].keys()) == list(schema.CSV_COLUMNS)
        assert json.loads(rows[0]["top_comments"]) == [{"username": "u", "text": "t", "likes": 3}]
        assert json.loads(rows[0]["extra"]) == {"k": 1}

    def test_json_roundtrips_and_run_meta_written(self, tmp_path):
        schema.write_outputs(tmp_path, self._posts(), {"posts": 1})
        raw = json.loads((tmp_path / "posts.json").read_text(encoding="utf-8"))
        assert [schema.ResearchPost(**r) for r in raw] == self._posts()
        assert json.loads((tmp_path / "run.json").read_text(encoding="utf-8")) == {"posts": 1}

    def test_load_and_merge_same_day_rerun(self, tmp_path):
        first = self._posts()
        schema.write_outputs(tmp_path, first, {})
        rerun = [schema.ResearchPost(platform="instagram", channel="chan",
                                     post_id="1", caption="updated"),
                 schema.ResearchPost(platform="instagram", channel="chan", post_id="2")]
        merged = schema.merge_posts(schema.load_existing_posts(tmp_path), rerun)
        assert [p.post_id for p in merged] == ["1", "2"]
        assert merged[0].caption == "updated"  # re-pulled post wins

    def test_load_existing_posts_missing_dir_is_empty(self, tmp_path):
        assert schema.load_existing_posts(tmp_path / "nope") == []


class TestResolveOutDir:
    def test_default(self):
        assert resolve_out_dir(env={}) == DEFAULT_OUT_DIR

    def test_env_override(self):
        assert resolve_out_dir(env={"RESEARCH_OUT_DIR": "/tmp/elsewhere"}) == Path("/tmp/elsewhere")
