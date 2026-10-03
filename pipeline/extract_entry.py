"""The one switch between the two readers.

``read_document`` is what every extraction writer calls — the cron loader,
the re-read script, the review page's re-run. Which reader runs is the
``EXTRACT_READER`` setting (pipeline/config):

* ``langextract`` — today's path (scripts/reset_langextract_and_extract
  .read_notice: routed model and passes, lot chunking when the reviewer
  count allows it, the retry ladder). The cron loader used to skip all of
  that and read every notice whole; now it gets the same read the scripts do.
* ``v2``         — pipeline/reader.read_notice.
* ``shadow``     — v1 is what gets written; v2 runs beside it and comes back
  in ``meta["shadow"]`` (entities, score, telemetry and keep_better's
  gains/losses against v1) for the store to keep under
  ``extraction_shadow_*``, so a cycle of real notices measures the new
  reader before it writes a single value.

Flipping the setting is the whole rollout and the whole rollback.
"""
from __future__ import annotations

import os
import time

from pipeline import config


def current_reader() -> str:
    """The reader in force: env at call time, then pipeline/config."""
    return (os.environ.get("EXTRACT_READER") or config.EXTRACT_READER or "langextract").strip().lower()


def _v1(d: dict, route: bool) -> tuple[list[dict], str | None]:
    from scripts.reset_langextract_and_extract import read_notice
    return read_notice(d, route)


def _v2(d: dict, route: bool, stability: str) -> tuple[list[dict], str, dict]:
    from pipeline.reader import read_notice
    from pipeline.reader.segment import describe  # noqa: F401 (describe already applied)
    model_id = None
    if not route:
        model_id = os.environ.get("LANGEXTRACT_MODEL_ID") or None
    r = read_notice(d["md"], expected_lot_count=d.get("expected_lot_count"),
                    roster=d.get("roster"), notice_type=d.get("notice_type"),
                    blocks=d.get("blocks"), model_id=model_id, stability=stability,
                    notice_date=d.get("notice_date"))
    meta = {"reader": "v2", "prompt_hash": r.prompt_hash, "schema_version": r.reader_version,
            "telemetry": r.telemetry, "timeline": r.timeline, "marks": r.marks,
            "dropped": r.dropped, "segmentation": r.segmentation,
            "findings": [f.__dict__ for f in r.findings], "reports": r.reports}
    return r.entities, r.model, meta


def read_document(d: dict, route: bool = True, *, reader: str | None = None,
                  stability: str | None = None) -> tuple[list[dict], str | None, dict]:
    """``(entities, model_id, meta)`` for one document dict (filename, md,
    notice_type, expected_lot_count, roster, blocks). ``meta["reader"]`` is
    the reader whose entities are returned; ``meta["shadow"]`` is set in
    shadow mode."""
    reader = (reader or current_reader())
    stability = stability or os.environ.get("EXTRACT_STABILITY", "verify")
    if reader == "v2":
        ents, model, meta = _v2(d, route, stability)
        return ents, model, meta
    if reader == "shadow":
        ents, model = _v1(d, route)
        meta: dict = {"reader": "langextract"}
        t0 = time.monotonic()
        try:
            v2_ents, v2_model, v2_meta = _v2(d, route, stability)
            from pipeline.keep_better import judge
            from pipeline.validators import validate_stored
            save, gains, losses = judge(ents, v2_ents, d["md"], d.get("expected_lot_count"))
            meta["shadow"] = {
                "entities": v2_ents, "model": v2_model,
                "score": validate_stored(v2_ents, source_text=d["md"])["score"],
                "telemetry": v2_meta.get("telemetry"), "segmentation": v2_meta.get("segmentation"),
                "dropped": len(v2_meta.get("dropped") or []),
                "judge": {"v2_better": bool(save), "gains": gains[:20], "losses": losses[:20]},
                "seconds": round(time.monotonic() - t0, 1),
            }
        except Exception as e:  # noqa: BLE001 - the shadow never fails the write
            meta["shadow"] = {"error": f"{type(e).__name__}: {str(e)[:300]}",
                              "seconds": round(time.monotonic() - t0, 1)}
        return ents, model, meta
    if reader != "langextract":
        raise ValueError(f"EXTRACT_READER={reader!r}: expected langextract, v2 or shadow")
    ents, model = _v1(d, route)
    return ents, model, {"reader": "langextract"}


__all__ = ["read_document", "current_reader"]
