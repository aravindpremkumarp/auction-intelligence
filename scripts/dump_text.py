
import requests
from bs4 import BeautifulSoup

url = "https://www.eauctionsindia.com/live-properties"
headers = {"User-Agent": "Mozilla/5.0"}

try:
    r = requests.get(url, headers=headers)
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # Print distinct text lines
    text = soup.get_text(separator='\n', strip=True)
    lines = text.split('\n')
    
    print(f"Total Lines: {len(lines)}")
    for i, line in enumerate(lines):
        if i < 100: # First 100 lines usually contain header stats
            print(f"{i}: {line}")
            
    # Search for "17" (part of 17869)
    print("\n--- Search for '17' ---")
    for line in lines:
        if "17" in line and len(line) < 50:
            print(f"Match: {line}")

except Exception as e:
    print(e)
