
import os
import time
import json
import csv
import requests
import utils
from tqdm import tqdm
from dotenv import load_dotenv
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Load environment variables (optional for this site)
load_dotenv()

OUTPUT_JSONL = "eauction_data.jsonl"
OUTPUT_CSV = "eauction_data.csv"
DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")

# Ensure download directory exists
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def extract_details(driver):
    """
    Extracts key-value pairs from the detail page using strong tags.
    Also grabs description and checks for downloads.
    """
    data = {}
    try:
        # 1. Main Title
        try:
            data['Title'] = driver.find_element(By.TAG_NAME, "h1").text.strip()
        except:
            data['Title'] = "N/A"
        
        data['URL'] = driver.current_url

        # 2. Extract Key-Value pairs based on 'Strong' tags
        strongs = driver.find_elements(By.TAG_NAME, "strong")
        for st in strongs:
            try:
                text = st.text.strip()
                if ":" in text:
                    key = text.replace(":", "").strip()
                    
                    # Try to get value from parent text
                    parent = st.find_element(By.XPATH, "..")
                    full_text = parent.text.strip()
                    
                    # Remove the key from the full text to get value
                    val = full_text.replace(text, "").replace(":", "").strip()
                    
                    if key and val and len(val) < 500: 
                        data[key] = val
            except:
                continue

        # 3. Extract Description
        try:
            # Locate the Description header (often in a card-header h5)
            # We want the content in the *following* sibling container (usually card-body)
            desc_header = driver.find_element(By.XPATH, "//*[contains(text(), 'Description')]")
            
            # Helper to find the content container
            # Go up to the container that holds the header (e.g. card-header or just a div)
            # Then look for the next sibling div
            
            # Simple approach: Get parent path, then following sibling
            parent = desc_header.find_element(By.XPATH, "./..") 
            content_div = parent.find_element(By.XPATH, "following-sibling::div")
            
            check_text = content_div.text.strip()
            if check_text:
                data['Description'] = check_text
            else:
                data['Description'] = "N/A"
        except:
             data['Description'] = "N/A"

        # 4. Downloads
        filenames = []
        download_links_found = set()
        
        # Strategy A: Look for "Downloads" container
        try:
            dl_header = driver.find_element(By.XPATH, "//*[contains(text(), 'Downloads')]")
            # Assuming card header -> parent -> card body (sibling)
            parent = dl_header.find_element(By.XPATH, "./..") 
            # Try finding the content div (next sibling)
            content_div = parent.find_element(By.XPATH, "following-sibling::div")
            
            # Get all links in this specific container
            container_links = content_div.find_elements(By.TAG_NAME, "a")
            for cl in container_links:
                href = cl.get_attribute("href")
                if href and href not in download_links_found:
                    download_links_found.add(href)
        except:
             # Header not found or structure differs
             pass

        # Strategy B: Global search for likely file patterns
        # The snippet showed: /storage/properties/2026-01-30/...
        try:
            all_links = driver.find_elements(By.TAG_NAME, "a")
            for link in all_links:
                try:
                    href = link.get_attribute("href")
                    if not href: continue
                    
                    text = link.text.lower()
                    
                    is_candidate = False
                    if href in download_links_found: continue # Already found
                    
                    if ".pdf" in href.lower(): is_candidate = True
                    elif "storage/properties" in href.lower(): is_candidate = True
                    elif "sale notice" in text: is_candidate = True
                    elif "catalogue" in text: is_candidate = True
                    
                    # specific check for the user's observed structure
                    # <strong class="mr-2">Sale Notice 1: </strong> ... <a>
                    
                    if is_candidate:
                         download_links_found.add(href)
                except:
                    continue
        except:
            pass

        # Execute Downloads
        if download_links_found:
            for url in download_links_found:
                fname = download_file(url)
                if fname:
                    filenames.append(fname)
            
        if filenames:
            data["Downloads"] = "; ".join(filenames)
        else:
             data["Downloads"] = "N/A"

    except Exception as e:
        print(f"Error extracting details: {e}")
        
    return data

