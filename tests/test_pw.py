import asyncio
from playwright.async_api import async_playwright

async def main():
    print("Testing playwright...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        print("Navigating...")
        await page.goto("https://www.eauctionsindia.com/live-properties")
        print("Waiting 15 seconds...")
        await asyncio.sleep(15)
        title = await page.title()
        print(f"Page Title: {title}")
        if "Just a moment" in title:
            print("Playwright couldn't bypass Cloudflare.")
        else:
            print("Playwright successfully bypassed Cloudflare!")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
