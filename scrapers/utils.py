
import os
import re
import time
import threading
import subprocess
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# The site renders the property description and the structured location fields
# (Province/State, City/Town, Area/Town) inside the same container, so grabbing
# the container's text glues those field labels onto the end of the description,
# e.g. "…land and buildingProvince/State :Tamil NaduCity/Town :Ranipet…".
# Cut the description at the first such label. These slashed labels never appear
# in genuine property prose, so this is safe.
# NOTE: mirrored by api/review/markdown_match.strip_field_bleed — keep in sync.
_FIELD_BLEED = re.compile(r"(?:Province/State|City/Town|Area/Town)")

# Lock to prevent parallel workers from patching chromedriver simultaneously
_uc_lock = threading.Lock()


def strip_field_bleed(text):
    """Remove the glued-on location fields from the end of a scraped description."""
    if not text:
        return text or ""
    m = _FIELD_BLEED.search(text)
    if not m:
        return text
    return text[: m.start()].rstrip(" ,;:-\n\t")


def setup_driver(download_dir=None, headless=False):
    """
    Sets up an undetected Chrome WebDriver that bypasses Cloudflare bot detection.
    Uses undetected-chromedriver to patch Chrome and avoid WebDriver fingerprinting.
    """
    if download_dir is None:
         download_dir = os.path.join(os.getcwd(), "downloads")
    
    if not os.path.exists(download_dir):
        os.makedirs(download_dir)

    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    
    # Set preferences for automatic downloads
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "plugins.always_open_pdf_externally": True,
        "profile.default_content_settings.popups": 0
    }
    options.add_experimental_option("prefs", prefs)

    # Thread-safe driver creation — UC patches the chromedriver binary,
    # so parallel workers must not race each other during patching.
    with _uc_lock:
        driver = uc.Chrome(options=options, use_subprocess=True, version_main=149)
    return driver

def notify_cloudflare_block():
    """
    Fire a one-time Windows notification the moment a Cloudflare challenge is
    detected, so a human physically at the machine notices even if nobody is
    watching the console (e.g. a Task-Scheduler-launched unattended run).

    Dependency-free: shells out to PowerShell's built-in Windows Forms message
    box. Fired via Popen (non-blocking) so it doesn't hold up the existing
    cf_wait() polling loop — the caller keeps polling driver.title/current_url
    in parallel while this dialog sits on screen for the human to see.
    """
    try:
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[System.Windows.Forms.MessageBox]::Show("
            "'Cloudflare challenge detected in the scraper Chrome window. "
            "Please solve the CAPTCHA manually — the script is waiting and "
            "will resume automatically once it is cleared.', "
            "'Auction scraper: action needed', "
            "[System.Windows.Forms.MessageBoxButtons]::OK, "
            "[System.Windows.Forms.MessageBoxIcon]::Warning)"
        )
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        # Never let a notification failure break the scrape itself.
        pass


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
