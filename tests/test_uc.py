import time
import undetected_chromedriver as uc
from bs4 import BeautifulSoup

def main():
    print("Testing undetected-chromedriver...")
    options = uc.ChromeOptions()
    options.headless = True
    options.add_argument('--headless=new')
    
    driver = uc.Chrome(options=options)
    
    try:
        driver.get("https://www.eauctionsindia.com/live-properties")
        time.sleep(10) # Wait for CF challenge
        
        driver.save_screenshot("uc_test.png")
        page_source = driver.page_source
        
        soup = BeautifulSoup(page_source, 'html.parser')
        title = soup.title.string if soup.title else "No Title"
        print(f"Page Title: {title}")
        
        if "Just a moment" in title:
            print("uc couldn't bypass Cloudflare in headless mode.")
        else:
            print("uc successfully bypassed Cloudflare!")
            
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
