"""HTTP helpers: one configured client, polite retries."""
from __future__ import annotations

import logging
import time

import httpx

from .config import Settings

log = logging.getLogger(__name__)


def make_client(settings: Settings, **headers: str) -> httpx.Client:
    h = {"User-Agent": settings.http.user_agent, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}
    h.update(headers)
    return httpx.Client(
        headers=h,
        timeout=settings.http.timeout_seconds,
        proxy=settings.http.proxy,
        follow_redirects=True,
    )


def get_with_retry(client: httpx.Client, url: str, *, retries: int = 3, backoff: float = 2.0, **kwargs) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.get(url, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"retryable {resp.status_code}", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and status not in (429, 500, 502, 503, 504):
                raise
            wait = backoff * (2**attempt)
            log.warning("GET %s failed (%s); retrying in %.1fs", url, e, wait)
            time.sleep(wait)
    assert last is not None
    raise last
