"""
scrapers/tnreginet_guideline.py
-------------------------------
Walk TNREGINET's guideline-value search (Registration Department, Tamil Nadu):
District > Sub-Registrar Office > Village > Streets, and save each street with
its guideline value.

WHY
---
Chennai notices name a locality ("Teynampet", "Madipakkam") that no revenue
register lists as a village. The guideline-value tables list streets *under
their revenue village*, per sub-registrar office (SRO): the government's own
street → village link. The SRO → village lists alone also help, since many
notices name the SRO.

RUN IT FROM AN INDIAN CONNECTION. The portal resets connections from cloud
hosts (checked 2026-10-12 from a cloud container); it answers a home browser.

WHAT IT TALKS TO (mapped 2026-10-12 in a browser)
-------------------------------------------------
One endpoint, plain form POSTs, no login and no captcha — but a session cookie
(HttpOnly, set on the first GET) and a Spring ``_csrf`` token from
``<meta name="_csrf">``::

    GET  /portal/webHP                                  -> cookie + _csrf (page in Tamil)
    POST /portal/webHP?requestType=ApplicationRH&actionVal=homePage&screenId=114
                      &UserLocaleID=en&_csrf=T          -> English page, district <select>
    POST /portal/webHP?requestType=ApplicationRH&actionVal=getSroOptionByDistrict
                      &screenId=7000054&districtId=D      body: _csrf=T
         -> "<option value='20051'>Adayar</option>..."   (loose tags, not XML)
    POST /portal/webHP?requestType=ApplicationRH&actionVal=getVillageOptionBySro
                      &fromHomeScreen=1&screenId=7000054&sroId=S&stSurvey=1
         -> "<option value='1'>Adayar</option>...&Chennai"  (trailing "&<district>")
    POST /portal/webHP?requestType=ApplicationRH&actionVal=listAllStreet&fromHome=1
                      &villageIdAllStreet=<id>&screenId=7000054&_csrf=T[&d-NNNN-p=<page>]
         body: villageIdAllStreet=<id>&zoneName=<district>&sroName=<sro>
               &villageName=<village>&villageId=<id>&_csrf=T
         -> "96 items found, displaying 1 to 10." + <table id="surveyTable">:
            Sr.No | Street Name | Guideline Value (British Value) |
            Guideline Value (Metric Value) | Land Classification |
            Effective Start Date | G.O.Download
            10 rows a page; the page links carry the displaytag page parameter
            ``d-NNNN-p``, read from the reply.

That is the page's "List All Street" button. (Its street search, actionVal=
criteriaSearch, returns only the first 10 matches, so it cannot list streets.)
A village is kept only when the rows collected equal the portal's item count.

OUTPUT (``--out``, default ``data/tnreginet/``; git-ignored)
------------------------------------------------------------
    districts.json     district id → name, from the page
    sros.jsonl         one row per SRO, with district
    villages.jsonl     one row per (SRO, village id, village)
    streets.jsonl      one row per (SRO, village, street) with guideline values
    progress.json      finished SROs (village level) and villages (street level)

Resumable: a parent is marked done only after its children are on disk. A
failed call raises; it is never recorded as "no villages" or "no streets".

USAGE
-----
    python -m scrapers.tnreginet_guideline --level village            # SRO → village lists
    python -m scrapers.tnreginet_guideline --level street --sro Adayar # streets of one SRO
    python -m scrapers.tnreginet_guideline --level street             # every Chennai street
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path

BASE = "https://tnreginet.gov.in/portal/webHP"
SCREEN = "7000054"
DEFAULT_OUT = Path("data/tnreginet")
LEVELS = ("village", "street")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (auction-intelligence guideline lookup)",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "text/html, */*; q=0.01",
}

_CSRF = re.compile(r"""<meta[^>]+name=["']_csrf["'][^>]+content=["']([^"']+)["']""", re.I)
_CSRF_ALT = re.compile(r"""<meta[^>]+content=["']([^"']+)["'][^>]+name=["']_csrf["']""", re.I)
_OPTION = re.compile(r"""<option[^>]*value=["']?([^"'>\s]+)["']?[^>]*>(.*?)</option>""", re.I | re.S)
_SELECT = re.compile(r"<select\b([^>]*)>(.*?)</select>", re.I | re.S)
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_TOTAL = re.compile(r"""class=["']pagebanner["'][^>]*>\s*([\d,]+|one|no)\s+items?\s+found""", re.I)
_NOTHING = re.compile(r"""<div[^>]*id=["']nothing["'][^>]*>\s*Nothing Found to Display""", re.I)
_PAGE_PARAM =re.compile(r"[?&;](d-\d+-p)=\d+")
_SURVEY_TABLE = re.compile(r"""<table\b[^>]*id=["']surveyTable["'][^>]*>.*?</table>""", re.I | re.S)


class PortalError(RuntimeError):
    """The portal answered something this walker cannot trust."""


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", fragment or "")).split())


def parse_options(body: str) -> list[tuple[str, str]]:
    """``(value, name)`` of every real option, the "- Select -" one dropped.
    The village reply ends in ``&<district>``, which is stripped first."""
    body = re.sub(r"&[^<>]*$", "", (body or "").strip())
    out = []
    for value, name in _OPTION.findall(body):
        name = _text(name)
        if value and value != "-1" and name and not name.startswith("-"):
            out.append((value, name))
    return out


def parse_csrf(page: str) -> str:
    m = _CSRF.search(page) or _CSRF_ALT.search(page)
    if not m:
        raise PortalError("no _csrf meta tag on the portal page")
    return m.group(1)


def parse_districts(page: str) -> list[tuple[str, str]]:
    """The district dropdown: the <select> whose options include Chennai."""
    for _attrs, inner in _SELECT.findall(page):
        opts = parse_options(inner)
        if any(name.lower() == "chennai" for _, name in opts):
            return opts
    raise PortalError("no district <select> (one listing Chennai) on the portal page")


def parse_total(body: str) -> int | None:
    """The "N items found" count over the result table, 0 for the portal's
    "Nothing Found to Display" box, or None if neither is there."""
    m = _TOTAL.search(body or "")
    if not m:
        return 0 if _NOTHING.search(body or "") else None
    n = m.group(1).lower()
    return {"one": 1, "no": 0}[n] if n in ("one", "no") else int(n.replace(",", ""))


def parse_streets(body: str) -> list[dict]:
    """Rows of the result table, keyed by its header. Rows without a street
    name (headers, "no records") are dropped. The reply also holds a
    search-criteria table, so the result table is picked by its id."""
    m = _SURVEY_TABLE.search(body or "")
    body = m.group(0) if m else body
    rows = [[_text(c) for c in _CELL.findall(r)] for r in _ROW.findall(body or "")]
    rows = [r for r in rows if r]
    if not rows:
        return []
    header = [h.lower() for h in rows[0]]

    def col(*words: str) -> int | None:
        for i, h in enumerate(header):
            if all(w in h for w in words):
                return i
        return None

    i_street = col("street")
    if i_street is None:
        return []
    i_ft = next((i for i in (col("british"), col("feet"), col("sq", "ft")) if i is not None), None)
    i_m = next((i for i in (col("metric"), col("metre"), col("sq", "m")) if i is not None), None)
    i_class, i_date = col("classification"), col("effective")
    out = []
    for r in rows[1:]:
        if len(r) <= i_street or not r[i_street]:
            continue

        def get(i):
            return r[i] if i is not None and i < len(r) else None
        out.append({"street": r[i_street], "value_sqft": get(i_ft), "value_sqm": get(i_m),
                    "classification": get(i_class), "effective_from": get(i_date)})
    return out


class TnreginetClient:
    """A cookie-keeping session with the page's _csrf token."""

    def __init__(self, session=None, base: str = BASE, delay: float = 1.5):
        if session is None:
            import requests
            session = requests.Session()
        self.s, self.base, self.delay = session, base, delay
        self.csrf: str | None = None
        self.page = ""

    def start(self) -> None:
        r = self.s.get(self.base, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=60)
        if r.status_code != 200:
            raise PortalError(f"GET portal: HTTP {r.status_code}")
        # The portal opens in Tamil; its "English" link re-renders the home
        # page for this session, which gives English district/SRO/village names.
        csrf = parse_csrf(r.text)
        r = self.s.post(self.base, params={"requestType": "ApplicationRH", "actionVal": "homePage",
                                           "screenId": "114", "UserLocaleID": "en", "_csrf": csrf},
                        data={"_csrf": csrf}, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=60)
        if r.status_code != 200:
            raise PortalError(f"switch to English: HTTP {r.status_code}")
        self.page = r.text
        self.csrf = parse_csrf(self.page)

    def _post(self, params: dict, data: dict, csrf_in_url: bool = False) -> str:
        if self.csrf is None:
            self.start()
        for attempt in (1, 2):
            time.sleep(self.delay)
            # read the token per attempt: a 403 restart replaces it
            query = {"requestType": "ApplicationRH", **params}
            if csrf_in_url:
                query["_csrf"] = self.csrf
            r = self._send(query, {**data, "_csrf": self.csrf})
            if r.status_code == 403 and attempt == 1:
                self.start()  # expired session or token: one fresh start
                continue
            if r.status_code != 200:
                raise PortalError(f"{params.get('actionVal')}: HTTP {r.status_code}")
            return r.text
        raise PortalError("unreachable")

    def _send(self, query: dict, data: dict, waits=(10, 30, 60, 120)):
        """One POST, retried through short network drops (DNS failures and
        resets seen mid-run from a home connection)."""
        import requests
        for wait in (*waits, None):
            try:
                return self.s.post(self.base, params=query, data=data, headers=HEADERS, timeout=60)
            except (requests.ConnectionError, requests.Timeout):
                if wait is None:
                    raise
                time.sleep(wait)

    def districts(self) -> list[tuple[str, str]]:
        if not self.page:
            self.start()
        return parse_districts(self.page)

    def sros(self, district_id: str) -> list[tuple[str, str]]:
        return parse_options(self._post({"actionVal": "getSroOptionByDistrict",
                                         "screenId": SCREEN, "districtId": district_id}, {}))

    def villages(self, sro_id: str) -> list[tuple[str, str]]:
        return parse_options(self._post({"actionVal": "getVillageOptionBySro", "fromHomeScreen": "1",
                                         "screenId": SCREEN, "sroId": sro_id, "stSurvey": "1"}, {}))

    def streets(self, district: str, sro: str, village: str, village_id: str) -> list[dict]:
        """Every street of a village, all pages. Raises unless the rows
        collected match the portal's item count."""
        params = {"actionVal": "listAllStreet", "fromHome": "1",
                  "villageIdAllStreet": village_id, "screenId": SCREEN}
        form = {"villageIdAllStreet": village_id, "zoneName": district, "sroName": sro,
                "villageName": village, "villageId": village_id}
        body = self._post(params, form, csrf_in_url=True)
        total = parse_total(body)
        if total is None:
            raise PortalError(f"{sro} / {village}: no item count in the street list")
        rows = parse_streets(body)
        page_param = _PAGE_PARAM.search(html.unescape(body))
        page = 1
        while len(rows) < total:
            if page_param is None:
                raise PortalError(f"{sro} / {village}: {total} streets but no page links")
            page += 1
            more = parse_streets(self._post({**params, page_param.group(1): str(page)}, form,
                                            csrf_in_url=True))
            if not more:
                raise PortalError(f"{sro} / {village}: page {page} empty at {len(rows)}/{total}")
            rows += more
        if len(rows) != total:
            raise PortalError(f"{sro} / {village}: {len(rows)} rows for {total} items")
        return rows


class Store:
    def __init__(self, out: Path | str):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self._progress_path = self.out / "progress.json"
        try:
            self.progress = json.loads(self._progress_path.read_text())
        except FileNotFoundError:
            self.progress = {"sros_done": [], "villages_done": []}

    def append(self, name: str, rows: list[dict]) -> None:
        with open(self.out / name, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def read(self, name: str) -> list[dict]:
        try:
            with open(self.out / name, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []

    def done(self, kind: str, key: str) -> bool:
        return key in self.progress[kind]

    def mark(self, kind: str, key: str) -> None:
        self.progress[kind].append(key)
        self._progress_path.write_text(json.dumps(self.progress, indent=1))


def _match(name: str, wanted: str | None) -> bool:
    return not wanted or wanted.lower() in name.lower()


def walk(client: TnreginetClient, store: Store, *, level: str = "village",
         district: str | None = "Chennai", sro: str | None = None,
         village: str | None = None, log=print) -> dict:
    stats = {"sros": 0, "villages": 0, "streets": 0, "empty_villages": 0}
    districts = client.districts()
    (store.out / "districts.json").write_text(json.dumps(dict(districts), indent=1, ensure_ascii=False))
    for d_id, d_name in districts:
        if not _match(d_name, district):
            continue
        for s_id, s_name in client.sros(d_id):
            if not _match(s_name, sro):
                continue
            stats["sros"] += 1
            key = f"{d_id}/{s_id}"
            if not store.done("sros_done", key):
                rows = [{"district_id": d_id, "district": d_name, "sro_id": s_id, "sro": s_name,
                         "village_id": v_id, "village": v_name}
                        for v_id, v_name in client.villages(s_id)]
                store.append("sros.jsonl", [{"district_id": d_id, "district": d_name,
                                             "sro_id": s_id, "sro": s_name}])
                store.append("villages.jsonl", rows)
                store.mark("sros_done", key)
                log(f"{d_name} / {s_name}: {len(rows)} villages")
            if level != "street":
                continue
            for v in [r for r in store.read("villages.jsonl")
                      if r["district_id"] == d_id and r["sro_id"] == s_id]:
                if not _match(v["village"], village):
                    continue
                stats["villages"] += 1
                vkey = f"{key}/{v['village_id']}"
                if store.done("villages_done", vkey):
                    continue
                found = client.streets(d_name, s_name, v["village"], v["village_id"])
                store.append("streets.jsonl", [{**{k: v[k] for k in ("district", "sro_id", "sro",
                                                                      "village_id", "village")}, **r}
                                               for r in found])
                store.mark("villages_done", vkey)
                stats["streets"] += len(found)
                stats["empty_villages"] += not found
                log(f"  {s_name} / {v['village']}: {len(found)} streets")
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("WHAT IT TALKS TO")[0].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--level", choices=LEVELS, default="village")
    ap.add_argument("--district", default="Chennai",
                    help="districts whose name contains this (default Chennai; '' for all)")
    ap.add_argument("--sro", help="only SROs whose name contains this")
    ap.add_argument("--village", help="only villages whose name contains this")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between calls (be polite)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)
    stats = walk(TnreginetClient(delay=args.delay), Store(args.out), level=args.level,
                 district=args.district or None, sro=args.sro, village=args.village)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
