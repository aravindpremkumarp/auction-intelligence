
import requests
from bs4 import BeautifulSoup

url = "https://www.eauctionsindia.com/properties/674482"
headers = {
    "User-Agent": "Mozilla/5.0"
}

r = requests.get(url, headers=headers)
soup = BeautifulSoup(r.text, 'html.parser')

print(f"URL: {url}")
print("-" * 20)

# Check logic
# desc_header = soup.find(lambda tag: tag.name in ['h5', 'h4', 'div'] and "Description" in tag.get_text()) # This might match the Body too if it contains the word "Description"?

# More specific find
headers = soup.find_all(['h5', 'h4', 'strong', 'div'])
for h in headers:
    txt = h.get_text(strip=True)
    if "Description" == txt or "Description" in txt:
        if len(txt) > 20: continue # Skip long text blocks containing "Description"
        
        print(f"Found Header Candidate: <{h.name} class='{h.get('class')}'>: '{txt}'")
        parent = h.parent
        print(f"  Parent: <{parent.name} class='{parent.get('class')}'>")
        
        sibling = parent.find_next_sibling("div")
        if sibling:
            print(f"  Next Sibling Div: <{sibling.name} class='{sibling.get('class')}'>")
            print(f"  Content Preview: {sibling.get_text(strip=True)[:100]}...")
        else:
            print("  No next sibling div.")
        print("-" * 10)
