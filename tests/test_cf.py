import time
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scrapers'))
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import utils

def main():
    print("Testing Selenium access...")
    driver = utils.setup_driver()
    driver.set_window_size(1920, 1080)
    driver.get("https://www.eauctionsindia.com/live-properties")
    time.sleep(10)
    
    driver.save_screenshot("cf_test.png")
    with open("cf_test.html", "w", encoding="utf-8") as f:
        f.write(driver.page_source)
        
    print(f"Title: {driver.title}")
    driver.quit()

if __name__ == "__main__":
    main()
