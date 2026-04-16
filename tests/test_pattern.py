
import requests

url = "https://www.eauctionsindia.com/search/2"
print(f"Testing {url}")
r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
print(f"Status: {r.status_code}")
print(f"Length: {len(r.text)}")

# Check against page 1
r1 = requests.get("https://www.eauctionsindia.com/search")
if len(r.text) == len(r1.text):
    print("Identical to Page 1 :(")
else:
    print("DIFFERENT from Page 1! :)")
