
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
        driver = uc.Chrome(options=options, use_subprocess=True,
                           version_main=installed_chrome_major())
    return driver


def installed_chrome_major():
    """Major version of the installed Chrome, so the driver matches it.

    A hard-coded version broke the weekly run every time Chrome auto-updated.
    CHROME_VERSION_MAIN overrides the lookup. Returns None when Chrome can't be
    found, which lets undetected-chromedriver detect the version itself.
    """
    override = os.environ.get("CHROME_VERSION_MAIN", "").strip()
    if override.isdigit():
        return int(override)
    if os.name == "nt":
        # Read the version stamped on chrome.exe itself. The install folder can
        # also hold a staged, not-yet-active update (new_chrome.exe), and the
        # registry value below only updates when Chrome is next opened.
        for base in (os.environ.get("ProgramFiles"),
                     os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            exe = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            major = _windows_file_major_version(exe)
            if major:
                return major
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r"Software\Google\Chrome\BLBeacon") as key:
                        version, _ = winreg.QueryValueEx(key, "version")
                    return int(str(version).split(".")[0])
                except (OSError, ValueError):
                    continue
        except ImportError:
            pass
    for cmd in ("google-chrome", "chromium", "chrome"):
        try:
            out = subprocess.run([cmd, "--version"], capture_output=True,
                                 text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"(\d+)\.\d+", out)
        if m:
            return int(m.group(1))
    return None


def _windows_file_major_version(path):
    """Major part of a Windows .exe's file version, or None."""
    if not os.path.isfile(path):
        return None
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None
        info = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(info), ctypes.byref(length)):
            return None
        # VS_FIXEDFILEINFO: dwFileVersionMS is the 3rd DWORD; its high word is the major.
        file_version_ms = ctypes.cast(info, ctypes.POINTER(wintypes.DWORD))[2]
        return (file_version_ms >> 16) or None
    except (OSError, AttributeError):
        return None

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
