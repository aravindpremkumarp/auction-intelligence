
import requests

def check_url(url, label):
    print(f"--- Checking {label}: {url} ---")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        print(f"Status: {r.status_code}")
        print(f"Length: {len(r.text)}")
        count = r.text.count("/properties/")
        print(f"Property Links Found (approx): {count}")
        if count == 0:
            print("  FAIL: No property links in HTML.")
            # Check for redirect
            if r.url != url:
                print(f"  Redirected to: {r.url}")
    except Exception as e:
        print(f"Error: {e}")
    print("\n")

check_url("https://www.eauctionsindia.com/live-properties?page=2", "Live Properties Page 2")
check_url("https://www.eauctionsindia.com/search?page=2", "Search Page 2")
