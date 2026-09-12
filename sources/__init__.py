"""Source adapters — one per auction portal, one contract.

Every portal we read has its own record shape, id space, date format and
document quirks. All of that stays inside the adapter for that portal; what
comes out is a :class:`sources.base.Listing`, the same shape from every
source, which is what the loader, the matcher and the spine are built on.

Design: docs/superpowers/specs/2026-09-12-source-adapters-design.md.
Portal contracts and traps: docs/source-recon-2026-09.md.

Nothing in this package imports ``scrapers/utils.py`` — that module pulls in
``undetected_chromedriver`` at import time, and these adapters must run
without a browser.
"""
from __future__ import annotations

from sources.base import SOURCE_RANK, DocRef, Listing, MediaRef, SourceAdapter

__all__ = ["ADAPTERS", "DocRef", "Listing", "MediaRef", "SOURCE_RANK", "SourceAdapter", "get_adapter"]

#: name → import path of the adapter class. Kept as strings so importing the
#: package does not import every adapter (and their optional dependencies).
ADAPTERS: dict[str, str] = {
    "eauctionsindia": "sources.eauctionsindia:EauctionsIndiaAdapter",
}


def get_adapter(name: str, **kwargs) -> SourceAdapter:
    """Instantiate the adapter registered under ``name``."""
    try:
        module_path, class_name = ADAPTERS[name].split(":")
    except KeyError:
        raise KeyError(f"unknown source {name!r}; known: {sorted(ADAPTERS)}") from None
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)(**kwargs)
