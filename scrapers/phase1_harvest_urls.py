"""
PHASE 1 — URL HARVESTER
========================
Scrapes the eauctionsindia.com listing pages and collects all property URLs.
Saves new (not-yet-scraped) URLs into data/pending_urls.txt.

Run this first, then run phase2_scrape_details.py.

Usage:
    python scrapers/phase1_harvest_urls.py
"""

import os
import re
import sys
import json
import time
from bs4 import BeautifulSoup
import utils

# Force line-buffered UTF-8 stdout so every print() appears immediately
# and Unicode characters don't crash on Windows cp1252 terminal
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

# ── Config ──────────────────────────────────────────────────────────────────
START_URL       = "https://www.eauctionsindia.com/live-properties"
BASE_URL        = "https://www.eauctionsindia.com"
PROJECT_ROOT    = os.path.join(os.path.dirname(__file__), "..")
OUTPUT_JSONL    = os.path.join(PROJECT_ROOT, "data", "live_eauction_data.jsonl")
PENDING_FILE    = os.path.join(PROJECT_ROOT, "data", "pending_urls.txt")
STOP_LOCK       = os.path.join(os.path.dirname(__file__), "scraper_stop.lock")
PAUSE_LOCK      = os.path.join(os.path.dirname(__file__), "scraper_pause.lock")
MAX_PAGES          = 3000
EMPTY_STREAK_LIMIT = 3   # consecutive pages with NO links at all → end of pagination
STALE_STREAK_LIMIT = 5   # consecutive pages with 0 NEW urls → back in already-scraped territory
# ─────────────────────────────────────────────────────────────────────────────


def extract_total_count(soup):
    """Read '17,869 Live Bank Auctions' text from the page."""
    try:
        target = soup.find(string=re.compile(r"\d+(?:,\d+)*\s*Live\s*Bank\s*Auctions", re.I))
        if target:
            m = re.search(r"([\d,]+)", target)
            if m:
                return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return 20000  # safe fallback


def cf_wait(driver):
    """Block until Cloudflare challenge is cleared (user solves it manually)."""
    if "Just a moment" not in driver.title and "challenge" not in driver.current_url:
        return
    print("\n" + "="*55, flush=True)
    print("🚨  CLOUDFLARE DETECTED", flush=True)
    print("Solve the CAPTCHA in the Chrome window that opened.", flush=True)
    print("The script will resume automatically once you pass.", flush=True)
    print("="*55 + "\n", flush=True)
    utils.notify_cloudflare_block()  # one-time Windows popup — see scrapers/utils.py
    waited = 0
    while "Just a moment" in driver.title or "challenge" in driver.current_url:
        time.sleep(2)
        waited += 2
        if waited % 10 == 0:
            print(f"  Still waiting for CF solve… ({waited}s)", flush=True)
    print("✅  Cloudflare cleared! Resuming in 3 s…", flush=True)
    time.sleep(3)


def load_already_processed() -> set:
    """Return set of URLs already in the master JSONL (already fully scraped)."""
    done = set()
    if os.path.exists(OUTPUT_JSONL):
        with open(OUTPUT_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line.strip())["URL"])
                except Exception:
                    pass
    return done


def load_pending() -> set:
    """Return set of URLs already written to pending_urls.txt."""
    existing = set()
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            for line in f:
                u = line.strip()
                if u:
                    existing.add(u)
    return existing


