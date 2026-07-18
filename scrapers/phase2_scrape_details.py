"""
PHASE 2 — PARALLEL DETAIL SCRAPER
====================================
Reads pending_urls.txt, scrapes each property detail page using N_WORKERS
parallel Selenium drivers, writes results to live_eauction_data.jsonl.

Must run phase1_harvest_urls.py first to populate pending_urls.txt.

Usage:
    python -u scrapers/phase2_scrape_details.py

Controls (create/delete these files to control the scraper):
    scrapers/scraper_stop.lock   -> graceful stop after current batch
    scrapers/scraper_pause.lock  -> pause until file is deleted
"""

import os
import csv
import json
import time
import sys
import threading
import requests
import queue
from datetime import datetime
from bs4 import BeautifulSoup
import utils

# Force line-buffered UTF-8 stdout — immediate output, no cp1252 crashes
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

# ── Config ───────────────────────────────────────────────────────────────────
PROJECT_ROOT  = os.path.join(os.path.dirname(__file__), "..")
OUTPUT_JSONL  = os.path.join(PROJECT_ROOT, "data", "live_eauction_data.jsonl")
OUTPUT_CSV    = os.path.join(PROJECT_ROOT, "data", "live_eauction_data.csv")
PENDING_FILE  = os.path.join(PROJECT_ROOT, "data", "pending_urls.txt")
DOWNLOAD_DIR  = os.path.join(PROJECT_ROOT, "downloads", "live_properties")
ERRORS_LOG    = os.path.join(PROJECT_ROOT, "data", "scrape_errors.log")
STOP_LOCK     = os.path.join(os.path.dirname(__file__), "scraper_stop.lock")
PAUSE_LOCK    = os.path.join(os.path.dirname(__file__), "scraper_pause.lock")
BASE_URL      = "https://www.eauctionsindia.com"

N_WORKERS     = 10   # Number of parallel Selenium drivers
BATCH_SIZE    = 50   # Rebuild CSV every N records scraped
MAX_RETRIES   = 2    # Retry detail scrape on failure before giving up
# ─────────────────────────────────────────────────────────────────────────────

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Thread-safe write primitives
write_lock    = threading.Lock()
counter_lock  = threading.Lock()
scraped_count = 0
error_count   = 0


# ── Per-thread requests session ───────────────────────────────────────────────
# requests.Session is NOT thread-safe — each worker gets its own session
_thread_local = threading.local()

def get_dl_session() -> requests.Session:
    """Return a per-thread requests.Session for safe concurrent downloads."""
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.4472.124 Safari/537.36"
            )
        })
        _thread_local.session = s
    return _thread_local.session


# ── URL normalisation ─────────────────────────────────────────────────────────
def normalise_url(href: str) -> str:
    """Ensure href is an absolute URL."""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return BASE_URL + href
    return href


# ── File Download ─────────────────────────────────────────────────────────────
def download_file(url: str, cookies: dict = None) -> str | None:
    """
    Download a sale notice / PDF to DOWNLOAD_DIR.
    Returns the local filename on success, None on failure.
    Files already on disk are skipped (idempotent).
    """
    try:
        clean_url = url.split("?")[0]
        local_filename = clean_url.split("/")[-1]

        # Ensure a valid file extension
        name, ext = os.path.splitext(local_filename)
        ext = ext.lower()
        if not ext or len(ext) < 2 or len(ext) > 5:
            local_filename += ".pdf"

        path = os.path.join(DOWNLOAD_DIR, local_filename)
        if os.path.exists(path):
            return local_filename  # Already on disk — link without re-downloading

        session = get_dl_session()
        r = session.get(url, stream=True, timeout=20, cookies=cookies)
        if r.status_code == 200:
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return local_filename

    except Exception:
        pass
    return None


# ── Download Link Extraction ──────────────────────────────────────────────────
def extract_download_links(soup: BeautifulSoup) -> set:
    """
    Extract all property document links from a parsed detail page.

    Strategy A — explicit Downloads card:
        Looks for the "Downloads" section header and pulls every <a> inside
        the following sibling content div.

    Strategy B — global page scan:
        Finds any <a> that matches these criteria:
          • href contains  .pdf              (PDF document)
          • href contains  storage/properties (server path pattern for docs)
          • link text contains  notice       (sale notice)
          • link text contains  catalogue    (property catalogue)

    All hrefs are normalised to absolute URLs before being returned.
    """
    links = set()

    # Strategy A: Downloads section
    dl_header = soup.find(
        lambda tag: tag.name in ["h5", "h4", "strong"]
        and "Downloads" in tag.get_text()
    )
    if dl_header:
        try:
            content = dl_header.parent.find_next_sibling("div")
            if content:
                for a in content.find_all("a", href=True):
                    links.add(normalise_url(a["href"]))
        except Exception:
            pass

    # Strategy B: global scan
    for a in soup.find_all("a", href=True):
        href = normalise_url(a["href"])
        txt  = a.get_text().lower()
        if (
            ".pdf" in href.lower()
            or "storage/properties" in href.lower()
            or "notice" in txt
            or "catalogue" in txt
        ):
            links.add(href)

    return links


