"""
scrapers/tamilnilam_dropdowns.py
--------------------------------
Pull the cascading lists behind the TamilNilam Geo-Info app's search panel —
District > Taluk > Village > Survey Number (> Sub-division) — straight from
the listing API the app and the TNGIS GI Viewer fill their dropdowns from.

WHAT IT TALKS TO
----------------
The Android app (``org.tnega.tamil.nilam``, TNeGA) and the web viewer at
``tngis.tn.gov.in/apps/gi_viewer/`` share one backend. Four of the five
dropdowns are plain GET calls on the "generic" listing API::

    GET {BASE}admin_master_district
            ?request_type=district
    GET {BASE}admin_master_taluk
            ?district_code=D&request_type=taluk
    GET {BASE}admin_master_village
            ?district_code=D&taluk_code=T&request_type=revenue_village
    GET {BASE}admin_master_survey_number
            ?district_code=D&taluk_code=T&revenue_village_code=V
            &area_type=rural&data_type=cadastral&request_type=survey_number

    headers: x-app-name: demo, x-requested-with: XMLHttpRequest
    reply:   {"success": 1, "data": [ {...}, ... ]}

The fifth list — sub-divisions of one survey number — is NOT a listing call.
The viewer gets it from ``POST .../land/check-areg`` behind a login, a
captcha, a per-session encryption key and a quota of 50 calls per account per
hour. ``scrapers/tamilnilam_subdivisions.py`` drives a real browser for that
level and plugs in here through ``SubdivisionFetcher``.

Endpoint shapes were taken from a working third-party scraper of the viewer
(docs/tamilnilam_dropdowns.md has the trail). They have NOT been exercised
from this repository's network yet — the host resets connections from the
cloud box this was written on — so the first live run is also the
verification run: ``--level district`` first, then widen.

OUTPUT (``--out``, default ``data/tamilnilam/``)
------------------------------------------------
    districts.json          the full district list, rewritten each run
    taluks.jsonl            one row per taluk, with district_code
    villages.jsonl          one row per revenue village, with district/taluk codes
    survey_numbers.jsonl    one row per (village, survey number)
    subdivisions.jsonl      one row per (village, survey number, sub-division)
    progress.json           which parents are finished at each level

Every row keeps the server's raw fields under ``raw`` next to the normalised
``*_code`` / ``*_name`` keys, so a field this module did not anticipate is
never lost. Runs are resumable: a parent is marked done only after its
children are on disk, and a done parent is never fetched again. A failed call
raises instead of recording an empty list, so a server hiccup can never look
like "this taluk has no villages".

USAGE
-----
    # the cheap levels: ~38 districts, a few hundred taluks, ~17k villages
    python -m scrapers.tamilnilam_dropdowns --level village

    # survey numbers for one taluk (one call per village)
    python -m scrapers.tamilnilam_dropdowns --district Tiruvallur --taluk Ponneri --level survey

    # the gazetteer refresh takes this CSV directly
    python -m scrapers.tamilnilam_dropdowns --level village --export-csv tn_villages.csv

    # sub-divisions need a TNGIS account + browser (see tamilnilam_subdivisions.py)
    TAMILNILAM_MOBILE=... TAMILNILAM_PASSWORD=... \
    python -m scrapers.tamilnilam_dropdowns --district Ariyalur --taluk Andimadam --level subdivision

Env: TAMILNILAM_APP_NAME (default "demo"), TAMILNILAM_COOKIE (optional,
pasted from a logged-in browser if the listing calls turn out to need one),
SOURCE_REQUEST_DELAY_S (pause between calls, default 1.0).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable, Protocol

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sources.http import PoliteSession  # noqa: E402

BASE = "https://tngis.tn.gov.in/apps/generic_api/v2/"
LEVELS = ("district", "taluk", "village", "survey", "subdivision")
DEFAULT_OUT = _REPO_ROOT / "data" / "tamilnilam"


class ApiError(RuntimeError):
    """A listing call did not come back as ``{"success": 1, "data": [...]}``
    after every retry. Raised, never swallowed: see the module docstring."""


class SubdivisionFetcher(Protocol):
    """Anything that can answer "which sub-divisions does this survey number
    have" — the browser-backed one lives in ``tamilnilam_subdivisions.py``."""

    def fetch(self, district_code: str, taluk_code: str, village_code: str,
              survey_number: str) -> list[str]: ...


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

def _pick(row: dict, *keys: str) -> str:
    """First non-empty value among ``keys`` (the server has used more than one
    spelling for the same thing across its apps), as a stripped string."""
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def norm_district(row: dict) -> dict:
    return {
        "district_code": _pick(row, "district_code", "code"),
        "district_name": _pick(row, "district_english_name", "district_name", "name"),
        "district_name_ta": _pick(row, "district_tamil_name", "district_name_ta"),
        "raw": row,
    }


