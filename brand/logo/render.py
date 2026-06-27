import glob, re
from playwright.sync_api import sync_playwright

cands = (glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")
         + glob.glob("/opt/pw-browsers/chromium/chrome-linux/chrome"))
exe = cands[0]

FONT = '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">'

def html(body, bg="#ffffff"):
    return f'<!doctype html><html><head><meta charset="utf-8">{FONT}<style>html,body{{margin:0;padding:0;background:{bg}}}*{{box-sizing:border-box}}</style></head><body>{body}</body></html>'

def responsive(svg):
    # strip fixed width/height so the SVG scales to its container
    return re.sub(r'<svg ([^>]*?)width="\d+" height="\d+"', r'<svg \1', svg, count=1)

ICON = open("brand/logo/icon.svg").read()
WORD = open("brand/logo/wordmark.svg").read()
WORD_DARK = WORD.replace('fill="#0a0b0d"', 'fill="#ffffff"')
ICON_R, WORD_R, WORD_DARK_R = map(responsive, (ICON, WORD, WORD_DARK))

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=exe)
    page = b.new_page(device_scale_factor=2)

    def svg_png(svg, out, omit=True, bg="#ffffff"):
        page.set_content(html(svg, bg)); page.wait_for_timeout(500)
        page.query_selector("svg").screenshot(path=out, omit_background=omit)
        print("rendered", out)

    # Core vector exports
    svg_png(ICON, "brand/logo/icon-1024.png")
    svg_png(WORD, "brand/logo/wordmark-light.png")
    svg_png(WORD_DARK, "brand/logo/wordmark-dark.png", omit=False, bg="#0a0b0d")

    # LinkedIn profile/company avatar 400x400, centered tile with padding
    avatar = f'<div style="width:400px;height:400px;background:#fff;display:flex;align-items:center;justify-content:center"><div style="width:400px;height:400px">{ICON_R}</div></div>'
    page.set_viewport_size({"width":400,"height":400})
    page.set_content(html(avatar)); page.wait_for_timeout(500)
    page.screenshot(path="brand/logo/linkedin-avatar-400.png", clip={"x":0,"y":0,"width":400,"height":400})
    print("rendered linkedin-avatar-400.png")

    # LinkedIn personal banner 1584x396
    banner = f'''<div style="width:1584px;height:396px;position:relative;overflow:hidden;
      background:radial-gradient(120% 160% at 12% 18%,#1e6bff 0%,#0052ff 42%,#0040cc 100%);font-family:Inter,sans-serif;color:#fff">
      <div style="position:absolute;right:-60px;top:-40px;width:520px;height:520px;opacity:.10">{ICON_R}</div>
      <div style="position:absolute;left:88px;top:0;height:100%;display:flex;flex-direction:column;justify-content:center;gap:18px">
        <div style="display:flex;align-items:center;gap:26px">
          <div style="width:104px;height:104px;filter:drop-shadow(0 8px 22px rgba(0,0,0,.28))">{ICON_R}</div>
          <div style="font-size:62px;font-weight:800;letter-spacing:-2px">AuctionScope</div>
        </div>
        <div style="font-size:27px;font-weight:500;color:#e8f0ff;max-width:1000px;letter-spacing:-.2px">
          AI intelligence for India's bank-auction property market.</div>
        <div style="font-size:20px;font-weight:600;color:#bcd2ff;letter-spacing:.3px">auctionscope.in</div>
      </div>
    </div>'''
    page.set_viewport_size({"width":1584,"height":396})
    page.set_content(html(banner)); page.wait_for_timeout(500)
    page.screenshot(path="brand/logo/linkedin-banner-1584x396.png", clip={"x":0,"y":0,"width":1584,"height":396})
    print("rendered linkedin-banner-1584x396.png")

    # Contact sheet (responsive embeds)
    sheet = f'''<div style="font-family:Inter,sans-serif;padding:56px;display:flex;flex-direction:column;gap:44px;background:#f6f7f9">
      <div style="display:flex;gap:44px;align-items:flex-end">
        <div style="width:160px;height:160px">{ICON_R}</div>
        <div style="width:96px;height:96px">{ICON_R}</div>
        <div style="width:56px;height:56px">{ICON_R}</div>
        <div style="width:36px;height:36px">{ICON_R}</div>
        <span style="color:#5b616e;font-size:17px;margin-bottom:6px">avatar legibility @ 160 / 96 / 56 / 36 px</span>
      </div>
      <div style="background:#fff;border:1px solid #dee0e4;border-radius:18px;padding:46px"><div style="width:760px">{WORD_R}</div></div>
      <div style="background:#0a0b0d;border-radius:18px;padding:46px"><div style="width:760px">{WORD_DARK_R}</div></div>
    </div>'''
    page.set_viewport_size({"width":1080,"height":900})
    page.set_content(html(sheet,"#f6f7f9")); page.wait_for_timeout(500)
    page.screenshot(path="/tmp/contact_sheet.png", full_page=True)
    print("rendered contact_sheet.png")

    b.close()
