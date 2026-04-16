
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

def test():
    driver = setup_driver()
    try:
        # Using the URL that supposedly has downloads or structure
        url = "https://www.eauctionsindia.com/properties/681863" 
        # Or try 682498 from the last check which seemed to be valid
        url = "https://www.eauctionsindia.com/properties/682275"
        
        print(f"Testing URL: {url}")
        driver.get(url)
        time.sleep(5)

        print("\n--- Downloads Section Analysis ---")
        # Search for "Downloads" header
        try:
            headers = driver.find_elements(By.XPATH, "//*[contains(text(), 'Downloads')]")
            for h in headers:
                print(f"\nHeader Found: <{h.tag_name}> class='{h.get_attribute('class')}'")
                
                # Print Parent and Next Sibling
                parent = h.find_element(By.XPATH, "..")
                print(f"Parent HTML: {parent.get_attribute('outerHTML')[:1000]}")
                
                try:
                    # If header is in card header, content is in card-body sibling
                    body = parent.find_element(By.XPATH, "following-sibling::div")
                    print(f"Sibling Body HTML: {body.get_attribute('outerHTML')[:1000]}")
                except:
                    print("No following sibling div found.")
        except Exception as e:
            print(f"Header search error: {e}")

        # Search for "Sale Notice" specific text
        print("\n--- 'Sale Notice' Text Search ---")
        elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'Sale Notice')]")
        for el in elements:
            print(f"Element: <{el.tag_name}> Text: '{el.text}'")
            print(f"Outer HTML: {el.get_attribute('outerHTML')[:500]}")
            # Check for parent link?
            try:
                parent_link = el.find_element(By.XPATH, "./ancestor::a")
                print(f"  Warning: wrapped in <a>: {parent_link.get_attribute('href')}")
            except:
                # Check for child link?
                links = el.find_elements(By.TAG_NAME, "a")
                for l in links:
                    print(f"  Child Link: {l.get_attribute('href')}")
                
                # Check for sibling link?
                try:
                    sibling = el.find_element(By.XPATH, "following-sibling::a")
                    print(f"  Sibling Link: {sibling.get_attribute('href')}")
                except: pass

    except Exception as e:
        print(f"Error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    test()