# ── Detail Page Scraper ───────────────────────────────────────────────────────
def scrape_detail(driver, url: str, worker_id: int) -> dict | None:
    """
    Scrape one property detail page.

    Returns a dict with ALL fields including:
      - URL            : canonical property URL (primary key)
      - Title          : property title from <h1>
      - <KV fields>    : all key-value pairs from <strong> tags
      - Description    : full description block text
      - Downloads      : semicolon-separated local filenames of downloaded docs
      - downloads_list : same filenames as a JSON array (easier to query)
      - _scraped_at    : UTC ISO timestamp
      - _worker        : which worker ID scraped this (for debugging)

    Returns None only if the page itself failed to load (triggers retry).
    On partial failure (e.g. description missing), returns what was scraped.
    """
    data = {"URL": url}
    try:
        driver.get(url)
        time.sleep(1.5)

        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Sanity check — if we got an error page, bail
        page_text = soup.get_text()
        if "404" in driver.title or "Not Found" in driver.title:
            return None

        # 1. Title
        h1 = soup.find("h1")
        data["Title"] = h1.get_text(strip=True) if h1 else "N/A"

        # 2. Key-value pairs from <strong> tags
        #    Pattern: <strong>Bank Name: </strong> inside a parent element
        #    Full parent text = "Bank Name: SBI" → strip key → "SBI"
        for st in soup.find_all("strong"):
            try:
                key_text = st.get_text(strip=True)
                if ":" not in key_text:
                    continue
                key = key_text.replace(":", "").strip()
                if not key or len(key) > 80:
                    continue
                parent    = st.parent
                full_text = parent.get_text(strip=True)
                val = full_text.replace(key_text, "").replace(":", "").strip()
                if val and len(val) < 500:
                    data[key] = val
            except Exception:
                continue

        # 3. Description block
        desc_header = soup.find(
            lambda tag: tag.name in ["h5", "h4", "strong"]
            and tag.get_text(strip=True) == "Description"
        )
        if desc_header:
            try:
                content = desc_header.parent.find_next_sibling("div")
                if content:
                    data["Description"] = content.get_text(strip=True)
            except Exception:
                pass
        if "Description" not in data:
            data["Description"] = "N/A"

        # 4. Extract & download all document links
        selenium_cookies = {c['name']: c['value'] for c in driver.get_cookies()}
        download_urls = extract_download_links(soup)
        filenames = []
        for dl_url in download_urls:
            fname = download_file(dl_url, cookies=selenium_cookies)
            if fname:
                filenames.append(fname)

        filenames = sorted(set(filenames))

        # Store in two formats:
        #   Downloads      → human-readable semicolon string (backward-compat)
        #   downloads_list → JSON array (easier for downstream processing)
        data["Downloads"]      = "; ".join(filenames) if filenames else "N/A"
        data["downloads_list"] = filenames  # list of local filenames, [] if none

        # 5. Audit fields
        data["_scraped_at"] = datetime.utcnow().isoformat()
        data["_worker"]     = worker_id

        return data

    except Exception as e:
        # Only return None for catastrophic failures (page didn't load etc.)
        # so the caller knows to retry
        return None


