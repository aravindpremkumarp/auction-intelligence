
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service
import time

def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

def verify_fix():
    driver = setup_driver()
    try:
        url = "https://www.eauctionsindia.com/properties/682275"
        print(f"Verifying Fix on: {url}")
        driver.get(url)
        time.sleep(5)
        
        # Exact Logic from Scraper
        filenames = []
        download_links_found = set()
        
        # Strategy A: Look for "Downloads" container
        try:
            print("Attempting Strategy A (Downloads Header)...")
            dl_header = driver.find_element(By.XPATH, "//*[contains(text(), 'Downloads')]")
            print("  Found Header.")
            parent = dl_header.find_element(By.XPATH, "./..") 
            content_div = parent.find_element(By.XPATH, "following-sibling::div")
            print("  Found Content Div.")
            
            container_links = content_div.find_elements(By.TAG_NAME, "a")
            print(f"  Found {len(container_links)} links in container.")
            
            for cl in container_links:
                href = cl.get_attribute("href")
                print(f"    Link: {href}")
                if href and href not in download_links_found:
                    download_links_found.add(href)
        except Exception as e:
             print(f"  Strategy A Failed: {e}")

        # Strategy B
        print("Attempting Strategy B (Global Search)...")
        try:
            all_links = driver.find_elements(By.TAG_NAME, "a")
            for link in all_links:
                try:
                    href = link.get_attribute("href")
                    if not href: continue
                    text = link.text.lower()
                    
                    if ".pdf" in href.lower() or "storage/properties" in href.lower() or "sale notice" in text:
                         print(f"    Global Match: {text} -> {href}")
                except: continue
        except: pass

    except Exception as e:
        print(f"Error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    verify_fix()
