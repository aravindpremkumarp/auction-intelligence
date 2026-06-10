
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service
import time

def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")  # Run properly in background
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--window-size=1920,1080")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

def explore():
    driver = setup_driver()
    try:
        url = "https://www.eauctionsindia.com/properties/681863"
        print(f"Visiting Detail Page: {url}")
        driver.get(url)
        time.sleep(5)
        
        print(f"Title: {driver.title}")
        driver.save_screenshot("eauctions_detail_full.png")
        
        # Dump the main content area
        # Try to find the nearest container with "Reserve Price"
        keyword = "Reserve Price"
        try:
            el = driver.find_element(By.XPATH, f"//*[contains(text(), '{keyword}')]")
            parent = el.find_element(By.XPATH, "./../../../../..") # Go up to main table/div
            print("\n--- Detail Content HTML ---")
            print(parent.get_attribute('outerHTML')[:4000]) # Print a good chunk
        except Exception as e:
            print(f"Could not find keyword container: {e}")
            print("\n--- Body HTML Start ---")
            print(driver.find_element(By.TAG_NAME, "body").get_attribute('innerHTML')[:2000])

        # Check for downloads
        print("\n--- Download Links ---")
        links = driver.find_elements(By.TAG_NAME, "a")
        for link in links:
            href = link.get_attribute("href")
            text = link.text
            if href and (".pdf" in href or "download" in text.lower()):
                print(f"Candidate: {text} -> {href}")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    explore()
