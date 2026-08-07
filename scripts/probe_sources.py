"""
probe_sources.py — reachability + shape probe for candidate auction sources.

Answers the three questions that decide whether a source is worth an adapter:

  1. Can we reach it at all, and does it challenge us? (Cloudflare / WAF)
  2. Is the listing server-rendered, or is it a JS app we'd need Selenium for?
  3. Does it expose sale-notice PDFs — the artifact the whole pipeline is fed by?

Run this LOCALLY. A cloud/datacenter IP gets challenged far more aggressively
than a residential one, so results from CI or a sandbox are pessimistic and
not representative. See docs/auction-source-expansion-2026-08.md.

Stdlib only — no venv, no install, works on a bare Python 3.10+.

Usage:
    python scripts/probe_sources.py                    # probe all sources
    python scripts/probe_sources.py baanknet ibapi     # probe a subset
    python scripts/probe_sources.py --json out.json    # also dump raw results
    python scripts/probe_sources.py --timeout 45
"""

import argparse
import gzip
import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ── Candidate sources ────────────────────────────────────────────────────────
# tier:  official  — statutory / government-sanctioned disclosure channel
#        asp       — the platform the auction actually executes on
#        issuer    — the bank / ARC that owns the asset
#        current   — what we scrape today, probed as the control
SOURCES = [
    ("baanknet",       "BAANKNET (PSB Alliance)",   "https://baanknet.com/",              "official"),
    ("ibapi",          "IBAPI",                     "https://ibapi.in/",                  "official"),
    ("ibbi",           "IBBI liquidation notices",  "https://ibbi.gov.in/",               "official"),
    ("bankeauctions",  "bankeauctions.com (C1)",    "https://www.bankeauctions.com/",     "asp"),
    ("auctiontiger",   "AuctionTiger (eProc Tech)", "https://www.auctiontiger.net/",      "asp"),
    ("mstc",           "MSTC e-commerce",           "https://www.mstcecommerce.com/",     "asp"),
    ("eauctionsindia", "eauctionsindia (current)",  "https://www.eauctionsindia.com/",    "current"),
]

# Hrefs that plausibly lead to a listing/search page worth probing next.
LISTING_HINT = re.compile(
    r"(auction|propert|e-?auction|listing|search|asset|sale-?notice)", re.I
)
# ...minus the static files and blog posts those keywords also match
# ("/assets/css/carousel.css" is not a listing page).
LISTING_NOISE = re.compile(
    r"\.(css|js|png|jpe?g|gif|svg|ico|woff2?|ttf|eot)$|/(assets|static|dist)/|/blog/",
    re.I,
)

# Markers that mean "a bot wall answered, not the site".
CHALLENGE_MARKERS = [
    ("cloudflare", re.compile(r"just a moment|cf-chl|challenge-platform|cf_chl_opt|attention required", re.I)),
    ("akamai",     re.compile(r"akamai|reference #\d+\.\w+", re.I)),
    ("incapsula",  re.compile(r"incapsula|_incap_|imperva", re.I)),
    ("captcha",    re.compile(r"recaptcha|hcaptcha|g-recaptcha", re.I)),
]

# Frameworks whose presence with near-zero links means client-side rendering.
SPA_MARKERS = re.compile(
    r'id="root"|id="app"|ng-app|data-reactroot|__NEXT_DATA__|__NUXT__', re.I
)


class Result(dict):
    """Plain dict with attribute-ish access for readability."""


def fetch(url: str, timeout: int):
    """
    GET a URL following redirects. Returns (result_dict, body_text).

    Never raises — transport failures land in result["error"] so one dead host
    doesn't abort the sweep.
    """
    res = Result(
        url=url, final_url=url, status=None, bytes=0, elapsed_ms=None,
        server=None, error=None,
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept-Encoding": "gzip",
            "Connection": "close",
        },
    )
    started = time.time()
    try:
        # Some Indian government hosts still negotiate weak/legacy TLS; probing
        # is read-only reconnaissance, so a permissive context here only affects
        # what we can *see*, never what we trust or ingest.
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            res["status"] = resp.status
            res["final_url"] = resp.url
            res["server"] = resp.headers.get("Server")
            res["cf_ray"] = resp.headers.get("CF-RAY")
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        res["status"] = e.code
        res["final_url"] = e.url or url
        res["server"] = e.headers.get("Server") if e.headers else None
        res["cf_ray"] = e.headers.get("CF-RAY") if e.headers else None
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as e:
        reason = getattr(e, "reason", e)
        res["error"] = f"{type(e).__name__}: {reason}"
        res["elapsed_ms"] = int((time.time() - started) * 1000)
        return res, ""

    res["elapsed_ms"] = int((time.time() - started) * 1000)
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    res["bytes"] = len(raw)
    return res, raw.decode("utf-8", errors="replace")


