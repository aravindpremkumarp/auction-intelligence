
import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

def setup_driver(download_dir=None, headless=False):
    """
    Sets up the Chrome WebDriver with headless options and a specified download directory.
    """
    if download_dir is None:
         download_dir = os.path.join(os.getcwd(), "downloads")
    
    if not os.path.exists(download_dir):
        os.makedirs(download_dir)

    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new") # Run in headless mode
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    
    # Set preferences for automatic downloads
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "plugins.always_open_pdf_externally": True,
        "profile.default_content_settings.popups": 0
    }
    chrome_options.add_experimental_option("prefs", prefs)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

def login(driver, email, password):
    """
    Logs into findauction.in using provided credentials.
    """
    try:
        print("Navigating to login page...")
        driver.get("https://findauction.in/login")
        
        print("Entering credentials...")
        email_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#email"))
        )
        password_input = driver.find_element(By.CSS_SELECTOR, "#password")
        login_btn = driver.find_element(By.CSS_SELECTOR, "button.btn-1.flat-btn")

        email_input.clear()
        email_input.send_keys(email)
        password_input.clear()
        password_input.send_keys(password)
        
        login_btn.click()
        
        # simple check for login success - wait for url change or check for a specific element
        # The site redirects to home or dashboard usually.
        time.sleep(3) 
        print(f"Login submitted. Current URL: {driver.current_url}")
        
        if "login" in driver.current_url:
            print("Warning: Might still be on login page. Check credentials.")
            return False
        return True

    except Exception as e:
        print(f"Login failed: {e}")
        return False
