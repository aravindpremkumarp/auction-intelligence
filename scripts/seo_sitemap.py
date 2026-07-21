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

<lastmod> is derived per URL so Google gets a real freshness signal (and only
recrawls pages that actually changed): the git commit date of the page's
source file, or today's build date for a page changed in the current,
not-yet-committed regeneration run. Git dates survive CI checkouts (unlike
filesystem mtimes, which git resets to "now" on clone). If git is unavailable
the build date is used for every page — still a valid lastmod, never a crash.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

SITE_BASE = "https://www.auctionscope.in"

# Hand-maintained static routes (not generated): loc, source file (relative to
# web_dir), changefreq, priority. Each is served from a standalone HTML file
# (see tests/api/test_static_routes.py), so lastmod tracks that file.
STATIC_ROUTES = [
    ("/", "index.html", "daily", "1.0"),
    ("/privacy-policy", "privacy-policy.html", "yearly", "0.3"),
    ("/terms-of-service", "terms-of-service.html", "yearly", "0.3"),
    ("/disclaimer", "disclaimer.html", "yearly", "0.3"),
]


def _url_for(index_html: Path, web_dir: Path) -> str:
    """web/bank-auctions/chennai/plots/index.html -> /bank-auctions/chennai/plots"""
    rel = index_html.parent.relative_to(web_dir).as_posix()
    return f"/{rel}"


def _build_date() -> str:
    """Today (UTC), YYYY-MM-DD — the lastmod for pages changed this run."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a git command, returning stripped stdout, or None on any failure
    (git missing, not a repo, timeout). Callers fall back to the build date."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=30, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return out.stdout.strip()


def _repo_root(web_dir: Path) -> Path | None:
    top = _git(["rev-parse", "--show-toplevel"], web_dir)
    return Path(top).resolve() if top else None


def _dirty_paths(repo_root: Path) -> set[str]:
    """Repo-relative POSIX paths with uncommitted changes (modified or
    untracked). Such a page changed 'now' — its lastmod is the build date,
    because the change is committed alongside this very sitemap and so has no
    git date yet. `git status` compares content, not mtime, so an identical
    rewrite (same inventory) stays clean and keeps its real historical date."""
    out = _git(["status", "--porcelain"], repo_root)
    if not out:
        return set()
    dirty: set[str] = set()
    for line in out.splitlines():
        # "XY <path>" — split off the status code (X/Y are single chars, either
        # may be a space) rather than slicing a fixed column, which is fragile
        # once leading whitespace is stripped. For a rename ("old -> new") keep
        # the new path.
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        path = parts[1].split(" -> ", 1)[-1].strip()
        if path:
            dirty.add(path)
    return dirty


def _lastmod(source: Path, repo_root: Path | None, dirty: set[str],
             build_date: str) -> str:
    """git commit date (YYYY-MM-DD) of `source`, or build_date if it changed
    this run / has no git history / git is unavailable."""
    if repo_root is None:
        return build_date
    try:
        rel = source.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return build_date
    if rel in dirty:
        return build_date
    return _git(["log", "-1", "--format=%cs", "--", rel], repo_root) or build_date


def collect_urls(web_dir: Path) -> list[tuple[str, str, str, str]]:
    """(loc_path, changefreq, priority, lastmod) for every page, static + generated."""
    web_dir = Path(web_dir).resolve()
    build_date = _build_date()
    repo_root = _repo_root(web_dir)
    dirty = _dirty_paths(repo_root) if repo_root else set()

    # (loc_path, source_file, changefreq, priority) — source_file feeds lastmod.
    entries: list[tuple[str, Path, str, str]] = [
        (loc, web_dir / src, freq, prio) for loc, src, freq, prio in STATIC_ROUTES
    ]

    # Programmatic landing pages — higher priority than an individual auction:
    # they're the ranking hubs and don't expire.
    for idx in sorted((web_dir / "bank-auctions").glob("**/index.html")):
        depth = len(idx.parent.relative_to(web_dir / "bank-auctions").parts)
        # /bank-auctions (hub) = depth 0, /bank-auctions/<city> = 1, city/type = 2
        priority = "0.9" if depth <= 1 else "0.8"
        entries.append((_url_for(idx, web_dir), idx, "daily", priority))

    # Individual property pages.
    for idx in sorted((web_dir / "property").glob("*/index.html")):
        entries.append((_url_for(idx, web_dir), idx, "weekly", "0.7"))

    # Educational guides (scripts/build_guides.py) — evergreen, don't expire.
    # /guides (hub) = depth 0, /guides/<slug> = 1.
    for idx in sorted((web_dir / "guides").glob("**/index.html")):
        depth = len(idx.parent.relative_to(web_dir / "guides").parts)
        entries.append((_url_for(idx, web_dir), idx, "monthly", "0.8" if depth == 0 else "0.7"))

    # Comparison / alternative pages (scripts/build_compare.py) — evergreen.
    for idx in sorted((web_dir / "compare").glob("**/index.html")):
        depth = len(idx.parent.relative_to(web_dir / "compare").parts)
        entries.append((_url_for(idx, web_dir), idx, "monthly", "0.8" if depth == 0 else "0.7"))

    # Free tools / calculators (scripts/build_tools.py) — evergreen, link magnets.
    for idx in sorted((web_dir / "tools").glob("**/index.html")):
        entries.append((_url_for(idx, web_dir), idx, "monthly", "0.8"))

    return [
        (loc, freq, prio, _lastmod(src, repo_root, dirty, build_date))
        for loc, src, freq, prio in entries
    ]


def build_sitemap_xml(web_dir: Path) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, freq, prio, lastmod in collect_urls(web_dir):
        lines += ["  <url>", f"    <loc>{SITE_BASE}{loc}</loc>",
                  f"    <lastmod>{lastmod}</lastmod>",
                  f"    <changefreq>{freq}</changefreq>",
                  f"    <priority>{prio}</priority>", "  </url>"]
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def write_sitemap(web_dir: Path) -> int:
    """Regenerate web/sitemap.xml from the filesystem. Returns the URL count."""
    urls = collect_urls(web_dir)
    (web_dir / "sitemap.xml").write_text(build_sitemap_xml(web_dir), encoding="utf-8")
    return len(urls)


if __name__ == "__main__":
    # Rebuild the sitemap in place from whatever pages currently exist on disk.
    n = write_sitemap(Path(__file__).resolve().parent.parent / "web")
    print(f"sitemap.xml rebuilt — {n} URLs")