def download_file(url):
    """Downloads a file from a URL to the local downloads directory."""
    try:
        # Clean URL of query params
        clean_url = url.split('?')[0]
        local_filename = clean_url.split('/')[-1]
        
        # Robust extension check
        name, ext = os.path.splitext(local_filename)
        ext = ext.lower()
        
        # If extension is missing or too long/short to be valid, default to pdf
        # Common image/doc extensions are 3-4 chars (.jpg, .jpeg, .png, .pdf, .doc)
        if not ext or len(ext) < 4 or len(ext) > 5:
             # Double check if it ends with a known pattern that splitext missed?
             # But splitext is reliable.
             # If "unknown", append .pdf
             if not ext:
                 local_filename += ".pdf"
        
        # Safety: If it ends in .jpg.pdf or similar, fix it? 
        # No, just trust the logic above.
            
        path = os.path.join(DOWNLOAD_DIR, local_filename)
        
        if os.path.exists(path):
            return local_filename # Skip if exists
            
        response = requests.get(url, stream=True, timeout=10)
        if response.status_code == 200:
            with open(path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return local_filename
    except Exception as e:
        # print(f"Download failed for {url}: {e}")
        pass
    return None

def save_to_jsonl(data):
    with open(OUTPUT_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(data) + "\n")

def convert_jsonl_to_csv():
    if not os.path.exists(OUTPUT_JSONL):
        return
    
    all_data = []
    all_keys = set()
    
    with open(OUTPUT_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            try:
                if not line.strip(): continue
                row = json.loads(line)
                all_data.append(row)
                all_keys.update(row.keys())
            except:
                continue
    
    if not all_data:
        return

    fieldnames = list(all_keys)
    
    # Priority Columns (User Requested)
    priority_map = {
        "Auction ID": 1,
        "Bank Name": 2,
        "Reserve Price": 3,
        "EMD": 4,
        "Title": 5,
        "Branch Name": 6,
        "Service Provider": 7,
        "Contact Details": 8, 
        "Mobile No": 9,
        "State": 10,
        "City": 11,
        "Area": 12,
        "Description": 13,
        "Borrower Name": 14,
        "Asset Category": 15,
        "Property Type": 16,
        "Auction Start Date": 17,
        "Auction End Time": 18,
        "Application Submission Date": 19,
        "Downloads": 20,
        "URL": 99
    }
    
    # Sort fieldnames based on priority, remaining alphabetical
    sorted_fieldnames = sorted(fieldnames, key=lambda x: (priority_map.get(x, 50), x))
            
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=sorted_fieldnames)
        writer.writeheader()
        writer.writerows(all_data)
    
    print(f"Success: Converted {len(all_data)} rows to {OUTPUT_CSV}")

def run_scraper():
    # 1. Setup
    driver = utils.setup_driver() # Reusing existing utils
    
    # Check for resume
    processed_urls = set()
    if os.path.exists(OUTPUT_JSONL):
        print("Resuming from existing JSONL...")
        with open(OUTPUT_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if "URL" in r: processed_urls.add(r["URL"])
                except: pass
    
    start_url = "https://www.eauctionsindia.com/search"
    print(f"Starting scraper at: {start_url}")
    driver.get(start_url)
    time.sleep(3)
    
    page_num = 1
    
    try:
        while True:
            print(f"--- Processing Page {page_num} ---")
            
            # 2. Get Links
            # Identify property cards. From exploration: row/card classes or just links with 'properties' in href
            # A safer generic approach for this site: Find all 'View Details' buttons or similar links
            
            # Re-evaluating selectors from exploration:
            # Listings are in `div.row`.
            # Links to details contain `properties/`.
            
            property_links = []
            try:
                # Find all links containing 'properties/'
                elements = driver.find_elements(By.XPATH, "//a[contains(@href, '/properties/')]")
                # Deduplicate
                seen_on_page = set()
                for el in elements:
                    url = el.get_attribute("href")
                    if url and url not in seen_on_page and url not in processed_urls:
                        property_links.append(url)
                        seen_on_page.add(url)
            except Exception as e:
                print(f"Error finding links: {e}")
            
            print(f"Found {len(property_links)} new properties on this page.")
            
            # 3. Pagination detection (Next Button) - Check BEFORE processing
            next_url = None
            try:
                # specific selector for Next button? 
                # Usually "Next" text or > icon.
                # Let's try text based.
                next_btns = driver.find_elements(By.XPATH, "//a[contains(text(), 'Next') or contains(text(), '»')]")
                for btn in next_btns:
                    if btn.is_displayed() and btn.is_enabled():
                        next_url = btn.get_attribute("href")
                        # If href is javascript or #, we might need to click.
                        # For now assume href is valid or we click it at end.
                        break
            except:
                pass


            # 4. Visit Details
            for link in property_links:
                try:
                    print(f"Scraping: {link}")
                    driver.get(link)
                    details = extract_details(driver)
                    save_to_jsonl(details)
                    processed_urls.add(link)
                    # Don't wait too long, be polite but fast
                    time.sleep(1) 
                except Exception as e:
                    print(f"Failed to scrape {link}: {e}")
            
            # 5. Navigate Next
            if next_url:
                print(f"Navigating to next page: {next_url}")
                driver.get(next_url)
                page_num += 1
                time.sleep(3) 
            else:
                # Inspect Pagination Link Debug
                try:
                   btns = driver.find_elements(By.XPATH, "//a[contains(text(), 'Next')]")
                   for b in btns:
                       print(f"DEBUG NEXT BTN: {b.get_attribute('outerHTML')}")
                except: pass

                # Try clicking if URL wasn't found
                try:
                    btn = driver.find_element(By.XPATH, "//a[contains(text(), 'Next')]")
                    print("Clicking Next button...")
                    btn.click()
                    page_num += 1
                    time.sleep(5)
                except:
                    print("No Next button found. Stopping.")
                    break
        
            # Force break for debug
            if page_num > 1: break

    except KeyboardInterrupt:
        print("Stopping by user request...")
    except Exception as e:
        print(f"Critical Error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    run_scraper()