def norm_taluk(row: dict, district_code: str) -> dict:
    return {
        "district_code": district_code,
        "taluk_code": _pick(row, "taluk_code", "code"),
        "taluk_name": _pick(row, "taluk_english_name", "taluk_name", "name"),
        "taluk_name_ta": _pick(row, "taluk_tamil_name", "taluk_name_ta"),
        "raw": row,
    }


def norm_village(row: dict, district_code: str, taluk_code: str) -> dict:
    return {
        "district_code": district_code,
        "taluk_code": taluk_code,
        "village_code": _pick(row, "village_code", "revenue_village_code", "code"),
        "village_name": _pick(row, "village_english_name", "village_name", "revenue_village_name", "name"),
        "village_name_ta": _pick(row, "village_tamil_name", "village_name_ta"),
        "raw": row,
    }


def norm_survey(row: dict, district_code: str, taluk_code: str, village_code: str) -> dict:
    return {
        "district_code": district_code,
        "taluk_code": taluk_code,
        "village_code": village_code,
        "survey_number": _pick(row, "survey_number", "survey_no", "sno"),
        "raw": row,
    }


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class TamilNilamClient:
    """The four listing calls, each returning the normalised rows.

    ``session`` is anything with ``requests``' ``get(url, params=, headers=)``
    — the polite shared session by default, a stub in tests. ``sleep`` is
    injectable for the same reason.
    """

    def __init__(self, session=None, *, base: str = BASE, app_name: str | None = None,
                 cookie: str | None = None, retries: int = 4,
                 sleep: Callable[[float], None] = time.sleep):
        self.session = session or PoliteSession()
        self.base = base if base.endswith("/") else base + "/"
        self.retries = max(1, retries)
        self.sleep = sleep
        self.headers = {
            "x-app-name": app_name or os.environ.get("TAMILNILAM_APP_NAME", "demo"),
            "x-requested-with": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://tngis.tn.gov.in/apps/gi_viewer/map-viewer/index.html",
        }
        cookie = cookie if cookie is not None else os.environ.get("TAMILNILAM_COOKIE")
        if cookie:
            self.headers["Cookie"] = cookie
        self.calls = Counter()

    def _get(self, path: str, **params) -> list[dict]:
        url = self.base + path
        last = ""
        for attempt in range(1, self.retries + 1):
            self.calls[path] += 1
            try:
                resp = self.session.get(url, params=params, headers=self.headers)
            except Exception as e:  # network-level: retry like a 5xx
                last = f"{type(e).__name__}: {e}"
            else:
                last = f"HTTP {resp.status_code}: {resp.text[:200]!r}"
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = None
                    if isinstance(body, dict) and body.get("success") == 1 and isinstance(body.get("data"), list):
                        return body["data"]
                    if isinstance(body, dict):
                        last = f"success={body.get('success')!r} message={body.get('message')!r}"
                if resp.status_code == 429 and hasattr(self.session, "delay_s"):
                    # the viewer's burst detector asks callers to slow down; obey for the rest of the run
                    self.session.delay_s = min(self.session.delay_s + 1.0, 10.0)
            if attempt < self.retries:
                self.sleep(min(60.0, 5.0 * attempt))
        raise ApiError(f"{path} {params}: {last}")

    def districts(self) -> list[dict]:
        return [norm_district(r) for r in self._get("admin_master_district", request_type="district")]

    def taluks(self, district_code: str) -> list[dict]:
        rows = self._get("admin_master_taluk", district_code=district_code, request_type="taluk")
        return [norm_taluk(r, district_code) for r in rows]

    def villages(self, district_code: str, taluk_code: str) -> list[dict]:
        rows = self._get("admin_master_village", district_code=district_code, taluk_code=taluk_code,
                         request_type="revenue_village")
        return [norm_village(r, district_code, taluk_code) for r in rows]

    def survey_numbers(self, district_code: str, taluk_code: str, village_code: str,
                       area_type: str = "rural") -> list[dict]:
        rows = self._get("admin_master_survey_number", district_code=district_code, taluk_code=taluk_code,
                         revenue_village_code=village_code, area_type=area_type, data_type="cadastral",
                         request_type="survey_number")
        return [norm_survey(r, district_code, taluk_code, village_code) for r in rows]


# --------------------------------------------------------------------------- #
# On-disk store with resume
# --------------------------------------------------------------------------- #

