"""Find and (optionally) delete OCR cache entries corrupted by the old
image+vision pipeline.

The vision LLM that previously read rendered PDF pages misread the small
Unicode fractions ½ and ¾ as ASCII `/` and `%`. The result was values like
`38/ft.` or `41% ft.` in `enrichment.total_area` / `enrichment.boundaries.*`
where the document actually said `38 ½ ft.` / `41 ¾ ft.`.

Run with no flags for a dry-run summary. Use ``--commit`` to delete the
corrupted cache files; the next ``python -m pipeline.ocr_extract`` will then
re-extract them via the new MinerU-markdown path.

Examples:
  python -m scripts.invalidate_corrupted_ocr_cache              # dry run
  python -m scripts.invalidate_corrupted_ocr_cache --commit     # delete
  python -m scripts.invalidate_corrupted_ocr_cache --show 10    # print samples
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from pipeline.config import CACHE_DIR
from pipeline.mineru import MINERU_MARKDOWN_DIR, cached_markdown_for_filename


# A token like "38/ft.", "41% ft.", "1200/sq.ft" — a digit run followed by
# `/` or `%` and then a measurement unit. Legitimate uses (survey numbers
# like "432/4", interest rates like "12%", "1/4 share") never have a unit
# immediately after the symbol, so this pattern is conservative.
UNIT_RX = re.compile(
    r"\d+\s*[/%]\s*"
    r"(?:ft|feet|sq\.?\s*ft|sft|sqft|cents?|acres?|grounds?|metres?|m\b)",
    re.IGNORECASE,
)


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)


def find_corruption(extraction: dict) -> str | None:
    """Return the first offending substring found, or None."""
    for s in _walk_strings(extraction):
        m = UNIT_RX.search(s)
        if m:
            return m.group(0)
    return None


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually delete the corrupted cache files")
    parser.add_argument("--show", type=int, default=5,
                        help="Show this many example corruptions (default 5)")
    args = parser.parse_args()

    if not CACHE_DIR.exists():
        print(f"Cache dir not found: {CACHE_DIR}")
        return

    corrupted: list[tuple[Path, str, str]] = []  # (path, filename, sample)
    total = 0
    for p in sorted(CACHE_DIR.glob("*.json")):
        total += 1
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sample = find_corruption(data)
        if sample:
            # Cache file name is `{auction_id}__{safe_filename}.json`; recover
            # the filename so we can tell whether MinerU markdown is available.
            _, _, tail = p.stem.partition("__")
            corrupted.append((p, tail, sample))

    print(f"Scanned: {total} cache files")
    print(f"Corrupted: {len(corrupted)}")

    if corrupted and args.show > 0:
        print(f"\nExamples (first {min(args.show, len(corrupted))}):")
        for p, _, sample in corrupted[: args.show]:
            print(f"  {p.name}  →  {sample!r}")

    if corrupted and MINERU_MARKDOWN_DIR.exists():
        with_md = sum(1 for _, fn, _ in corrupted
                      if fn and cached_markdown_for_filename(fn) is not None)
        without_md = len(corrupted) - with_md
        print(f"\nMarkdown availability for corrupted files:")
        print(f"  with cached markdown   (free to re-extract): {with_md}")
        print(f"  without cached markdown (needs MinerU call): {without_md}")

    if not corrupted:
        return

    if not args.commit:
        print("\nDry run. Re-run with --commit to delete the corrupted cache files.")
        return

    deleted = 0
    for p, _, _ in corrupted:
        try:
            p.unlink()
            deleted += 1
        except OSError as e:
            print(f"  [WARN] could not delete {p.name}: {e}")
    print(f"\nDeleted {deleted} cache files. Next `python -m pipeline.ocr_extract` will re-extract them.")


if __name__ == "__main__":
    main()
