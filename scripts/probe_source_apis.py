"""
probe_source_apis.py — verify the BAANKNET and bankeauctions.com JSON contracts.

`probe_sources.py` answers "is this source reachable and server-rendered?".
This script goes one level deeper for the two sources we actually intend to
adapt, and checks the three things that decide whether an adapter is viable:

  1. Does the search endpoint answer without auth, and can we filter by state
     and page through the whole result set?
  2. Does a detail record carry the fields the normalized schema needs?
  3. Is the **sale notice PDF** reachable, and is it a real PDF? Everything
     downstream (MinerU OCR → extraction → graph) is fed by that file; a source
     without it is a lead list, not a pipeline input.

Findings and the full request/response shapes: docs/source-recon-2026-09.md.

Read-only. No auth, no cookies, no writes. Stdlib only — no venv, no install.

Usage:
    python scripts/probe_source_apis.py                  # both sources, TN
    python scripts/probe_source_apis.py baanknet         # one source
    python scripts/probe_source_apis.py --pages 5        # deeper live-share sample
    python scripts/probe_source_apis.py --json out.json
"""

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Tamil Nadu's id differs per source and neither matches the other's — BAANKNET
# returns id=31/code=33 from /common/states?countryId=101, bankeauctions uses
# the <option value> from its own homepage dropdown.
TN_BAANKNET = 31
TN_BANKEAUCTIONS = 24

BAANKNET_API = "https://baanknet.com/api/v1"
BANKEAUCTIONS_DT = "https://bankeauctions.com/home/liveAuctionDatatable/"

# Fields an adapter must be able to fill from one search row, per docs/SCHEMA.md.
BAANKNET_REQUIRED = [
    "propertyDetailId", "auctionId", "bankName", "borrowerName",
    "propertyTitle", "propertyType", "districtName", "stateName",
    "auctionStartTime", "auctionEndTime", "emdEndTime",
]


def _open(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout)


def get_json(url, timeout=40):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    with _open(req, timeout) as resp:
        return json.load(resp)


def post_json(url, body, timeout=40):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with _open(req, timeout) as resp:
        return json.load(resp)


def post_form(url, form, timeout=40):
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with _open(req, timeout) as resp:
        return json.load(resp)


def check_pdf(url, timeout=60):
    """Fetch a candidate notice and confirm it really is a PDF."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with _open(req, timeout) as resp:
            head = resp.read(2048)
            rest = resp.read()
    except Exception as e:  # noqa: BLE001 — a dead link is a finding, not a crash
        return {"url": url, "ok": False, "error": f"{type(e).__name__}: {e}"}
    size = len(head) + len(rest)
    return {
        "url": url,
        "ok": head.startswith(b"%PDF"),
        "bytes": size,
        "magic": head[:8].decode("latin-1"),
    }


def is_live(row, now):
    """A row is live if an auction exists and its window hasn't closed."""
    end = row.get("auctionEndTime")
    if not row.get("isAuctionAvailable") or not end:
        return False
    try:
        return datetime.datetime.fromisoformat(end.replace("Z", "+00:00")) > now
    except ValueError:
        return False


