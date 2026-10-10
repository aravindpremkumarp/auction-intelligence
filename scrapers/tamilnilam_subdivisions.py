"""
scrapers/tamilnilam_subdivisions.py
-----------------------------------
The fifth dropdown — sub-divisions of one survey number — through a real
browser, because the server will not hand it out any other way.

WHY A BROWSER
-------------
The GI Viewer asks ``POST {GI_API_BASE}/land/check-areg`` with a payload the
page encrypts under a per-session key (``landEncryption.encrypt(json,
_mapSessionKey)``) and signs with ``X-Session-ID`` / ``X-CSRF-Token`` headers
it received at login. The key, the token and the cipher live in the page's
own JavaScript and change per session, so the cheapest faithful client is the
page itself: log in once, then run the same ``fetch`` the page's
``fetchCheckAregSecure()`` runs, from inside the page. That is what this
module does, with Playwright.

Login needs a TNGIS citizen account (mobile + password) and a captcha. The
captcha is saved to ``captcha.png`` and typed by whoever is at the terminal —
no solver is wired in, on purpose; add one behind ``captcha_reader`` if you
have one. Server limits honoured: 50 check-areg calls per account per hour
(``HourlyLimit`` is raised; the walk's progress is already on disk, re-run
after the window), and a burst detector that trips under ~3 s between calls
(default spacing 5 s, grows by 1 s on every 429).

This mirrors the flow of a working third-party scraper (see
docs/tamilnilam_dropdowns.md). It has NOT been run against the live site
from this repository — the selectors (``#publicIdentifier``,
``#publicPassword``, ``#publicCaptchaInput``, ``#publicLoginBtn``) and the
page globals (``_mapSessionKey``, ``GI_API_BASE``, ``landEncryption``) are the
ones that page exposed in 2026-09 and are the first thing to re-check if
login or fetch fails.

    pip install playwright && playwright install chromium
    TAMILNILAM_MOBILE=9xxxxxxxxx TAMILNILAM_PASSWORD=... \
    python -m scrapers.tamilnilam_dropdowns --district Ariyalur --taluk Andimadam --level subdivision
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable

VIEWER_URL = "https://tngis.tn.gov.in/apps/gi_viewer/map-viewer/index.html"
PAGE_TIMEOUT_MS = 60_000
MAX_DELAY_S = 10.0
BURST_WAIT_S = 90.0

#: The page's own check-areg request, returning the HTTP status and the
#: decrypted body as-is (the page's wrapper hides a 429 behind a generic
#: "Failed to check Areg."). ``sub_division_number`` is a deliberate non-value:
#: the server then lists every sub-division of the survey number.
_AREG_JS = """async a=>{
  if (typeof _mapSessionPromise !== 'undefined' && _mapSessionPromise) await _mapSessionPromise;
  if (!_mapSessionKey || !_mapSessionId || !_mapCsrfToken)
    return JSON.stringify({status:0, body:{success:0, message:'session not ready'}});
  const enc = landEncryption.encrypt(JSON.stringify({district_code:a[0], taluk_code:a[1], village_code:a[2],
      survey_number:a[3], sub_division_number:'jjj', area_type:a[4]}), _mapSessionKey);
  const r = await fetch(`${GI_API_BASE}/land/check-areg`, {method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json', 'X-Secure-Request':'true',
               'X-Session-ID':_mapSessionId, 'X-CSRF-Token':_mapCsrfToken},
      body: JSON.stringify({payload:enc})});
  const txt = await r.text(); let body = {};
  try { const j = JSON.parse(txt); body = j && j.payload ? JSON.parse(landEncryption.decrypt(j.payload, _mapSessionKey)) : j; }
  catch (e) { body = {success:0, message:'bad response: ' + txt.slice(0, 80)}; }
  return JSON.stringify({status:r.status, body:body});
}"""

#: Messages that mean "try again", not "no sub-divisions here".
_TRANSIENT = ("session", "token", "expired", "timeout", "temporarily", "internal", "gateway", "unavailable")


class HourlyLimit(RuntimeError):
    """The account's check-areg quota is spent. Progress is on disk; re-run
    after the window the message names (or with another account)."""


class LoginFailed(RuntimeError):
    pass


class BrowserSubdivisionFetcher:
    """``SubdivisionFetcher`` over a logged-in GI Viewer page. Use as a
    context manager so the browser is closed on the way out."""

    def __init__(self, mobile: str | None = None, password: str | None = None, *, delay_s: float = 5.0,
                 headless: bool = True, area_type: str = "rural", captcha_file: Path | str = "captcha.png",
                 captcha_reader: Callable[[Path], str] | None = None, log: Callable[[str], None] = print):
        self.mobile = mobile or os.environ.get("TAMILNILAM_MOBILE", "")
        self.password = password or os.environ.get("TAMILNILAM_PASSWORD", "")
        if not (self.mobile and self.password):
            raise LoginFailed("set TAMILNILAM_MOBILE and TAMILNILAM_PASSWORD (a TNGIS citizen login)")
        self.delay_s = delay_s
        self.headless = headless
        self.area_type = area_type
        self.captcha_file = Path(captcha_file)
        self.captcha_reader = captcha_reader or self._ask_terminal
        self.log = log
        self._pw = self._browser = self._page = None
        self._last_call = 0.0
        self.calls = 0

    # -- lifecycle --------------------------------------------------------- #
    def __enter__(self) -> "BrowserSubdivisionFetcher":
        from playwright.sync_api import sync_playwright  # heavy; only when this level is asked for
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.login()
        return self

    def __exit__(self, *exc) -> None:
        for closer in (getattr(self._browser, "close", None), getattr(self._pw, "stop", None)):
            try:
                closer and closer()
            except Exception:
                pass

    # -- login ------------------------------------------------------------- #
    @staticmethod
    def _ask_terminal(path: Path) -> str:
        try:
            return input(f"Open {path} and type the captcha (blank = fetch a new one): ").strip()
        except EOFError:
            return ""

    def login(self, max_tries: int = 8) -> None:
        from playwright.sync_api import Error as PWError
        ctx = self._browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=2)
        self._page = page = ctx.new_page()
        for attempt in range(1, max_tries + 1):
            try:
                page.goto(VIEWER_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                page.get_by_text("Existing User").click(timeout=PAGE_TIMEOUT_MS)
                page.fill("#publicIdentifier", self.mobile)
                page.fill("#publicPassword", self.password)
                page.wait_for_timeout(2500)  # the captcha canvas is drawn asynchronously from /auth/captcha
                page.locator("#publicCaptchaImage").screenshot(path=str(self.captcha_file))
            except PWError as e:
                self.log(f"  login page not ready ({str(e).splitlines()[0][:100]}); retrying in 20s")
                time.sleep(20)
                continue
            answer = self.captcha_reader(self.captcha_file)
            if not answer:
                try:
                    page.click("#publicRefreshCaptcha", timeout=2000)
                except PWError:
                    pass
                continue
            page.fill("#publicCaptchaInput", answer)
            page.click("#publicLoginBtn")
            page.wait_for_timeout(8000)
            if not page.query_selector("#publicLoginBtn"):
                self.log(f"  logged in to TNGIS as {self.mobile} (attempt {attempt})")
                return
            err = self._login_error(page)
            wait = _lockout_seconds(err)
            if wait:
                self.log(f"  network lockout from the server: {err!r}; sleeping {wait / 60:.0f} min")
                time.sleep(wait)
            else:
                self.log(f"  login failed ({err or 'wrong captcha?'}); new captcha")
        raise LoginFailed(f"could not log in after {max_tries} attempts")

    @staticmethod
    def _login_error(page) -> str:
        try:
            return page.evaluate("()=>{const e=document.querySelector('#publicLoginError');"
                                 "return e && !e.classList.contains('d-none') ? e.innerText.trim() : ''}") or ""
        except Exception:
            return ""

    # -- the call ---------------------------------------------------------- #
    def fetch(self, district_code: str, taluk_code: str, village_code: str, survey_number: str) -> list[str]:
        """Sub-division numbers of one survey number (``[]`` when the server
        says there are none). Retries transient failures and the burst
        limiter; raises ``HourlyLimit`` when the account's quota is spent."""
        fails = 0
        while True:
            wait = self.delay_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1
            try:
                raw = self._page.evaluate(_AREG_JS, [district_code, taluk_code, village_code, survey_number,
                                                     self.area_type])
                res = json.loads(raw)
            except Exception as e:
                fails += 1
                self.log(f"  check-areg error ({str(e).splitlines()[0][:100]}); retrying in 10s")
                if fails % 3 == 0:
                    self.login()
                time.sleep(10)
                continue
            status, body = res.get("status"), res.get("body") or {}
            if status == 200 and body.get("success") == 2:
                return [str(x.get("subdiv_no", "")).strip() for x in body.get("data", []) if x.get("subdiv_no") not in (None, "")]
            msg = str(body.get("message", "")).lower()
            if status == 429 or body.get("rate_limited") or "limit" in msg or "too many" in msg:
                if "slow down" in msg or "unusual" in msg:
                    self.delay_s = min(self.delay_s + 1.0, MAX_DELAY_S)
                    self.log(f"  burst limiter tripped; waiting {BURST_WAIT_S:.0f}s, spacing now {self.delay_s:.0f}s")
                    time.sleep(BURST_WAIT_S)
                    continue
                raise HourlyLimit(body.get("message") or f"HTTP {status}")
            if (status is not None and status != 200) or any(k in msg for k in _TRANSIENT):
                fails += 1
                self.log(f"  check-areg transient failure (HTTP {status}: {body.get('message')}); retrying")
                if fails % 3 == 0:
                    self.login()
                time.sleep(10)
                continue
            # success != 2 with no error: the server's way of saying "no sub-divisions"
            return []


def _lockout_seconds(msg: str) -> float:
    """Seconds to wait if ``msg`` is the per-network login lockout ("Too many
    failed login attempts ... Try again in 4hr 12min."), else 0."""
    m = msg.lower()
    if "too many" not in m and "try again in" not in m:
        return 0
    t = re.search(r"try again in\s*(?:(\d+)\s*h(?:ou)?rs?)?\s*(?:(\d+)\s*min)?", m)
    h, mi = (int(t.group(1) or 0), int(t.group(2) or 0)) if t else (0, 0)
    return (h * 3600 + mi * 60 or 900) + 90
