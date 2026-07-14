"""
marketing/research/media.py
---------------------------
Shared media download for the channel-research fetchers.
"""
from __future__ import annotations

from pathlib import Path

import httpx


def download_media(url: str, dest: Path, timeout: float = 60.0) -> None:
    """Stream ``url`` to ``dest`` (chunked — reel videos run to tens of MB)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