# ── BAANKNET ────────────────────────────────────────────────────────────────
def probe_baanknet(state_id, pages, timeout):
    """
    Contract:
      POST /api/v1/property/detail/property-filter
           {"search": {"stateId": N}, "sort": {"type": "mostrecent"},
            "range": "", "page": P, "limit": L}
      GET  /api/v1/auction/detail/{auctionId}   → auctionDocuments[].url
    """
    out = {"source": "baanknet", "ok": False, "notes": []}
    url = f"{BAANKNET_API}/property/detail/property-filter"

    def page(p, limit=50):
        body = {
            "search": {"stateId": state_id},
            "sort": {"type": "mostrecent"},
            "range": "",
            "page": p,
            "limit": limit,
        }
        return post_json(url, body, timeout)["data"]

    first = page(1)
    out["total"] = first["total"]
    out["total_pages"] = first["totalPages"]
    rows = [r["_source"] for r in first["data"]]
    print(f"   search: total={first['total']:,}  pages={first['totalPages']:,}"
          f"  rows/page={len(first['data'])}")

    missing = [f for f in BAANKNET_REQUIRED if f not in rows[0]]
    out["missing_fields"] = missing
    print(f"   row fields: {len(rows[0])} keys, missing={missing or 'none'}")

    # Live share: sample evenly, because the default sort is most-recent-first
    # and page 1 alone badly over-states how much of the archive is still open.
    now = datetime.datetime.now(datetime.timezone.utc)
    step = max(1, first["totalPages"] // max(1, pages))
    sampled = live = 0
    for p in range(1, first["totalPages"] + 1, step):
        batch = [r["_source"] for r in page(p)["data"]] if p != 1 else rows
        sampled += len(batch)
        live += sum(1 for r in batch if is_live(r, now))
        if sampled >= pages * 50:
            break
    share = live / sampled if sampled else 0
    out["live_sample"] = {"sampled": sampled, "live": live, "share": round(share, 3)}
    out["live_estimate"] = round(first["total"] * share)
    print(f"   live: {live}/{sampled} sampled ({share:.1%})"
          f"  → est. {out['live_estimate']:,} open auctions")

    # Notice PDFs hang off the auction, not the property.
    checked = with_notice = 0
    pdf = None
    for row in rows:
        aid = row.get("auctionId")
        if not aid:
            continue
        try:
            detail = get_json(f"{BAANKNET_API}/auction/detail/{aid}", timeout)["data"]
        except urllib.error.HTTPError as e:
            out["notes"].append(f"auction {aid}: HTTP {e.code}")
            continue
        checked += 1
        docs = detail.get("auctionDocuments") or []
        urls = [d.get("url", "") for d in docs if (d.get("url") or "").lower().endswith(".pdf")]
        if urls:
            with_notice += 1
            pdf = pdf or urls[0]
        if checked >= 10:
            break
    out["notice_coverage"] = {"checked": checked, "with_pdf": with_notice}
    print(f"   notices: {with_notice}/{checked} auctions expose a PDF")

    if pdf:
        out["pdf"] = check_pdf(pdf, timeout)
        print(f"   pdf: {out['pdf']['ok']}  {out['pdf'].get('bytes', 0):,} bytes")

    out["ok"] = bool(rows) and not missing and with_notice > 0
    return out


# ── bankeauctions.com ───────────────────────────────────────────────────────
def probe_bankeauctions(state_id, timeout):
    """
    Contract (legacy DataTables, server-side):
      filters ride the QUERY STRING, paging rides the POST BODY —
      POST /home/liveAuctionDatatable/?state=N
           iDisplayStart=S&iDisplayLength=10&sEcho=1
      detail page: /{category}-{subcategory}-{city}-{col10}  (slugified)
    """
    out = {"source": "bankeauctions", "ok": False, "notes": []}
    url = f"{BANKEAUCTIONS_DT}?state={state_id}"

    first = post_form(url, {"iDisplayStart": 0, "iDisplayLength": 10, "sEcho": 1}, timeout)
    out["total"] = int(first["iTotalRecords"])
    print(f"   search: total={out['total']:,}  rows/page={len(first['aaData'])}")

    # Paging advances, but not cleanly: the page size is pinned to 10 whatever
    # iDisplayLength says, and consecutive pages share a row — the server's
    # offset is off by one. So a sweep must dedupe by id, and iTotalRecords
    # over-counts the rows you can actually reach.
    second = post_form(url, {"iDisplayStart": 10, "iDisplayLength": 10, "sEcho": 1}, timeout)
    ids1 = [r[1] for r in first["aaData"]]
    ids2 = [r[1] for r in second["aaData"]]
    new = len(set(ids2) - set(ids1))
    out["page_size"] = len(ids1)
    out["page2_new_rows"] = new
    out["page_overlap"] = len(set(ids1) & set(ids2))
    out["pagination_ok"] = new >= len(ids2) - 2
    print(f"   paging: page size={len(ids1)}  page2 new rows={new}"
          f"  overlap={out['page_overlap']}")

    # Reported total vs. rows actually reachable — the gap is the off-by-one
    # above, and it is what an adapter's row count has to be judged against.
    if out["total"] <= 500:
        seen = set()
        for start in range(0, out["total"], 10):
            batch = post_form(
                url, {"iDisplayStart": start, "iDisplayLength": 10, "sEcho": 1}, timeout
            )["aaData"]
            seen.update(r[1] for r in batch)
        out["unique_rows"] = len(seen)
        print(f"   sweep: {len(seen)} unique rows reachable of {out['total']} reported")

    row = first["aaData"][0]
    slug = "-".join(
        [
            row[12].replace(" ", "-").lower(),
            row[13].replace(" ", "-").lower(),
            row[4].replace(" ", "-").replace("(", "").replace(")", "").lower(),
            row[10],
        ]
    )
    detail_url = f"https://bankeauctions.com/{slug}"
    out["detail_url"] = detail_url
    req = urllib.request.Request(detail_url, headers={"User-Agent": UA})
    with _open(req, timeout) as resp:
        html = resp.read().decode("utf-8", "replace")
    print(f"   detail: {detail_url} ({len(html):,} bytes)")

    # Per-auction notices live under /public/uploads/bank/; the site-wide
    # policy PDFs sit at the root and must not be counted as notices.
    hrefs = re.findall(r'href="([^"]*?/public/uploads/bank/[^"]+\.pdf)"', html)
    out["notice_links"] = len(hrefs)
    print(f"   notices: {len(hrefs)} PDF link(s) on the detail page")
    if hrefs:
        href = hrefs[0]
        if href.startswith("/"):
            href = "https://bankeauctions.com" + href
        out["pdf"] = check_pdf(href, timeout)
        print(f"   pdf: {out['pdf']['ok']}  {out['pdf'].get('bytes', 0):,} bytes")

    out["ok"] = out["pagination_ok"] and bool(hrefs) and out.get("pdf", {}).get("ok", False)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sources", nargs="*", default=[],
                    help="baanknet and/or bankeauctions (default: both)")
    ap.add_argument("--pages", type=int, default=8,
                    help="pages of 50 to sample for BAANKNET's live share")
    ap.add_argument("--timeout", type=int, default=40)
    ap.add_argument("--json", help="also dump raw results here")
    args = ap.parse_args()

    wanted = set(args.sources) or {"baanknet", "bankeauctions"}
    results = []

    if "baanknet" in wanted:
        print("── BAANKNET (baanknet.com) — official, PSU banks")
        try:
            results.append(probe_baanknet(TN_BAANKNET, args.pages, args.timeout))
        except Exception as e:  # noqa: BLE001 — report, don't abort the sweep
            print(f"   FAILED: {type(e).__name__}: {e}")
            results.append({"source": "baanknet", "ok": False, "error": str(e)})
        print()

    if "bankeauctions" in wanted:
        print("── bankeauctions.com (C1 India) — ASP, private banks / NBFCs / ARCs")
        try:
            results.append(probe_bankeauctions(TN_BANKEAUCTIONS, args.timeout))
        except Exception as e:  # noqa: BLE001
            print(f"   FAILED: {type(e).__name__}: {e}")
            results.append({"source": "bankeauctions", "ok": False, "error": str(e)})
        print()

    print("=" * 68)
    for r in results:
        verdict = "ADAPTER VIABLE" if r.get("ok") else "NEEDS A LOOK"
        print(f"{r['source']:<16} {verdict:<16} TN rows={r.get('total', '?')}")
    print("\nNeither source needed a browser, a login, or a CAPTCHA.")
    print("Contracts and caveats: docs/source-recon-2026-09.md")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"\nRaw results → {args.json}")

    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
