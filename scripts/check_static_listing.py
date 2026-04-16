
import requests

# Test page 2 of search
url = "https://www.eauctionsindia.com/search?page=2"
headers = {
    "User-Agent": "Mozilla/5.0"
}
try:
    r = requests.get(url, headers=headers, timeout=10)
    print(f"Status: {r.status_code}")
    if "/properties/" in r.text:
       print("Success: Found property links in static HTML.")
    else:
       print("Failure: No property links found (JS required?).")
       
    # Check simple pagination check
    if "page=3" in r.text or "Next" in r.text:
        print("Success: Pagination links found.")
except Exception as e:
    print(e)
