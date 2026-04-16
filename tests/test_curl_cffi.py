from curl_cffi import requests
from bs4 import BeautifulSoup

def main():
    print("Testing curl_cffi...")
    # impersonate Chrome 120
    response = requests.get("https://www.eauctionsindia.com/live-properties", impersonate="chrome120")
    print(f"Status Code: {response.status_code}")
    
    soup = BeautifulSoup(response.text, 'html.parser')
    title = soup.title.string if soup.title else "No Title"
    print(f"Page Title: {title}")
    
    if "Just a moment" in title:
        print("curl_cffi couldn't bypass Cloudflare.")
    else:
        print("curl_cffi successfully bypassed Cloudflare!")

if __name__ == "__main__":
    main()