def main():
    # Clean up any stale stop lock from previous run
    if os.path.exists(STOP_LOCK):
        os.remove(STOP_LOCK)

    print("=" * 55, flush=True)
    print("PHASE 1 — URL HARVESTER", flush=True)
    print("=" * 55, flush=True)

    already_scraped = load_already_processed()
    already_pending = load_pending()
    known_urls      = already_scraped | already_pending

    print(f"  Already scraped  : {len(already_scraped):,}", flush=True)
    print(f"  Already pending  : {len(already_pending):,}", flush=True)
    print(f"  Total known URLs : {len(known_urls):,}", flush=True)
    print(flush=True)

    driver = utils.setup_driver()

    # ── Bootstrap: load listing page and handle CF ───────────────────────────
    print("Loading listing page…", flush=True)
    driver.get(START_URL)
    time.sleep(5)
    cf_wait(driver)

    soup0       = BeautifulSoup(driver.page_source, "html.parser")
    total_items = extract_total_count(soup0)
    print(f"Portal reports ~{total_items:,} live auctions.\n", flush=True)

    # ── Pagination loop ──────────────────────────────────────────────────────
    new_urls      = []
    empty_streak  = 0
    stale_streak  = 0   # pages with links but 0 new URLs
    start_time    = time.time()

    try:
        for page in range(1, MAX_PAGES + 1):
            # Stop / pause signals
            if os.path.exists(STOP_LOCK):
                print(">> STOP signal detected. Exiting…", flush=True)
                break
            while os.path.exists(PAUSE_LOCK):
                print(">> PAUSED. Delete scraper_pause.lock to resume…", end="\r", flush=True)
                time.sleep(5)

            list_url = START_URL if page == 1 else f"{START_URL}/{page}"

            elapsed  = time.time() - start_time
            rate     = len(new_urls) / elapsed if elapsed > 0 else 0
            print(f"Page {page:>4}  |  new collected: {len(new_urls):>5}  |  {rate:.1f} urls/s", flush=True)

            try:
                driver.get(list_url)
                time.sleep(2.5)

                # Re-check CF on every page (it can re-trigger)
                cf_wait(driver)

                if "Access denied" in driver.page_source or "blocked" in driver.page_source.lower():
                    print(f"  ⚠️  Access blocked on page {page}. Waiting 10 s…", flush=True)
                    time.sleep(10)
                    driver.get(list_url)
                    time.sleep(5)

                soup  = BeautifulSoup(driver.page_source, "html.parser")
                links = set()
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/properties/" in href:
                        if href.startswith("/"):
                            href = BASE_URL + href
                        links.add(href)

                if not links:
                    empty_streak += 1
                    print(f"  No properties found (empty streak: {empty_streak}/{EMPTY_STREAK_LIMIT})", flush=True)
                    if empty_streak >= EMPTY_STREAK_LIMIT:
                        print("  Reached end of pagination. Stopping.", flush=True)
                        break
                    continue
                else:
                    empty_streak = 0

                truly_new = [u for u in links if u not in known_urls]
                print(f"  Found {len(links)} links -> {len(truly_new)} new", flush=True)

                if truly_new:
                    stale_streak = 0
                    with open(PENDING_FILE, "a", encoding="utf-8") as pf:
                        for u in truly_new:
                            pf.write(u + "\n")
                    new_urls.extend(truly_new)
                    known_urls.update(truly_new)
                else:
                    stale_streak += 1
                    print(f"  [stale streak: {stale_streak}/{STALE_STREAK_LIMIT}]", flush=True)
                    if stale_streak >= STALE_STREAK_LIMIT:
                        print(f"  {STALE_STREAK_LIMIT} consecutive pages with 0 new URLs — reached already-scraped territory. Stopping early.", flush=True)
                        break

            except Exception as e:
                print(f"  Error on page {page}: {e}", flush=True)
                time.sleep(5)

    except KeyboardInterrupt:
        print("\nStopped by user.", flush=True)
    finally:
        driver.quit()

    print(flush=True)
    print("=" * 55, flush=True)
    print("PHASE 1 COMPLETE", flush=True)
    print(f"  New URLs discovered this run : {len(new_urls):,}", flush=True)
    print(f"  Total pending (all time)     : {len(load_pending()):,}", flush=True)
    print(f"  Saved to: {PENDING_FILE}", flush=True)
    print("=" * 55, flush=True)
    print("\nNext step → run:  python scrapers/phase2_scrape_details.py", flush=True)


if __name__ == "__main__":
    main()
