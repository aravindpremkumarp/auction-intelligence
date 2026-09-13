"""Download a document to disk without ever leaving a partial file behind.

Same rules as ``scrapers/phase2_scrape_details.py::download_file``, which
learned them the hard way (two notices reached R2 as headless JPEGs): the
body lands on a per-attempt ``.part`` file, is checked against
``Content-Length`` when the server sends one, and is renamed into place only
once complete. A failed attempt leaves nothing a later run could mistake for
a download. Minus the Selenium cookie jar — these portals need none.

Also: pull the members out of a zip bundle under names the adapter chooses,
with the same never-partial guarantee.
"""
from __future__ import annotations

import os
import threading
import zipfile
from pathlib import Path
from typing import Callable

from sources import http

CHUNK = 8192


def filename_from_url(url: str) -> str:
    """``https://cdn/…/379330.pdf?x=1`` → ``379330.pdf``; an extensionless
    name gets ``.pdf``, as the phase2 scraper always did."""
    name = url.split("?")[0].rstrip("/").split("/")[-1] or "download"
    _, ext = os.path.splitext(name)
    if not ext or len(ext) < 2 or len(ext) > 5:
        name += ".pdf"
    return name


def download(
    url: str,
    dest_dir: str | Path,
    *,
    filename: str | None = None,
    referer: str | None = None,
    session=None,
    timeout: int = 60,
) -> Path | None:
    """Fetch ``url`` to ``dest_dir/filename``. Returns the path, or ``None``.

    Idempotent: a file already at the destination is returned without a
    request. Never raises on transport errors — a dead link is a finding for
    the caller to count, not a crash.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / (filename or filename_from_url(url))
    if final.exists():
        return final

    part = dest_dir / f"{final.name}.{os.getpid()}.{threading.get_ident()}.part"
    session = session or http.get_session()
    headers = {"Referer": referer} if referer else None
    try:
        r = session.get(url, stream=True, timeout=timeout, headers=headers)
        if r.status_code != 200:
            return None
        expected = r.headers.get("Content-Length")
        expected = int(expected) if expected and str(expected).isdigit() else None

        written = 0
        with open(part, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK):
                f.write(chunk)
                written += len(chunk)

        if written == 0:
            return None
        if expected is not None and written != expected:
            return None

        os.replace(part, final)  # same directory → atomic
        return final
    except Exception:  # noqa: BLE001 — transport failure is a result, not a crash
        return None
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass


def extract_zip_members(
    zip_path: str | Path,
    dest_dir: str | Path,
    rename: Callable[[str], str],
) -> list[tuple[str, Path]]:
    """Write each file in ``zip_path`` to ``dest_dir / rename(member_name)``.

    Returns ``[(member_name, path)]`` for every member written or already
    present. Directories are skipped; a member whose target already exists is
    not rewritten.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[tuple[str, Path]] = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            target = dest_dir / rename(info.filename)
            if target.exists():
                out.append((info.filename, target))
                continue
            part = dest_dir / f"{target.name}.{os.getpid()}.{threading.get_ident()}.part"
            try:
                with z.open(info) as src, open(part, "wb") as dst:
                    while True:
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        dst.write(chunk)
                os.replace(part, target)
                out.append((info.filename, target))
            finally:
                if part.exists():
                    try:
                        part.unlink()
                    except OSError:
                        pass
    return out