class Store:
    """JSON/JSONL files under one directory plus a ``progress.json`` that says
    which parents are complete at each level. Appends are flushed before the
    parent is marked done, so a crash between the two re-fetches (and
    de-duplicates on read) rather than losing children."""

    FILES = {
        "taluk": "taluks.jsonl",
        "village": "villages.jsonl",
        "survey": "survey_numbers.jsonl",
        "subdivision": "subdivisions.jsonl",
    }
    KEYS = {  # what makes a row unique at each level, for de-dup on read
        "taluk": ("district_code", "taluk_code"),
        "village": ("district_code", "taluk_code", "village_code"),
        "survey": ("district_code", "taluk_code", "village_code", "survey_number"),
        "subdivision": ("district_code", "taluk_code", "village_code", "survey_number", "subdivision"),
    }

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._progress_path = self.root / "progress.json"
        self.progress: dict[str, list[str]] = {lvl: [] for lvl in self.FILES}
        if self._progress_path.exists():
            self.progress.update(json.loads(self._progress_path.read_text(encoding="utf-8")))
        self._done = {lvl: set(v) for lvl, v in self.progress.items()}

    # -- progress ---------------------------------------------------------- #
    @staticmethod
    def parent_key(*codes: str) -> str:
        return "|".join(codes)

    def is_done(self, level: str, key: str) -> bool:
        return key in self._done[level]

    def mark_done(self, level: str, key: str) -> None:
        if key in self._done[level]:
            return
        self._done[level].add(key)
        self.progress[level] = sorted(self._done[level])
        tmp = self._progress_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.progress, indent=1), encoding="utf-8")
        os.replace(tmp, self._progress_path)

    # -- rows -------------------------------------------------------------- #
    def write_districts(self, rows: list[dict]) -> None:
        (self.root / "districts.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    def read_districts(self) -> list[dict]:
        p = self.root / "districts.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    def append(self, level: str, rows: Iterable[dict]) -> None:
        with open(self.root / self.FILES[level], "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def rows(self, level: str, **where: str) -> list[dict]:
        """Rows of one level, de-duplicated on the level's key, optionally
        filtered on equality of the given columns."""
        p = self.root / self.FILES[level]
        if not p.exists():
            return []
        seen: dict[tuple, dict] = {}
        keys = self.KEYS[level]
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if all(r.get(k) == v for k, v in where.items()):
                    seen[tuple(r.get(k) for k in keys)] = r
        return list(seen.values())


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #

def _match(name: str, wanted: str | None) -> bool:
    return not wanted or wanted.lower() in (name or "").lower()


def walk(client: TamilNilamClient, store: Store, *, level: str = "village",
         district: str | None = None, taluk: str | None = None, village: str | None = None,
         area_type: str = "rural", subdivisions: SubdivisionFetcher | None = None,
         log: Callable[[str], None] = print) -> Counter:
    """Fetch every level down to ``level`` under the name filters, resuming
    from ``store``. Returns a count of rows fetched per level this run."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    if level == "subdivision" and subdivisions is None:
        raise ValueError("level=subdivision needs a SubdivisionFetcher (see tamilnilam_subdivisions.py)")
    depth = LEVELS.index(level)
    got: Counter = Counter()

    districts = client.districts()
    store.write_districts(districts)
    got["district"] = len(districts)
    log(f"{len(districts)} districts")
    if depth < 1:
        return got

    for d in districts:
        dc, dn = d["district_code"], d["district_name"]
        if not _match(dn, district):
            continue
        dkey = store.parent_key(dc)
        if store.is_done("taluk", dkey):
            taluks = store.rows("taluk", district_code=dc)
        else:
            taluks = client.taluks(dc)
            store.append("taluk", taluks)
            store.mark_done("taluk", dkey)
            got["taluk"] += len(taluks)
        log(f"{dn}: {len(taluks)} taluks")
        if depth < 2:
            continue

        for t in taluks:
            tc, tn = t["taluk_code"], t["taluk_name"]
            if not _match(tn, taluk):
                continue
            tkey = store.parent_key(dc, tc)
            if store.is_done("village", tkey):
                villages = store.rows("village", district_code=dc, taluk_code=tc)
            else:
                villages = client.villages(dc, tc)
                store.append("village", villages)
                store.mark_done("village", tkey)
                got["village"] += len(villages)
            log(f"  {dn} / {tn}: {len(villages)} villages")
            if depth < 3:
                continue

            for v in villages:
                vc, vn = v["village_code"], v["village_name"]
                if not _match(vn, village):
                    continue
                vkey = store.parent_key(dc, tc, vc)
                if store.is_done("survey", vkey):
                    surveys = store.rows("survey", district_code=dc, taluk_code=tc, village_code=vc)
                else:
                    surveys = client.survey_numbers(dc, tc, vc, area_type=area_type)
                    store.append("survey", surveys)
                    store.mark_done("survey", vkey)
                    got["survey"] += len(surveys)
                    log(f"    {vn}: {len(surveys)} survey numbers")
                if depth < 4:
                    continue

                pending = [s for s in surveys if not store.is_done("subdivision", store.parent_key(dc, tc, vc, s["survey_number"]))]
                for s in pending:
                    sn = s["survey_number"]
                    subs = subdivisions.fetch(dc, tc, vc, sn)  # type: ignore[union-attr]
                    store.append("subdivision", [{
                        "district_code": dc, "taluk_code": tc, "village_code": vc,
                        "survey_number": sn, "subdivision": sub, "area_type": area_type,
                    } for sub in subs])
                    store.mark_done("subdivision", store.parent_key(dc, tc, vc, sn))
                    got["subdivision"] += len(subs)
                if pending:
                    log(f"    {vn}: sub-divisions for {len(pending)} survey numbers")
    return got


# --------------------------------------------------------------------------- #
# Export for scripts/refresh_village_gazetteer.py
# --------------------------------------------------------------------------- #

def export_villages_csv(store: Store, path: Path | str) -> int:
    """``district,taluk,village`` plus the TNGIS codes under headers the
    gazetteer refresh ignores — its ``village_code`` is the within-taluk
    revenue serial and nobody has confirmed TNGIS's code is that serial, so
    the codes ride along for a human to compare, not for the loader to read.
    Returns the number of rows written."""
    d_name = {d["district_code"]: d["district_name"] for d in store.read_districts()}
    t_name = {(t["district_code"], t["taluk_code"]): t["taluk_name"] for t in store.rows("taluk")}
    villages = sorted(store.rows("village"), key=lambda r: (d_name.get(r["district_code"], ""),
                                                             t_name.get((r["district_code"], r["taluk_code"]), ""),
                                                             r["village_name"]))
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["district", "taluk", "village", "name_ta",
                    "tngis_district_code", "tngis_taluk_code", "tngis_village_code"])
        for r in villages:
            dn = d_name.get(r["district_code"])
            tn = t_name.get((r["district_code"], r["taluk_code"]))
            if not (dn and tn and r["village_name"]):
                continue  # half a hierarchy is not a row the gazetteer can diff
            w.writerow([dn, tn, r["village_name"], r.get("village_name_ta", ""),
                        r["district_code"], r["taluk_code"], r["village_code"]])
            n += 1
    return n


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("WHAT IT TALKS TO")[0].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--level", choices=LEVELS, default="village",
                    help="deepest list to fetch (default village; survey = one call per village)")
    ap.add_argument("--district", help="only districts whose name contains this (case-insensitive)")
    ap.add_argument("--taluk", help="only taluks whose name contains this")
    ap.add_argument("--village", help="only villages whose name contains this")
    ap.add_argument("--area-type", default="rural", choices=("rural", "urban"),
                    help="survey-number list variant (the viewer's rural/urban toggle)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help=f"output directory (default {DEFAULT_OUT})")
    ap.add_argument("--delay", type=float, default=None,
                    help="seconds between calls (default SOURCE_REQUEST_DELAY_S or 1.0)")
    ap.add_argument("--base", default=BASE, help="listing API base URL, if it moves")
    ap.add_argument("--app-name", default=None, help="x-app-name header (default TAMILNILAM_APP_NAME or demo)")
    ap.add_argument("--export-csv", metavar="PATH",
                    help="after the walk, write every stored village as a gazetteer-refresh CSV")
    ap.add_argument("--subdivision-delay", type=float, default=5.0,
                    help="seconds between check-areg calls; the burst limiter trips below ~3 (default 5)")
    ap.add_argument("--headed", action="store_true", help="show the browser used for sub-divisions")
    args = ap.parse_args(argv)

    session = PoliteSession(delay_s=args.delay) if args.delay is not None else PoliteSession()
    client = TamilNilamClient(session, base=args.base, app_name=args.app_name)
    store = Store(args.out)

    fetcher = None
    if args.level == "subdivision":
        from scrapers.tamilnilam_subdivisions import BrowserSubdivisionFetcher  # Playwright, imported on demand
        fetcher = BrowserSubdivisionFetcher(delay_s=args.subdivision_delay, headless=not args.headed)

    t0 = time.monotonic()
    try:
        if fetcher is not None:
            with fetcher:
                got = walk(client, store, level=args.level, district=args.district, taluk=args.taluk,
                           village=args.village, area_type=args.area_type, subdivisions=fetcher)
        else:
            got = walk(client, store, level=args.level, district=args.district, taluk=args.taluk,
                       village=args.village, area_type=args.area_type)
    except ApiError as e:
        print(f"\nstopped: {e}\nprogress is saved in {store.root}; re-run to resume", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"\ninterrupted; progress is saved in {store.root}; re-run to resume", file=sys.stderr)
        return 130

    print(f"\nfetched this run: {dict(got)}  ({time.monotonic() - t0:.0f}s, {sum(client.calls.values())} listing calls)")
    print(f"files in {store.root}")
    if args.export_csv:
        n = export_villages_csv(store, args.export_csv)
        print(f"wrote {n} village rows to {args.export_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
