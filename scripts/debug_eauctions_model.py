
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service
import time
import json

def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

def extract_model():
    driver = setup_driver()
    try:
        # User reported missing info. Let's look at the same URL again.
        url = "https://www.eauctionsindia.com/properties/681863"
        print(f"Scraping Data Model from: {url}")
        driver.get(url)
        time.sleep(5)

        # 1. Debug Downloads
        print("\n--- LINK ANALYSIS ---")
        links = driver.find_elements(By.TAG_NAME, "a")
        for i, link in enumerate(links):
            text = link.text.strip()
            href = link.get_attribute("href")
            # print all links that might be relevant
            if text or href:
                if "download" in text.lower() or "notice" in text.lower() or ".pdf" in str(href):
                    print(f"Potential Download: '{text}' -> {href}")
                # Also print surrounding text of links?

        # 2. Debug Description
        # Look for the word "Description" on the page
        print("\n--- DESCRIPTION ANALYSIS ---")
        try:
            # Case 1: Header "Description"
            headers = driver.find_elements(By.XPATH, "//*[contains(text(), 'Description')]")
            for h in headers:
                print(f"Found 'Description' text in: <{h.tag_name}> class='{h.get_attribute('class')}'")
                # Print next sibling?
                try:
                    sibling = h.find_element(By.XPATH, "following-sibling::*")
                    print(f"  -> Next Sibling: <{sibling.tag_name}>: {sibling.text[:100]}...")
                except:
                    print("  -> No sibling.")
                
                # Print Parent content?
                parent = h.find_element(By.XPATH, "..")
                print(f"  -> Parent text: {parent.text[:100]}...")
                
        except Exception as e:
            print(f"Desc error: {e}")
            
        # Case 2: Just dump all paragraphs?
        ps = driver.find_elements(By.TAG_NAME, "p")
        print(f"\nFound {len(ps)} paragraphs. Sample:")
        for p in ps[:10]:
            if len(p.text) > 50:
                print(f"  P: {p.text[:100]}...")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    extract_model()
