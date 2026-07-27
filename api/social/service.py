"""
api/social/service.py
---------------------
Filesystem side of the staged-content review surface: locate batches under
`marketing/outputs/`, read `drafts.json`, and resolve/bundle the assets a
reviewer downloads.

No FastAPI imports here on purpose — every path rule (and therefore every
traversal guard) is unit-testable without spinning up the app, and the router
stays a thin translation layer from `ValueError` to HTTP 404.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Batch directories are named by the Poster as `date.today().isoformat()`.
# Anything else is not a batch, and — because the name goes into a filesystem
# path — is rejected before it is ever joined.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Only the file types a staged batch actually contains. Keeping this an
# allowlist (rather than blocking `.py`) means a new file type has to be added
# deliberately instead of leaking by default.
_ASSET_SUFFIXES = {".png", ".json", ".md"}


def outputs_dir() -> Path:
    """Root of the staged batches.

    Honours `POSTER_OUT_DIR` — the same env var `marketing_agents/poster.py`
    writes to — so a test (or a one-off local run) can point both halves at the
    same throwaway directory. A relative value resolves against the repo root,
    not the process CWD, so the API behaves the same from any working directory.
    """
    raw = os.environ.get("POSTER_OUT_DIR", "").strip()
    if not raw:
        return _REPO_ROOT / "marketing" / "outputs"
    p = Path(raw)
    return p if p.is_absolute() else _REPO_ROOT / p


def validate_date(date: str) -> str:
    """Return `date` if it is a well-formed batch name, else raise ValueError.

    Called before any path is built from a caller-supplied date.
    """
    if not _DATE_RE.match(date or ""):
        raise ValueError(f"invalid batch date: {date!r}")
    return date


def _kind_for(template: str, stem: str, from_reels: bool) -> str:
    if from_reels:
        return "reel"
    blob = f"{template} {stem}".lower()
    return "carousel" if "carousel" in blob else "card"


def _rendered_pngs(batch_dir: Path, stem: str) -> list[str]:
    """Rendered PNGs for one island, batch-relative and sorted.

    `render_social.py --render-staged` writes `rendered/card-<stem>.png` for a
    single-stage template, and `rendered/card-<stem>_01.png`, `_02.png`, … for a
    multi-stage one (the carousel renders one PNG per slide). Match both so the
    UI shows every slide without special-casing carousels.
    """
    rendered = batch_dir / "rendered"
    if not rendered.is_dir():
        return []
    names = sorted(
        p.name for p in rendered.glob(f"card-{stem}.png")
    ) + sorted(
        p.name for p in rendered.glob(f"card-{stem}_*.png")
    )
    return [f"rendered/{n}" for n in names]


def _artifact(row: dict, batch_dir: Path, from_reels: bool) -> dict:
    """Normalise one `cards[]`/`reels[]` manifest row into an artifact dict."""
    island_path = str(row.get("data") or "")
    stem = Path(island_path).stem
    template = str(row.get("template") or "")
    pngs = _rendered_pngs(batch_dir, stem) if not from_reels else []
    return {
        "kind": _kind_for(template, stem, from_reels),
        "stem": stem,
        "template": template,
        "auction_id": row.get("auction_id"),
        "headline": row.get("headline"),
        "island_path": island_path,
        "png_paths": pngs,
        "png_available": bool(pngs),
        "hook": row.get("hook"),
        "draft_index": row.get("draft_index"),
    }


def _read_drafts_json(batch_dir: Path) -> dict:
    return json.loads((batch_dir / "drafts.json").read_text(encoding="utf-8"))


def list_batches() -> list[dict]:
    """Every staged batch, newest first.

    A date directory with no `drafts.json` is not a batch and is skipped. One
    whose `drafts.json` is unreadable is returned with `error` set rather than
    dropped — a batch that fails to parse is exactly the thing a reviewer needs
    to be told about, and one bad batch must not blank the whole list.
    """
    root = outputs_dir()
    if not root.is_dir():
        return []
    out: list[dict] = []
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir() or not _DATE_RE.match(child.name):
            continue
        if not (child / "drafts.json").is_file():
            continue
        summary: dict = {
            "date": child.name,
            "generated_at": None,
            "editor_notes": None,
            "n_drafts": 0,
            "n_cards": 0,
            "n_reels": 0,
            "n_carousels": 0,
            "error": None,
        }
        try:
            data = _read_drafts_json(child)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            summary["error"] = f"{type(exc).__name__}: {exc}"
            out.append(summary)
            continue
        cards = data.get("cards") or []
        reels = data.get("reels") or []
        n_carousel = sum(
            1 for c in cards
            if _kind_for(str(c.get("template") or ""),
                         Path(str(c.get("data") or "")).stem, False) == "carousel"
        )
        summary.update({
            "generated_at": (data.get("stats") or {}).get("generated_at"),
            "editor_notes": data.get("editor_notes"),
            "n_drafts": len(data.get("drafts") or []),
            "n_cards": len(cards) - n_carousel,
            "n_reels": len(reels),
            "n_carousels": n_carousel,
        })
        out.append(summary)
    return out


def load_batch(date: str) -> dict:
    """One batch: drafts joined to the artifacts staged for them.

    Raises ValueError for a malformed date, a batch that does not exist, or a
    `drafts.json` that cannot be parsed.
    """
    validate_date(date)
    batch_dir = outputs_dir() / date
    if not (batch_dir / "drafts.json").is_file():
        raise ValueError(f"no staged batch for {date}")
    try:
        data = _read_drafts_json(batch_dir)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"unreadable batch {date}: {exc}") from exc

    artifacts = [_artifact(r, batch_dir, False) for r in (data.get("cards") or [])]
    artifacts += [_artifact(r, batch_dir, True) for r in (data.get("reels") or [])]

    drafts_raw = data.get("drafts") or []
    drafts: list[dict] = []
    # Manifest `draft_index` is 1-based into `drafts` (index 0 is reserved for
    # the batch-level artifacts: the stats reel and the city carousel).
    for i, d in enumerate(drafts_raw, 1):
        mine = [a for a in artifacts if a.get("draft_index") == i]
        drafts.append({
            "draft_index": i,
            "auction_id": d.get("auction_id"),
            "angle": d.get("angle"),
            "hook_mechanism": d.get("hook_mechanism"),
            "post": d.get("post") or "",
            "pinned_comment": d.get("pinned_comment"),
            "hashtags": d.get("hashtags") or [],
            "alt_text": d.get("alt_text"),
            "video_title": d.get("video_title"),
            "location_tag": d.get("location_tag"),
            "engagement_question": d.get("engagement_question"),
            "save_line": d.get("save_line"),
            "reel_hook": d.get("reel_hook"),
            "reel_context_lines": d.get("reel_context_lines") or [],
            "hook_alternatives": d.get("hook_alternatives") or [],
            "source": d.get("source") or {},
            "artifacts": mine,
        })

    claimed = {id(a) for d in drafts for a in d["artifacts"]}
    orphans = [a for a in artifacts if id(a) not in claimed]

    return {
        "date": date,
        "generated_at": (data.get("stats") or {}).get("generated_at"),
        "editor_notes": data.get("editor_notes"),
        "stats": data.get("stats") or {},
        "drafts": drafts,
        "orphan_artifacts": orphans,
        "rejected": data.get("rejected") or [],
    }


def resolve_asset(date: str, relpath: str) -> Path:
    """Resolve a batch-relative asset path to a real file, or raise ValueError.

    The security boundary for `GET /social/asset/...`. The join is resolved
    *before* the containment check, so `..` segments, symlinks and absolute
    paths are all normalised away first and then caught by the single
    `is_relative_to` test.
    """
    validate_date(date)
    if not relpath or relpath.startswith("/") or "\x00" in relpath:
        raise ValueError(f"invalid asset path: {relpath!r}")
    batch_dir = (outputs_dir() / date).resolve()
    full = (batch_dir / relpath).resolve()
    if not full.is_relative_to(batch_dir):
        raise ValueError(f"asset path escapes the batch: {relpath!r}")
    if full.suffix.lower() not in _ASSET_SUFFIXES:
        raise ValueError(f"asset type not served: {relpath!r}")
    if not full.is_file():
        raise ValueError(f"no such asset: {relpath!r}")
    return full


def _caption_text(draft: dict) -> str:
    """The publish-ready caption: post, hashtags, pinned comment."""
    parts = [draft.get("post") or ""]
    tags = draft.get("hashtags") or []
    if tags:
        parts.append(" ".join(f"#{t.lstrip('#')}" for t in tags))
    if draft.get("pinned_comment"):
        parts.append(f"pinned comment:\n{draft['pinned_comment']}")
    if draft.get("alt_text"):
        parts.append(f"alt text:\n{draft['alt_text']}")
    return "\n\n".join(p for p in parts if p) + "\n"


def bundle_draft(date: str, draft_index: int) -> tuple[str, bytes]:
    """Zip one draft's caption + rendered images + islands for publishing.

    Returns (filename, zip bytes). Raises ValueError if the batch or the draft
    index does not exist.
    """
    batch = load_batch(date)
    match = [d for d in batch["drafts"] if d["draft_index"] == draft_index]
    if not match:
        raise ValueError(f"no draft {draft_index} in batch {date}")
    draft = match[0]
    batch_dir = outputs_dir() / date

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("caption.txt", _caption_text(draft))
        for art in draft["artifacts"]:
            for rel in [art["island_path"], *art["png_paths"]]:
                src = batch_dir / rel
                if src.is_file():
                    zf.write(src, arcname=rel)
    aid = draft.get("auction_id") or "draft"
    return f"{date}-draft-{draft_index:02d}-{aid}.zip", buf.getvalue()
