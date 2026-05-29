
import os
import csv
import json
import time
import requests
import re
import concurrent.futures
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import utils

def extract_total_count(soup):
    try:
        # Pattern: "17,869 Live Bank Auctions"
        target_text = soup.find(string=re.compile(r'\d+(?:,\d+)*\s*Live\s*Bank\s*Auctions', re.I))
        if target_text:
            match = re.search(r'([\d,]+)', target_text)
            if match:
                num = match.group(1).replace(",", "")
                return int(num)
    except: pass
    return 18000 # Fallback

# Configuration
START_URL = "https://www.eauctionsindia.com/live-properties"
BASE_URL = "https://www.eauctionsindia.com"
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')
DOWNLOAD_DIR = os.path.join(PROJECT_ROOT, "downloads", "live_properties")
OUTPUT_JSONL = os.path.join(PROJECT_ROOT, "data", "live_eauction_data.jsonl")
OUTPUT_CSV = os.path.join(PROJECT_ROOT, "data", "live_eauction_data.csv")
MAX_DETAIL_WORKERS = 5  # Concurrent Selenium detail scrapers

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Session for file downloads only (PDFs etc. — these don't need Cloudflare bypass)
download_session = requests.Session()
download_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.4472.124 Safari/537.36"
})

def download_file(url):
    try:
        clean_url = url.split('?')[0]
        local_filename = clean_url.split('/')[-1]
        
        name, ext = os.path.splitext(local_filename)
        ext = ext.lower()
        if not ext or len(ext) < 4 or len(ext) > 5:
             if not ext: local_filename += ".pdf"

        path = os.path.join(DOWNLOAD_DIR, local_filename)
        if os.path.exists(path): return local_filename
        
        r = download_session.get(url, stream=True, timeout=15)
        if r.status_code == 200:
            with open(path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return local_filename
    except: pass
    return None

def scrape_detail_selenium(driver, url):
    """Scrape a single detail page using the provided Selenium driver."""
    data = {"URL": url}
    try:
        driver.get(url)
        time.sleep(1.5)  # Wait for page load
        
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        
        # 1. Title
        h1 = soup.find("h1")
        data["Title"] = h1.get_text(strip=True) if h1 else "N/A"
        
        # 2. Key-Values (Strong tags)
        strongs = soup.find_all("strong")
        for st in strongs:
            try:
                key_text = st.get_text(strip=True)
                if ":" in key_text:
                    key = key_text.replace(":", "").strip()
                    parent = st.parent
                    full_text = parent.get_text(strip=True)
                    val = full_text.replace(key_text, "").replace(":", "").strip()
                    if key and val and len(val) < 500:
                        data[key] = val
            except: continue

        # 3. Description
        desc_header = soup.find(lambda tag: tag.name in ['h5', 'h4', 'strong'] and "Description" == tag.get_text(strip=True))
        if desc_header:
            try:
                parent = desc_header.parent
                content = parent.find_next_sibling("div")
                if content:
                    data["Description"] = utils.strip_field_bleed(content.get_text(strip=True))
            except: pass
        
        if "Description" not in data: 
             data["Description"] = "N/A"

        # 4. Downloads
        filenames = []
        download_links = set()
        
        # Strategy A: Downloads Section
        dl_header = soup.find(lambda tag: "Downloads" in tag.get_text() and tag.name in ['h5', 'strong'])
        if dl_header:
            try:
                parent = dl_header.parent
                content = parent.find_next_sibling("div")
                if content:
                    links = content.find_all("a")
                    for l in links:
                        href = l.get('href')
                        if href: download_links.add(href)
            except: pass

        # Strategy B: Global
        for a in soup.find_all("a", href=True):
            href = a['href']
            if href.startswith("/"): href = BASE_URL + href
            
            txt = a.get_text().lower()
            if ".pdf" in href.lower() or "storage/properties" in href.lower() or "notice" in txt.lower():
                download_links.add(href)
                
        for dl in download_links:
             fname = download_file(dl)
             if fname: filenames.append(fname)
             
        # Deduplicate filenames
        filenames = sorted(list(set(filenames)))
        data["Downloads"] = "; ".join(filenames) if filenames else "N/A"
        
        return data

    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return None

def convert_jsonl_to_csv():
    if not os.path.exists(OUTPUT_JSONL): return
    print(f"Converting {OUTPUT_JSONL} to {OUTPUT_CSV}...")
    
    all_data = []
    all_keys = set()
    
    with open(OUTPUT_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                all_data.append(row)
                all_keys.update(row.keys())
            except: pass
            
    if not all_data: return

    # Priority columns
    priority = ["Title", "Bank Name", "Reserve Price", "EMD", "City", "State", "Downloads", "URL", "Description"]
    fieldnames = list(all_keys)
    fieldnames.sort(key=lambda x: (priority.index(x) if x in priority else 99, x))
    
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_data)
    print("CSV Conversion Complete.")

def main():
    print(f"Starting Selenium-based Scraper... Saving to {OUTPUT_JSONL}")
    
    start_page = 1
    max_pages = 2500
    
    # Setup Selenium driver for listing pages
    print("Initializing Selenium driver...")
    driver = utils.setup_driver(download_dir=DOWNLOAD_DIR)
    
    # Get initial Total
    try:
        print("Navigating to start URL...")
        driver.get(START_URL)
        time.sleep(5)  # Wait for Cloudflare challenge + page load
        
        # Check if Cloudflare is blocking us
        print("Checking for Cloudflare block...")
        if "Just a moment" in driver.title or "Cloudflare" in driver.page_source:
             print("\n=======================================================")
             print("🚨 CLOUDFLARE DETECTED 🚨")
             print("The site is blocking our automated scraper.")
             print("A Chrome window is open. Please go to it and MANUALLY solve the CAPTCHA/challenge.")
             print("The script will wait until you pass the challenge...")
             print("=======================================================\n")
             
             # Wait until the title changes from "Just a moment..."
             wait_time = 0
             while "Just a moment" in driver.title or "challenge" in driver.current_url:
                  time.sleep(2)
                  wait_time += 2
                  if wait_time % 10 == 0:
                       print("Still waiting for you to pass CF... (do it in the opened Chrome window)")
             
             print("\n✅ Cloudflare passed! Resuming automation in 5 seconds...")
             time.sleep(5)

        soup0 = BeautifulSoup(driver.page_source, 'html.parser')
        total_items = extract_total_count(soup0)
        print(f"Total Properties to Scrape: {total_items}")
    except Exception as e:
        print(f"Error loading start page or checking CF: {e}")
        total_items = 18000
    
    # Check resume
    processed_urls = set()
    if os.path.exists(OUTPUT_JSONL):
         with open(OUTPUT_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                try: processed_urls.add(json.loads(line)["URL"])
                except: pass
    print(f"Resuming: {len(processed_urls)} already done.")
    
    # Setup Metrics
    start_time = time.time()
    scraped_count = 0
    empty_page_streak = 0

    try:
        for page in range(start_page, max_pages + 1):
            if page == 1:
                list_url = f"{START_URL}"
            else:
                list_url = f"{START_URL}/{page}"
            
            # Check Controls
            if os.path.exists("scraper_stop.lock"):
                print(">> STOP Signal detected. Exiting...")
                break
                
            while os.path.exists("scraper_pause.lock"):
                print(">> PAUSED. Waiting for resume signal...", end='\r')
                time.sleep(5)
            
            # Progress Calc
            current_total = len(processed_urls)
            progress = (current_total / total_items) * 100 if total_items > 0 else 0
            
            # Rate calc
            elapsed = time.time() - start_time
            if scraped_count > 0:
                rate = scraped_count / elapsed
                remaining = total_items - current_total
                eta_sec = remaining / rate if rate > 0 else 0
                eta_min = eta_sec / 60
                print(f"Scanning Page {page} | Progress: {current_total}/{total_items} ({progress:.2f}%) | ETA: {eta_min:.1f} min")
            else:
                print(f"Scanning Page {page} | Progress: {current_total}/{total_items} ({progress:.2f}%)")
            
            try:
                # Use Selenium to load listing page (bypasses Cloudflare)
                driver.get(list_url)
                time.sleep(3)  # Let page fully load
                
                # Check for errors
                page_source = driver.page_source
                if "Just a moment" in driver.title or "Access denied" in page_source or "blocked" in page_source.lower():
                    print(f"\nPage {page}: Cloudflare or Access Denied block re-engaged!")
                    print("Please check the Chrome window and solve any challenges.")
                    while "Just a moment" in driver.title or "Access denied" in driver.page_source:
                        time.sleep(3)
                    print("Block cleared. Retrying page...")
                    driver.get(list_url)
                    time.sleep(5)
                    page_source = driver.page_source
                
                soup = BeautifulSoup(page_source, 'html.parser')
                
                # Find detail links
                links = set()
                for a in soup.find_all("a", href=True):
                    href = a['href']
                    if "/properties/" in href:
                        if href.startswith("/"): href = BASE_URL + href
                        links.add(href)
                
                if not links:
                    empty_page_streak += 1
                    print(f"No properties found (streak: {empty_page_streak}). End of listings?")
                    if empty_page_streak >= 3:
                        print("3 consecutive empty pages. Stopping.")
                        break
                    continue
                else:
                    empty_page_streak = 0
                
                new_links = [l for l in links if l not in processed_urls]
                print(f"  Found {len(links)} links ({len(new_links)} new).")
                
                if not new_links:
                    continue

                # Scrape detail pages sequentially using the same Selenium driver
                # (Selenium drivers are not thread-safe, so we do this sequentially)
                for i, url in enumerate(new_links):
                    try:
                        data = scrape_detail_selenium(driver, url)
                        if data:
                            with open(OUTPUT_JSONL, "a", encoding="utf-8") as f:
                                line = json.dumps(data) + "\n"
                                f.write(line)
                                f.flush()
                                os.fsync(f.fileno())
                            processed_urls.add(url)
                            scraped_count += 1
                            
                            if scraped_count % 20 == 0:
                                elapsed_now = time.time() - start_time
                                rate = scraped_count / elapsed_now
                                print(f"    [{scraped_count} scraped | {rate:.1f}/s]")
                            
                            if scraped_count % 100 == 0:
                                try:
                                    convert_jsonl_to_csv()
                                except PermissionError:
                                    print("    Warning: CSV locked. Skipping CSV update..")
                                except Exception as e:
                                    print(f"    Warning: CSV update failed: {e}")
                    except Exception as e:
                        print(f"    Failed {url}: {e}")
                        with open(os.path.join(PROJECT_ROOT, "data", "scrape_errors.log"), "a") as errf:
                            errf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {url} - {str(e)}\n")
                
                # After scraping details, navigate back to listing for next page
                # (driver.get in the loop above changes the page)
                        
            except Exception as e:
                print(f"Error on page {page}: {e}")
                time.sleep(5) # Backoff
                
    except KeyboardInterrupt:
        print("\nStopping by user request...")
    finally:
        try:
            convert_jsonl_to_csv()
        except PermissionError:
            print("\n[IMPORTANT] Final CSV conversion failed: Permission Denied.")
            print("Please close 'live_eauction_data.csv' and run the script again or manually convert.")
        except Exception as e:
            print(f"\n[ERROR] Final CSV conversion failed: {e}")
        
        print(f"\nScraper finished. Total new records scraped this session: {scraped_count}")
        print(f"Total entries in {OUTPUT_JSONL}: {len(processed_urls)}")
        
        try:
            driver.quit()
        except: pass

if __name__ == "__main__":
    main()