def analyse(res: Result, body: str) -> Result:
    """Fill in protection / render-mode / PDF / listing-path findings."""
    if res["error"]:
        # Nothing came back — don't let an empty body masquerade as a finding.
        res.update(challenge=None, links=0, pdf_links=0, render="n/a",
                   listing_candidates=[])
        return res

    res["challenge"] = None
    for name, pat in CHALLENGE_MARKERS:
        if pat.search(body):
            res["challenge"] = name
            break
    if res["challenge"] is None and res.get("cf_ray") and res["status"] in (403, 503):
        res["challenge"] = "cloudflare"

    hrefs = re.findall(r'href=["\']([^"\']+)["\']', body, re.I)
    res["links"] = len(hrefs)
    res["pdf_links"] = sum(1 for h in hrefs if ".pdf" in h.lower())

    # Render mode: an SPA shell ships framework markers but almost no anchors.
    if res["challenge"]:
        res["render"] = "blocked"
    elif len(hrefs) < 10 and SPA_MARKERS.search(body):
        res["render"] = "client-side (needs a browser)"
    elif len(hrefs) < 10:
        res["render"] = "thin (redirect stub or empty shell?)"
    else:
        res["render"] = "server-rendered"

    # Candidate listing paths, deduped, same-host only — saves guessing URLs.
    base = res["final_url"]
    host = urlparse(base).netloc
    seen, candidates = set(), []
    for h in hrefs:
        if h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base, h)
        parsed = urlparse(absolute)
        path = parsed.path or ""
        if parsed.netloc != host or not LISTING_HINT.search(path):
            continue
        if LISTING_NOISE.search(path):
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if clean in seen:
            continue
        seen.add(clean)
        candidates.append(clean)
    res["listing_candidates"] = candidates[:8]
    return res


def verdict(res: Result) -> str:
    """One-line read on whether this source is cheap, costly, or unknown."""
    if res["error"]:
        return "UNREACHABLE — network/DNS/TLS (re-run locally before believing it)"
    if res["challenge"]:
        return f"CHALLENGED by {res['challenge']} — needs a real browser or a deal"
    if res["status"] and res["status"] >= 400:
        return f"HTTP {res['status']} — wrong entry path, try a listing URL directly"
    if res["render"].startswith("server-rendered"):
        return "CHEAP — plain HTTP + parser, CI-runnable, no Selenium"
    if res["render"].startswith("client-side"):
        return "COSTLY — client-rendered, needs a browser (or find its JSON API)"
    return "INCONCLUSIVE — inspect by hand"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("keys", nargs="*", help="source keys to probe (default: all)")
    ap.add_argument("--timeout", type=int, default=30, help="per-request seconds")
    ap.add_argument("--json", metavar="PATH", help="write raw results as JSON")
    args = ap.parse_args()

    wanted = set(args.keys)
    targets = [s for s in SOURCES if not wanted or s[0] in wanted]
    if wanted:
        unknown = wanted - {s[0] for s in SOURCES}
        if unknown:
            print(f"Unknown source key(s): {', '.join(sorted(unknown))}")
            print(f"Known: {', '.join(s[0] for s in SOURCES)}")
            return 2

    print(f"Probing {len(targets)} source(s) — run this from a residential "
          f"connection, not CI.\n")

    results = []
    for key, name, url, tier in targets:
        res, body = fetch(url, args.timeout)
        analyse(res, body)
        res["key"], res["name"], res["tier"] = key, name, tier
        res["verdict"] = verdict(res)
        results.append(res)

        status = res["error"] or f"HTTP {res['status']}"
        print(f"── {name}  [{tier}]")
        print(f"   {url}")
        print(f"   {status}   {res['bytes']:,} bytes   {res['elapsed_ms']} ms"
              f"   server={res['server'] or '?'}")
        if not res["error"]:
            if res["final_url"] != url:
                print(f"   redirected → {res['final_url']}")
            print(f"   render={res['render']}   links={res['links']}"
                  f"   pdf_links={res['pdf_links']}")
            for c in res["listing_candidates"]:
                print(f"     ? listing candidate: {c}")
        print(f"   → {res['verdict']}\n")

    print("=" * 72)
    print(f"{'source':<16} {'tier':<9} {'status':<10} {'render':<22} pdfs")
    print("-" * 72)
    for r in results:
        print(f"{r['key']:<16} {r['tier']:<9} "
              f"{(r['error'] and 'ERR') or r['status']:<10} "
              f"{r['render']:<22} {r['pdf_links']}")

    print("\nReminders:")
    print("  • pdf_links on a homepage is a weak signal — re-probe the listing")
    print("    candidates above, that's where sale notices actually hang.")
    print("  • UNREACHABLE from a sandbox usually means egress policy, not a")
    print("    dead host. Only a local run settles it.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"\nRaw results → {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
