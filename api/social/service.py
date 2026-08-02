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


def _rendered_pngs(batch_dir: Path, stem: str, media_keys: dict[str, str]) -> list[str]:
    """Rendered PNGs for one island, batch-relative and sorted.

    `render_social.py --render-staged` writes `rendered/card-<stem>.png` for a
    single-stage template, and `rendered/card-<stem>_01.png`, `_02.png`, … for a
    multi-stage one (the carousel renders one PNG per slide). Match both so the
    UI shows every slide without special-casing carousels.

    Since PNGs moved to R2 they are usually *not* on disk, so `media_keys` (the
    manifest's {batch-relative path → R2 key} map) is the primary source and the
    filesystem is the fallback for batches staged while renders were committed.
    """
    prefix = f"rendered/card-{stem}"
    from_r2 = sorted(
        p for p in media_keys
        if p.lower().endswith(".png")
        and (p == f"{prefix}.png" or p.startswith(f"{prefix}_"))
    )
    if from_r2:
        return from_r2
    rendered = batch_dir / "rendered"
    if not rendered.is_dir():
        return []
    names = sorted(
        p.name for p in rendered.glob(f"card-{stem}.png")
    ) + sorted(
        p.name for p in rendered.glob(f"card-{stem}_*.png")
    )
    return [f"rendered/{n}" for n in names]


def _artifact(row: dict, batch_dir: Path, from_reels: bool,
              media_keys: dict[str, str], media_bytes: dict[str, int]) -> dict:
    """Normalise one `cards[]`/`reels[]` manifest row into an artifact dict."""
    island_path = str(row.get("data") or "")
    stem = Path(island_path).stem
    template = str(row.get("template") or "")
    pngs = [] if from_reels else _rendered_pngs(batch_dir, stem, media_keys)
    # Reels are the one artifact with no on-disk fallback: MP4s were never
    # committed, so a reel is playable only if the uploader recorded its key.
    video_path = f"reels/{stem}.mp4" if from_reels else None
    video_key = media_keys.get(video_path) if video_path else None
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
        # The R2 key is an internal pointer — it tells the router which private
        # object to stream and is never serialised to the client.
        "video_path": video_path,
        "video_key": video_key,
        "video_bytes": media_bytes.get(video_path) if video_path else None,
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

    media_keys: dict[str, str] = data.get("media_keys") or {}
    media_bytes: dict[str, int] = data.get("media_bytes") or {}
    artifacts = [_artifact(r, batch_dir, False, media_keys, media_bytes)
                 for r in (data.get("cards") or [])]
    artifacts += [_artifact(r, batch_dir, True, media_keys, media_bytes)
                  for r in (data.get("reels") or [])]

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
        "media_keys": media_keys,
    }


def media_key_for(date: str, relpath: str) -> str | None:
    """R2 key for one batch-relative path, or None if it isn't in R2.

    The path is validated against the manifest's own `media_keys` map rather
    than derived from the caller's input, so an attacker-chosen path can never
    become an attacker-chosen object key.
    """
    validate_date(date)
    batch_dir = outputs_dir() / date
    if not (batch_dir / "drafts.json").is_file():
        raise ValueError(f"no staged batch for {date}")
    try:
        data = _read_drafts_json(batch_dir)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"unreadable batch {date}: {exc}") from exc
    return (data.get("media_keys") or {}).get(relpath)


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


def bundle_draft(
    date: str,
    draft_index: int,
    extra_files: dict[str, bytes] | None = None,
) -> tuple[str, bytes]:
    """Zip one draft's caption + rendered images + islands for publishing.

    `extra_files` is {arcname: bytes} for content that isn't on disk — the
    router passes reel MP4s pulled from private R2, so the bundle contains
    everything needed to publish rather than everything that happens to be
    committed. Keeping the fetch in the caller leaves this module free of R2.

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
        for arcname, blob in (extra_files or {}).items():
            zf.writestr(arcname, blob)
    aid = draft.get("auction_id") or "draft"
    return f"{date}-draft-{draft_index:02d}-{aid}.zip", buf.getvalue()


def draft_media_keys(date: str, draft_index: int) -> dict[str, str]:
    """{arcname: r2 key} for every R2-hosted asset of one draft.

    Covers card PNGs, carousel slides and reel MP4s alike — the bundle needs all
    of them now that rendered media isn't committed. Assets still on disk are
    absent here and get zipped straight from the filesystem instead.
    """
    batch = load_batch(date)
    match = [d for d in batch["drafts"] if d["draft_index"] == draft_index]
    if not match:
        raise ValueError(f"no draft {draft_index} in batch {date}")
    keys = batch["media_keys"]
    out: dict[str, str] = {}
    for a in match[0]["artifacts"]:
        for rel in [*a["png_paths"], a.get("video_path")]:
            if rel and rel in keys:
                out[rel] = keys[rel]
    return out
