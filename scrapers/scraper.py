
import os
import time
import json
import csv
import utils
from tqdm import tqdm
from dotenv import load_dotenv
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Load environment variables
load_dotenv()

EMAIL = os.getenv("FINDAUCTION_EMAIL")
PASSWORD = os.getenv("FINDAUCTION_PASSWORD")

def extract_details(driver):
    """
    Extracts all key-value pairs from the definition list on the page.
    """
    data = {}
    try:
        # Get all DT/DD pairs inside the .auction_desc container
        auction_desc = driver.find_elements(By.CSS_SELECTOR, ".auction_desc dl dt")
        for dt_el in auction_desc:
            try:
                # The DD is usually the next sibling of DT
                key = dt_el.text.strip()
                val_el = dt_el.find_element(By.XPATH, "following-sibling::dd[1]")
                val = val_el.text.strip()
                if key and val:
                    data[key] = val
            except Exception:
                continue

        # Also get the title/header
        try:
            data['Title'] = driver.find_element(By.TAG_NAME, "h2").text.strip()
        except:
            data['Title'] = "N/A"
            
        data['URL'] = driver.current_url
    except Exception as e:
        pass
        
    return data

def save_to_jsonl(data, filename="auction_data.jsonl"):
    with open(filename, "a", encoding="utf-8") as f:
        f.write(json.dumps(data) + "\n")

def convert_jsonl_to_csv(jsonl_file="auction_data.jsonl", csv_file="auction_data.csv"):
    if not os.path.exists(jsonl_file):
        return
    
    all_data = []
    all_keys = set()
    
    with open(jsonl_file, "r", encoding="utf-8") as f:
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
    priority_cols = ["Title", "Reserve Price", "Auction Start Date & Time", "City", "URL", "PDF_Filename"]
    
    sorted_fieldnames = []
    for col in priority_cols:
        if col in fieldnames:
            sorted_fieldnames.append(col)
            fieldnames.remove(col)
    
    sorted_fieldnames.extend(sorted(fieldnames))
            
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=sorted_fieldnames)
        writer.writeheader()
        writer.writerows(all_data)

def run_scraper():
    if not EMAIL or not PASSWORD:
        print("Error: Credentials not found in .env file.")
        return

    # 1. Load already processed URLs to resume
    processed_links = set()
    if os.path.exists("auction_data.jsonl"):
        print("Resuming from existing auction_data.jsonl...")
        with open("auction_data.jsonl", "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if "URL" in row:
                        processed_links.add(row["URL"])
                except:
                    continue
    
    print(f"Skipping {len(processed_links)} already processed items.")

    driver = utils.setup_driver()
    
    try:
        if not utils.login(driver, EMAIL, PASSWORD):
            print("Login failed.")
            return

        start_url = "https://findauction.in/bank-property/chennai"
        driver.get(start_url)
        
        # 2. Get Total Count for Progress Bar
        total_count = 0
        try:
            count_text = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "h1.listingh1"))
            ).text
            total_count = int(count_text.split()[0].replace(',', ''))
            print(f"Total Properties Found: {total_count}")
        except Exception:
            print("Could not determine total property count.")

        pbar = tqdm(total=total_count if total_count > 0 else None, desc="Scraping Chennai Auctions", unit="property")
        pbar.update(len(processed_links))
        
        # 3. Optimize: Jump to the approximate page
        # Each page has ~15 items.
        start_page = (len(processed_links) // 15) + 1
        if start_page > 1:
            jump_url = f"https://findauction.in/bank-property/chennai/all/all/{start_page}"
            print(f"Jumping to page {start_page}: {jump_url}")
            driver.get(jump_url)
            page_num = start_page
        else:
            page_num = 1

        while True:
            # Collect links on current page
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "a.linklist"))
                )
            except:
                print(f"No auction links found on page {page_num}. Ending.")
                break
            
            # Capture Next Page URL early
            next_page_url = None
            try:
                next_btn = driver.find_elements(By.CSS_SELECTOR, "a[aria-label='Next']")
                if next_btn:
                    next_page_url = next_btn[0].get_attribute("href")
            except:
                pass

            elements = driver.find_elements(By.CSS_SELECTOR, "a.linklist")
            links = []
            for el in elements:
                href = el.get_attribute("href")
                if href:
                    links.append(href)
            
            # If all links on this page are already processed, we can skip processing items and just move to next page
            all_processed = all(link in processed_links for link in links)
            
            if not all_processed:
                for link in links:
                    if link in processed_links: 
                        continue
                    
                    try:
                        driver.get(link)
                        details = extract_details(driver)
                        
                        # Download handling
                        pdf_filename = "N/A"
                        try:
                            download_btn = WebDriverWait(driver, 4).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, "#notice_download"))
                            )
                            href = download_btn.get_attribute("href")
                            download_btn.click()
                            time.sleep(2) 
                            pdf_filename = href.split('/')[-1]
                        except:
                            pass
                        
                        details["PDF_Filename"] = pdf_filename
                        save_to_jsonl(details)
                        processed_links.add(link)
                        pbar.update(1)
                        
                    except Exception as e:
                        pbar.write(f"Error scraping {link}")
                        continue
            
            # Pagination
            if next_page_url:
                driver.get(next_page_url)
                page_num += 1
            else:
                break
                
        pbar.close()
        
    except Exception as e:
        print(f"Main Loop Error: {e}")
    finally:
        driver.quit()
        convert_jsonl_to_csv()
        print("\nScraping complete. Final data saved to auction_data.csv")

if __name__ == "__main__":
    run_scraper()
