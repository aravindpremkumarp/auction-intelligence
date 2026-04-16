
import requests
from bs4 import BeautifulSoup
import re

url = "https://www.eauctionsindia.com/live-properties"
headers = {
    "User-Agent": "Mozilla/5.0"
}

try:
    r = requests.get(url, headers=headers)
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # Common patterns for counts
    # "Showing 1-X of Y Results" or just "Y Properties found"
    
    # 1. Search for numbers in text
    # Look for text containing "Found" or "Properties"
    # candidates = soup.find_all(text=re.compile(r'properties|found', re.I))
    
    # Specific known pattern in this site often:
    # <div class="col-md-6 ..."> <h4> Total Properties Found : <span>17869</span> </h4> </div>
    
    h4s = soup.find_all("h4")
    for h in h4s:
        print(f"H4: {h.get_text(strip=True)}")
        
    spans = soup.find_all("span")
    for s in spans:
        if s.get_text().isdigit() and len(s.get_text()) > 3:
            print(f"Span Digit candidate: {s.get_text()} | Parent: {s.parent.name} text='{s.parent.get_text(strip=True)}'")
        
except Exception as e:
    print(e)