# ── JSONL / CSV helpers ───────────────────────────────────────────────────────
def append_jsonl(record: dict):
    """Thread-safe append of one record to the master JSONL."""
    with write_lock:
        with open(OUTPUT_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def log_error(url: str, reason: str):
    with write_lock:
        with open(ERRORS_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {url} | {reason}\n")


def convert_jsonl_to_csv():
    """Rebuild the full CSV from the master JSONL — called every BATCH_SIZE records."""
    if not os.path.exists(OUTPUT_JSONL):
        return
    print("  [CSV] Rebuilding...", end=" ", flush=True)
    all_data, all_keys = [], set()
    with open(OUTPUT_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                # Exclude downloads_list (list type) from CSV — use Downloads string instead
                row.pop("downloads_list", None)
                all_data.append(row)
                all_keys.update(row.keys())
            except Exception:
                pass
    if not all_data:
        return
    priority = ["Title", "Bank Name", "Reserve Price", "EMD", "Province/State",
                "City/Town", "Downloads", "URL", "Description", "_scraped_at", "_worker"]
    fieldnames = sorted(all_keys, key=lambda x: (priority.index(x) if x in priority else 99, x))
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_data)
    print(f"done ({len(all_data):,} rows)", flush=True)


# ── Cloudflare helper ─────────────────────────────────────────────────────────
_cf_notified_lock = threading.Lock()
_cf_notified = False  # ensures the Windows popup fires once per run, not once per worker


def cf_wait(driver, worker_id: int):
    """Block until Cloudflare challenge is manually solved in the Chrome window."""
    global _cf_notified
    if "Just a moment" not in driver.title and "challenge" not in driver.current_url:
        return
    print(f"\n[Worker {worker_id}] CLOUDFLARE detected — solve CAPTCHA in Chrome window.", flush=True)
    with _cf_notified_lock:
        if not _cf_notified:
            _cf_notified = True
            utils.notify_cloudflare_block()  # one-time Windows popup — see scrapers/utils.py
    waited = 0
    while "Just a moment" in driver.title or "challenge" in driver.current_url:
        time.sleep(2)
        waited += 2
        if waited % 10 == 0:
            print(f"[Worker {worker_id}] Still waiting for CF... ({waited}s)", flush=True)
    print(f"[Worker {worker_id}] CF cleared. Resuming...", flush=True)
    time.sleep(2)


# ── Worker Thread ─────────────────────────────────────────────────────────────
def worker_thread(worker_id: int, url_queue: queue.Queue, processed_urls: set):
    global scraped_count, error_count

    driver = None
    for startup_attempt in range(1, 6):
        try:
            driver = utils.setup_driver(download_dir=DOWNLOAD_DIR)
            break
        except Exception as startup_err:
            print(f"[Worker {worker_id}] Startup driver error (attempt {startup_attempt}/5): {startup_err}. Retrying in 5s...", flush=True)
            time.sleep(5)
            
    if not driver:
        print(f"[Worker {worker_id}] Fatal: Could not initialize driver on startup. Thread exiting.", flush=True)
        return
    print(f"[Worker {worker_id}] Started — warming up driver...", flush=True)

    # Warm-up: load site once so Cloudflare cookie is established
    try:
        driver.get(BASE_URL)
        time.sleep(4)
        cf_wait(driver, worker_id)
        print(f"[Worker {worker_id}] Ready.", flush=True)
    except Exception as e:
        print(f"[Worker {worker_id}] Warmup warning: {e}", flush=True)

    while True:
        # ── Control signals ───────────────────────────────────────────────────
        if os.path.exists(STOP_LOCK):
            print(f"[Worker {worker_id}] Stop signal — exiting.", flush=True)
            break

        while os.path.exists(PAUSE_LOCK):
            time.sleep(3)

        # ── Get next URL ──────────────────────────────────────────────────────
        try:
            url = url_queue.get(timeout=10)
        except queue.Empty:
            break  # Queue exhausted — we're done

        if url in processed_urls:
            url_queue.task_done()
            continue

        # ── Scrape with retry ─────────────────────────────────────────────────
        data = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Re-check CF before each scrape (it can re-trigger mid-run)
                if "Just a moment" in driver.title:
                    cf_wait(driver, worker_id)

                data = scrape_detail(driver, url, worker_id)
                if data:
                    break
                else:
                    if attempt < MAX_RETRIES:
                        print(f"[Worker {worker_id}] Retry {attempt} for {url.split('/')[-1]}", flush=True)
                        time.sleep(2)
            except Exception as e:
                print(f"[Worker {worker_id}] Driver error on attempt {attempt}: {e}. Recreating driver...", flush=True)
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(3)
                try:
                    driver = utils.setup_driver(download_dir=DOWNLOAD_DIR)
                    print(f"[Worker {worker_id}] Driver recreated successfully.", flush=True)
                except Exception as re_err:
                    print(f"[Worker {worker_id}] Failed to recreate driver: {re_err}", flush=True)

        # ── Record result ─────────────────────────────────────────────────────
        if data:
            append_jsonl(data)
            downloads = data.get("downloads_list", [])
            dl_info = f" | docs: {len(downloads)}" if downloads else " | docs: 0"

            with counter_lock:
                processed_urls.add(url)
                scraped_count += 1
                local_count = scraped_count

            if local_count % 5 == 0:
                print(
                    f"[Worker {worker_id}] #{local_count:>4} scraped"
                    f" | {url.split('/')[-1]}{dl_info}",
                    flush=True
                )

            if local_count % BATCH_SIZE == 0:
                try:
                    convert_jsonl_to_csv()
                except PermissionError:
                    print("  [CSV] Skipped — file locked. Will retry next batch.", flush=True)
                except Exception as e:
                    print(f"  [CSV] Error: {e}", flush=True)
        else:
            log_error(url, "Failed after all retries")
            with counter_lock:
                error_count += 1
            print(f"[Worker {worker_id}] FAILED: {url.split('/')[-1]}", flush=True)

        url_queue.task_done()

    try:
        driver.quit()
    except Exception:
        pass
    print(f"[Worker {worker_id}] Done.", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global scraped_count, error_count

    # Clean up stale stop lock from previous run
    if os.path.exists(STOP_LOCK):
        os.remove(STOP_LOCK)

    print("=" * 60, flush=True)
    print("PHASE 2 — PARALLEL DETAIL SCRAPER", flush=True)
    print(f"  Workers  : {N_WORKERS}", flush=True)
    print(f"  Output   : {OUTPUT_JSONL}", flush=True)
    print(f"  Downloads: {DOWNLOAD_DIR}", flush=True)
    print("=" * 60, flush=True)

    # Load already-processed URLs (resume support)
    processed_urls = set()
    if os.path.exists(OUTPUT_JSONL):
        with open(OUTPUT_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    processed_urls.add(json.loads(line.strip())["URL"])
                except Exception:
                    pass
    print(f"Already in JSONL  : {len(processed_urls):,}", flush=True)

    # Load pending URLs
    pending = []
    if not os.path.exists(PENDING_FILE):
        print(f"\nWARNING: {PENDING_FILE} not found.", flush=True)
        print("  Run phase1_harvest_urls.py first.\n", flush=True)
        return

    with open(PENDING_FILE, "r", encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if u and u not in processed_urls:
                pending.append(u)

    print(f"Pending to scrape : {len(pending):,}", flush=True)
    print(flush=True)

    if not pending:
        print("Nothing to do — all pending URLs already scraped!", flush=True)
        return

    # Build work queue
    url_queue = queue.Queue()
    for u in pending:
        url_queue.put(u)

    total      = len(pending)
    start_time = time.time()

    # Launch worker threads — stagger by 2s so CF doesn't see a burst of sessions
    threads = []
    for i in range(1, N_WORKERS + 1):
        t = threading.Thread(
            target=worker_thread,
            args=(i, url_queue, processed_urls),
            daemon=True,
        )
        t.start()
        threads.append(t)
        time.sleep(2)

    # Progress monitor on main thread
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(15)
            elapsed   = time.time() - start_time
            rate      = scraped_count / elapsed if elapsed > 0 else 0
            remaining = total - scraped_count - error_count
            eta_min   = (remaining / rate / 60) if rate > 0 else 0
            pct       = ((scraped_count + error_count) / total * 100) if total > 0 else 0
            print(
                f"[Monitor] {scraped_count:,} scraped | {error_count} errors | "
                f"{pct:.1f}% done | {rate:.2f} rec/s | ETA {eta_min:.0f} min",
                flush=True
            )
    except KeyboardInterrupt:
        print("\n[Monitor] Ctrl-C — creating stop lock...", flush=True)
        with open(STOP_LOCK, "w") as f:
            f.write("stop")

    for t in threads:
        t.join(timeout=30)

    # Final CSV rebuild
    print("\nFinal CSV rebuild...", flush=True)
    try:
        convert_jsonl_to_csv()
    except PermissionError:
        print("  CSV locked — close the file and run convert_to_excel.py manually.", flush=True)
    except Exception as e:
        print(f"  CSV error: {e}", flush=True)

    elapsed = time.time() - start_time
    print(flush=True)
    print("=" * 60, flush=True)
    print("PHASE 2 COMPLETE", flush=True)
    print(f"  Scraped this run : {scraped_count:,}", flush=True)
    print(f"  Errors           : {error_count}", flush=True)
    print(f"  Time elapsed     : {elapsed/60:.1f} min", flush=True)
    print(f"  Output           : {OUTPUT_JSONL}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
