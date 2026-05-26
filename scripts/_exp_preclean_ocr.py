"""One-off experiment: does pre-cleaning the scan improve MinerU OCR?

Hypothesis: the dominant cause of OCR errors on notice
9b6c180b-0d13-4d2b-ae71-2bb2d373b21517785819725241.jpg is under-resolution
input (518x700 @ 96 DPI). Test by running MinerU twice -- raw vs.
2x LANCZOS upscale + unsharp mask -- and diffing the markdown.

Outputs land in scripts/_exp_preclean_out/.
"""
from __future__ import annotations

import time
from pathlib import Path

import requests
from PIL import Image, ImageFilter

from pipeline.mineru_api import (
    download_zip,
    parse_zip_payload,
    poll,
    request_batch,
    upload_files,
)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "downloads" / "live_properties" / "9b6c180b-0d13-4d2b-ae71-2bb2d373b21517785819725241.jpg"
OUT = ROOT / "scripts" / "_exp_preclean_out"
OUT.mkdir(exist_ok=True)


def preclean(src_path: Path, factor: int = 2) -> Path:
    """Upscale LANCZOS + unsharp mask. Returns path to cleaned JPEG.

    Saved as JPEG q=95 (not PNG) -- a 3x upscale of a 518x700 source is
    ~3 MB as PNG and MinerU's OSS endpoint resets the connection on
    uploads that large. JPEG q=95 brings it under 500 KB with no visible
    OCR-relevant quality loss.
    """
    im = Image.open(src_path).convert("RGB")
    w, h = im.size
    im = im.resize((w * factor, h * factor), Image.LANCZOS)
    im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    out_path = OUT / "cleaned.jpg"
    im.save(out_path, format="JPEG", quality=95)
    print(f"  wrote {out_path}  ({w*factor}x{h*factor}, {out_path.stat().st_size//1024} KB)")
    return out_path


def ocr(disk_path: Path, label: str, attempts: int = 3) -> str:
    """Submit one file to MinerU, return the markdown.

    Retries the whole batch (new batch_id + fresh signed URL) on
    connection-reset errors, the same pattern stage1_mineru uses for the
    bulk path. The OSS signed URLs sometimes RST mid-upload from this
    ISP -- a fresh batch usually goes through.
    """
    item = {
        "filename":  disk_path.name,
        "file_path": f"exp/{label}_{disk_path.name}",
        "disk_path": disk_path,
    }
    last_err: Exception | None = None
    for attempt in range(attempts):
        try:
            batch_id, urls = request_batch([item])
            print(f"  [{label}] attempt {attempt+1}/{attempts}  batch_id={batch_id}")
            upload_files([item], urls)
            results = poll(batch_id, timeout_s=300)
            if not results or results[0].get("state") != "done":
                raise RuntimeError(f"MinerU failed: {results}")
            zip_bytes = download_zip(results[0]["full_zip_url"])
            md, _ = parse_zip_payload(zip_bytes or b"")
            return md or ""
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_err = e
            wait = 5 * (attempt + 1)
            print(f"  [transient] {type(e).__name__}; retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"MinerU failed after {attempts} attempts: {last_err}")


CHECKS = {
    "Cholan (correct spelling)":    "Cholan",
    "Chilan (misread variant)":     "Chilan",
    "Canara":                       "Canara",
    "Chennai":                      "Chennai",
    "Reserve price 31,50,000":      "31,50,000",
    "Auction date 22.06.2026":      "22.06.2026",
    "EMD date 21.06.2026":          "21.06.2026",
    "BAANKNET":                     "BAANKNET",
    "Sugavaneswaran":               "Sugavaneswaran",
    "Tiruchirapalli/Trichi/Trichy": None,  # informational only
}


def stats(text: str, label: str) -> None:
    print(f"\n=== {label} ===")
    print(f"  chars: {len(text)}  lines: {text.count(chr(10)) + 1}")
    for desc, needle in CHECKS.items():
        if needle is None:
            continue
        present = needle in text
        mark = "OK " if present else "-- "
        print(f"  {mark} {desc}: {present}")


def main() -> None:
    print(f"Source: {SRC}")
    print(f"Outputs: {OUT}\n")

    raw_path = OUT / "raw.md"
    if raw_path.exists():
        raw_md = raw_path.read_text(encoding="utf-8")
        print(f"[1/2] OCR raw image: REUSING cached {raw_path} ({len(raw_md)} chars)")
    else:
        print("[1/2] OCR raw image")
        raw_md = ocr(SRC, "raw")
        raw_path.write_text(raw_md, encoding="utf-8")
        print(f"  wrote {raw_path}  ({len(raw_md)} chars)")

    print("\n[2/2] Pre-clean and OCR cleaned image")
    cleaned_path = preclean(SRC, factor=3)
    cleaned_md = ocr(cleaned_path, "cleaned")
    (OUT / "cleaned.md").write_text(cleaned_md, encoding="utf-8")
    print(f"  wrote {OUT/'cleaned.md'}  ({len(cleaned_md)} chars)")

    stats(raw_md, "RAW (518x700 @ 96 DPI)")
    stats(cleaned_md, "CLEANED (3x LANCZOS + unsharp mask)")


if __name__ == "__main__":
    main()
