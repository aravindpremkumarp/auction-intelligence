"""One polite HTTP session for every adapter.

Both new portals answered ~60 requests at ~1/s without complaint during the
recon; a full sweep is a different load, so the pause between requests is
configurable (``SOURCE_REQUEST_DELAY_S``, default 1.0) and retries back off
on 429 / 5xx rather than hammering.

No cookies are kept across runs and nothing logs in. The only header trick
any adapter needs is a ``Referer`` on bankeauctions' NIT zip, which the
caller passes per request.
"""
from __future__ import annotations

import os
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_DELAY_S = float(os.environ.get("SOURCE_REQUEST_DELAY_S", "1.0"))
DEFAULT_TIMEOUT_S = 40


class PoliteSession(requests.Session):
    """A ``requests.Session`` that waits ``delay_s`` between requests and
    retries transient failures with exponential backoff."""

    def __init__(self, delay_s: float = DEFAULT_DELAY_S, retries: int = 3):
        super().__init__()
        self.delay_s = max(0.0, delay_s)
        self._last = 0.0
        self._lock = threading.Lock()
        self.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})
        retry = Retry(
            total=retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def request(self, method, url, **kwargs):  # type: ignore[override]
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT_S)
        with self._lock:
            wait = self.delay_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        return super().request(method, url, **kwargs)


_shared: PoliteSession | None = None


def get_session() -> PoliteSession:
    """The process-wide session. Tests replace this function, not the class."""
    global _shared
    if _shared is None:
        _shared = PoliteSession()
    return _shared
