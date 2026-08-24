"""Shared HTTP client.

One session per process, conservative retries, identifiable User-Agent.
No CAPTCHA or anti-bot circumvention of any kind.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import USER_AGENT, get_settings

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class TransientHTTPError(RuntimeError):
    """Raised for statuses that are worth retrying."""

    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


class SourceUnavailable(RuntimeError):
    """Raised when a source cannot be read at all (after retries)."""


_session: requests.Session | None = None


def get_session() -> requests.Session:
    global _session
    if _session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en,pt-BR;q=0.8,es;q=0.6",
            }
        )
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _session = session
    return _session


def reset_session() -> None:
    global _session
    if _session is not None:
        _session.close()
    _session = None


def _sleep_for_retry_after(response: requests.Response) -> None:
    header = response.headers.get("Retry-After")
    if not header:
        return
    try:
        delay = min(float(header), 30.0)
    except ValueError:
        return
    logger.info("action=rate_limited retry_after=%.1f url=%s", delay, response.url)
    time.sleep(delay)


def request(
    method: str,
    url: str,
    *,
    timeout: int | None = None,
    retries: int | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Perform an HTTP request with exponential backoff on transient errors."""
    settings = get_settings()
    timeout = timeout or settings.http_timeout
    attempts = retries if retries is not None else settings.http_retries

    @retry(
        stop=stop_after_attempt(max(1, attempts)),
        wait=wait_exponential(multiplier=1.5, min=1, max=20),
        retry=retry_if_exception_type(
            (TransientHTTPError, requests.ConnectionError, requests.Timeout)
        ),
        reraise=True,
    )
    def _do() -> requests.Response:
        response = get_session().request(method, url, timeout=timeout, **kwargs)
        if response.status_code in RETRYABLE_STATUS:
            if response.status_code == 429:
                _sleep_for_retry_after(response)
            raise TransientHTTPError(response.status_code, url)
        return response

    try:
        return _do()
    except (TransientHTTPError, requests.RequestException) as exc:
        raise SourceUnavailable(str(exc)) from exc


def get(url: str, **kwargs: Any) -> requests.Response:
    return request("GET", url, **kwargs)


def get_text(url: str, **kwargs: Any) -> str:
    response = get(url, **kwargs)
    response.raise_for_status()
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def get_json(url: str, **kwargs: Any) -> Any:
    response = get(url, **kwargs)
    response.raise_for_status()
    return response.json()


def head_or_get(url: str, **kwargs: Any) -> requests.Response:
    """HEAD with GET fallback (many CDNs answer 405 to HEAD)."""
    try:
        response = request("HEAD", url, allow_redirects=True, **kwargs)
        if response.status_code < 400:
            return response
    except SourceUnavailable:
        pass
    return request(
        "GET", url, allow_redirects=True, headers={"Range": "bytes=0-2047"}, **kwargs
    )
