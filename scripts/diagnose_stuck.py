
import requests
from bs4 import BeautifulSoup

def analyze_page(page):
    url = f"https://www.eauctionsindia.com/search?page={page}"
    print(f"--- Analyzing Page {page}: {url} ---")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        print(f"Status: {r.status_code}")
        
        soup = BeautifulSoup(r.text, 'html.parser')
        links = set()
        for a in soup.find_all("a", href=True):
            if "/properties/" in a['href']:
                links.add(a['href'])
        
        print(f"Links found: {len(links)}")
        if len(links) > 0:
            print(f"Sample Link: {list(links)[0]}")
        return links
    except Exception as e:
        print(f"Error: {e}")
        return set()

p1 = analyze_page(1)
p2 = analyze_page(2)

if p1 and p2:
    if p1 == p2:
        print("\nFATAL: Page 1 and Page 2 are IDENTICAL.")
    else:
        new = p2 - p1
        print(f"\nUnique links on Page 2: {len(new)}")
        if len(new) == 0:
             print("Page 2 has links, but they are all repeats of Page 1?")
        else:
             print("Pagination seems working.")
