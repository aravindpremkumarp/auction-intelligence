
import requests
from bs4 import BeautifulSoup

def get_links(page):
    url = f"https://www.eauctionsindia.com/live-properties?page={page}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(r.text, 'html.parser')
    links = []
    for a in soup.find_all("a", href=True):
        if "/properties/" in a['href']:
            links.append(a['href'])
    return sorted(list(set(links)))

p1 = get_links(1)
p6 = get_links(6)

print(f"Page 1 Links: {len(p1)}")
print(f"Page 6 Links: {len(p6)}")

if p1 == p6:
    print("FATAL: Page 1 and Page 6 are IDENTICAL.")
else:
    print("Pages are different.")
    print(f"P1 First: {p1[0]}")
    print(f"P6 First: {p6[0]}")
