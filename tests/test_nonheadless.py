import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

def main():
    print("Testing regular Selenium non-headless...")
    chrome_options = Options()
    # Remove headless
    # chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    
    driver.get("https://www.eauctionsindia.com/live-properties")
    print("Waiting 15 seconds to let Cloudflare pass...")
    time.sleep(15)
    
    title = driver.title
    print(f"Page Title: {title}")
    if "Just a moment" in title:
        print("Non-headless failed.")
    else:
        print("Non-headless successfully bypassed Cloudflare!")
        
    driver.quit()

if __name__ == "__main__":
    main()
