
import requests

base = "https://www.eauctionsindia.com/search"

# Try different params
params = [
    f"{base}?page=2",
    f"{base}?pageNumber=2",
    f"{base}?p=2",
    f"{base}?start=12",
    f"{base}?offset=12",
    f"{base}/page/2"
]

print("Base Page 1 Length (Reference)")
r1 = requests.get(base)
len1 = len(r1.text)
print(f"Len: {len1}")

for p in params:
    try:
        r = requests.get(p)
        print(f"Testing: {p} -> Status: {r.status_code} | Len: {len(r.text)}")
        if abs(len(r.text) - len1) > 500: # Significant difference
             print("  -> POSSIBLE MATCH (Content differs)")
        else:
             print("  -> Identical?")
    except: pass
