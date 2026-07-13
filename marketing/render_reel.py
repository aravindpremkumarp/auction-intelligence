"""Render the 9:16 reel templates in marketing/templates/ to MP4s.

Video companion to render_social.py (which owns the static PNGs and refuses
reels). Reels are HyperFrames compositions: this script stages a scratch
HyperFrames project per render (the CLI wants a project dir with an
index.html, not a bare file), substitutes the ``<script id="data">`` island
with per-auction data from the Poster, runs ``hyperframes lint`` and then
``render``, and collects the MP4. The template's ``*.motion.json`` sidecar is
staged alongside as ``index.motion.json`` so the retention assertions
(hook appearsBy 0.3s, count fired, end card on time) keep guarding every
render.

Environment (all baked in, learned the hard way):
  * hyperframes is PINNED to 0.7.52 — bare ``npx hyperframes`` can resolve to
    an unpublished version (npm registry lag) and die silently. Prefer the
    local install in videos/node_modules (``npm ci --ignore-scripts --prefix
    videos`` — --ignore-scripts because onnxruntime's postinstall download
    fails behind proxies; it's only needed for talking-head features).
  * The render browser comes from HYPERFRAMES_BROWSER_PATH; we resolve
    Chromium from /opt/pw-browsers (Claude sandboxes) or ~/.cache/ms-playwright
    (GitHub runners after ``npx playwright install chromium``).
  * ffprobe must be on PATH (ships in the ffprobe-static npm package).

Usage:
    python marketing/render_reel.py --data marketing/outputs/<d>/reels/01-X.json
    python marketing/render_reel.py --manifest marketing/outputs/<d>/drafts.json \
        --out .reel_renders
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent
TEMPLATES = ROOT / "templates"

HYPERFRAMES_VERSION = "0.7.52"
DEFAULT_TEMPLATE = "deal-reel-1080x1920"
REEL_TEMPLATES = [
    "deal-reel-1080x1920",
    "stats-reel-1080x1920",
    "evaluate-reel-1080x1920",
]

DATA_ISLAND = re.compile(
    r'(<script id="data" type="application/json">).*?(</script>)', re.S
)


def chromium_path() -> str | None:
    """Chromium for the render browser: Claude sandboxes ship it under
    /opt/pw-browsers; GitHub runners get it from `npx playwright install`."""
    cands = (
        glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")
        + glob.glob("/opt/pw-browsers/chromium/chrome-linux/chrome")
        + glob.glob(str(pathlib.Path.home()
                        / ".cache/ms-playwright/chromium-*/chrome-linux/chrome"))
    )
    return cands[0] if cands else None


def hyperframes_cmd() -> list[str]:
    """The pinned hyperframes CLI: local install first, pinned npx fallback."""
    local = REPO / "videos" / "node_modules" / ".bin" / "hyperframes"
    if local.exists():
        return ["node", str(local)]
    return ["npx", "-y", f"hyperframes@{HYPERFRAMES_VERSION}"]


def build_env() -> dict:
    env = os.environ.copy()
    if "HYPERFRAMES_BROWSER_PATH" not in env:
        chrome = chromium_path()
        if chrome:
            env["HYPERFRAMES_BROWSER_PATH"] = chrome
    # ffprobe-static's binary dir (local videos/ install) onto PATH
    probe = glob.glob(str(REPO / "videos/node_modules/ffprobe-static/bin/*/*/ffprobe"))
    if probe and not shutil.which("ffprobe"):
        env["PATH"] = os.path.dirname(probe[0]) + os.pathsep + env.get("PATH", "")
    return env


def stage_project(template: str, island_json: str | None, stage_dir: pathlib.Path) -> None:
    """Materialize a one-shot HyperFrames project: template as index.html
    (island substituted), lib/ beside it, motion sidecar renamed to match."""
    src = TEMPLATES / f"{template}.html"
    if not src.exists():
        sys.exit(f"unknown reel template: {template} ({src} missing)")
    html = src.read_text(encoding="utf-8")
    if island_json is not None:
        html, n = DATA_ISLAND.subn(
            lambda m: m.group(1) + "\n" + island_json.strip() + "\n" + m.group(2),
            html,
        )
        if n != 1:
            sys.exit(f"{template}: expected exactly one #data island, found {n}")
    stage_dir.mkdir(parents=True, exist_ok=True)
    (stage_dir / "index.html").write_text(html, encoding="utf-8")
    shutil.copytree(TEMPLATES / "lib", stage_dir / "lib", dirs_exist_ok=True)
    sidecar = TEMPLATES / f"{template}.motion.json"
    if sidecar.exists():
        shutil.copy(sidecar, stage_dir / "index.motion.json")


def run_cli(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        hyperframes_cmd() + args, env=env, capture_output=True, text=True,
        timeout=900,
    )


def render_one(template: str, data_file: pathlib.Path | None,
               out_path: pathlib.Path, quality: str, skip_checks: bool) -> bool:
    island = data_file.read_text(encoding="utf-8") if data_file else None
    env = build_env()
    with tempfile.TemporaryDirectory(prefix="reel-") as td:
        stage = pathlib.Path(td) / "project"
        stage_project(template, island, stage)
        if not skip_checks:
            lint = run_cli(["lint", str(stage)], env)
            if lint.returncode != 0:
                print(f"LINT FAILED for {out_path.name}:\n{lint.stdout[-2000:]}"
                      f"{lint.stderr[-500:]}", file=sys.stderr)
                return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        res = run_cli(["render", str(stage), "-q", quality,
                       "-o", str(out_path)], env)
        if res.returncode != 0 or not out_path.exists():
            print(f"RENDER FAILED for {out_path.name}:\n{res.stdout[-2000:]}"
                  f"{res.stderr[-500:]}", file=sys.stderr)
            return False
    size_kb = out_path.stat().st_size // 1024
    print(f"rendered {out_path} ({size_kb} KB)")
    return True


def manifest_jobs(manifest_path: pathlib.Path) -> list[tuple[str, pathlib.Path, str]]:
    """(template, island_path, output_stem) rows from a drafts.json manifest."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    jobs = []
    for row in data.get("reels") or []:
        island = base / row["data"]
        stem = pathlib.Path(row["data"]).stem  # e.g. 01-800979
        jobs.append((row["template"], island, stem))
    return jobs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", help="a single reel #data island JSON file")
    ap.add_argument("--manifest", help="a drafts.json with a 'reels' manifest")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE, choices=REEL_TEMPLATES,
                    help="template for --data mode (manifest rows carry their own)")
    ap.add_argument("--out", default="marketing/outputs/reels",
                    help="output directory for MP4s")
    ap.add_argument("--quality", default="high", choices=["draft", "standard", "high"])
    ap.add_argument("--skip-checks", action="store_true",
                    help="skip the lint gate (renders still run motion assertions)")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    if args.manifest:
        jobs = manifest_jobs(pathlib.Path(args.manifest))
        if not jobs:
            print("manifest has no reels — nothing to render")
            return
    elif args.data:
        data_path = pathlib.Path(args.data)
        jobs = [(args.template, data_path, data_path.stem)]
    else:
        # No data: render the template's own sample island (design check)
        jobs = [(args.template, None, args.template + "-sample")]

    failures = 0
    for template, island_path, stem in jobs:
        if island_path is not None and not island_path.exists():
            print(f"missing island: {island_path}", file=sys.stderr)
            failures += 1
            continue
        ok = render_one(template, island_path, out_dir / f"{stem}.mp4",
                        args.quality, args.skip_checks)
        failures += 0 if ok else 1
    if failures:
        sys.exit(f"{failures} reel(s) failed to render")


if __name__ == "__main__":
    main()
