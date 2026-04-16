import httpx
from bs4 import BeautifulSoup

def main():
    print("Testing httpx with HTTP/2...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    
    with httpx.Client(http2=True, headers=headers, follow_redirects=True) as client:
        response = client.get("https://www.eauctionsindia.com/live-properties")
        print(f"Status Code: {response.status_code}")
        soup = BeautifulSoup(response.text, 'html.parser')
        title = soup.title.string if soup.title else "No Title"
        print(f"Page Title: {title}")
        if "Just a moment" in title:
            print("httpx couldn't bypass Cloudflare.")
        else:
            print("httpx bypassed Cloudflare!")

if __name__ == "__main__":
    main()
