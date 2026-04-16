
import requests

url = "https://www.eauctionsindia.com/properties/682275"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

try:
    response = requests.get(url, headers=headers, timeout=10)
    print(f"Status: {response.status_code}")
    if "Reserve Price" in response.text:
        print("Success: 'Reserve Price' found in HTML.")
    else:
        print("Failure: Key content not found (might be JS rendered).")
        
    if "Description" in response.text:
        print("Success: 'Description' found.")
    
except Exception as e:
    print(f"Error: {e}")
