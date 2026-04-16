import cloudscraper
from bs4 import BeautifulSoup

def main():
    print("Testing cloudscraper...")
    scraper = cloudscraper.create_scraper()
    response = scraper.get("https://www.eauctionsindia.com/live-properties", timeout=15)
    print(f"Status Code: {response.status_code}")
    
    soup = BeautifulSoup(response.text, 'html.parser')
    title = soup.title.string if soup.title else "No Title"
    print(f"Page Title: {title}")
    
    if "Just a moment" in title:
        print("Cloudscraper couldn't bypass Cloudflare.")
    else:
        print("Cloudscraper successfully bypassed Cloudflare!")

if __name__ == "__main__":
    main()
