"""
scripts/seo_sitemap.py
----------------------
Single source of truth for web/sitemap.xml.

Both prerendered generators (scripts/prerender_properties.py for
/property/<id> pages, scripts/build_landing_pages.py for the
/bank-auctions/** programmatic landing pages) call write_sitemap() so the
sitemap always reflects EVERY generated page — writing it from either script
alone would drop the other's URLs the next time it ran.

The sitemap is rebuilt by scanning the filesystem, not from an in-memory
list, so it's correct no matter which generator ran last or which subset of
cities was regenerated.
"""
from __future__ import annotations

from pathlib import Path

SITE_BASE = "https://www.auctionscope.in"

# Hand-maintained static routes (not generated). loc, changefreq, priority.
STATIC_ROUTES = [
    ("/", "daily", "1.0"),
    ("/privacy-policy", "yearly", "0.3"),
    ("/terms-of-service", "yearly", "0.3"),
    ("/disclaimer", "yearly", "0.3"),
]


def _url_for(index_html: Path, web_dir: Path) -> str:
    """web/bank-auctions/chennai/plots/index.html -> /bank-auctions/chennai/plots"""
    rel = index_html.parent.relative_to(web_dir).as_posix()
    return f"/{rel}"


def collect_urls(web_dir: Path) -> list[tuple[str, str, str]]:
    """(loc_path, changefreq, priority) for every page, static + generated."""
    urls: list[tuple[str, str, str]] = list(STATIC_ROUTES)

    # Programmatic landing pages — higher priority than an individual auction:
    # they're the ranking hubs and don't expire.
    for idx in sorted((web_dir / "bank-auctions").glob("**/index.html")):
        depth = len(idx.parent.relative_to(web_dir / "bank-auctions").parts)
        # /bank-auctions (hub) = depth 0, /bank-auctions/<city> = 1, city/type = 2
        priority = "0.9" if depth <= 1 else "0.8"
        urls.append((_url_for(idx, web_dir), "daily", priority))

    # Individual property pages.
    for idx in sorted((web_dir / "property").glob("*/index.html")):
        urls.append((_url_for(idx, web_dir), "weekly", "0.7"))

    return urls


def build_sitemap_xml(web_dir: Path) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, freq, prio in collect_urls(web_dir):
        lines += ["  <url>", f"    <loc>{SITE_BASE}{loc}</loc>",
                  f"    <changefreq>{freq}</changefreq>",
                  f"    <priority>{prio}</priority>", "  </url>"]
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def write_sitemap(web_dir: Path) -> int:
    """Regenerate web/sitemap.xml from the filesystem. Returns the URL count."""
    urls = collect_urls(web_dir)
    (web_dir / "sitemap.xml").write_text(build_sitemap_xml(web_dir), encoding="utf-8")
    return len(urls)
