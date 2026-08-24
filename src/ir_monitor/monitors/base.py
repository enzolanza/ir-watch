"""Base interface for company monitors plus reusable building blocks.

Every adapter follows the same four-step contract:

    fetch_candidates() -> classify() -> normalize() -> validate()

Company specific logic lives in the adapter. Anything shared (RSS parsing,
static HTML link extraction, Playwright fallback) lives in the mixins below.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import date
from typing import Any, Iterable, Sequence
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import http
from ..config import CompanyConfig
from ..models import CandidateEvent, NormalizedEvent
from ..normalization import canonical_url, parse_date, slug_title, squash

logger = logging.getLogger(__name__)


class ParserFailure(RuntimeError):
    """Raised when a source answers but the extraction produced nothing.

    Never treated as "no news". Disappearing content must never be a trigger,
    and an empty parse of a historically populated page is a red flag.
    """


class CompanyMonitor(ABC):
    """Base class for all company adapters."""

    key: str = ""
    name: str = ""

    #: A page that has always had records returning zero items is a parser
    #: failure, not silence. Adapters that legitimately can return zero
    #: (e.g. a probe-only secondary source) set this to 0.
    min_expected_candidates: int = 1

    def __init__(self, config: CompanyConfig):
        self.config = config
        self.key = config.key
        self.name = config.name
        self.source_used: str | None = None

    # -- contract ---------------------------------------------------------
    @abstractmethod
    def fetch_candidates(self) -> list[CandidateEvent]:
        """Return every item the source listed, unfiltered."""

    @abstractmethod
    def classify(self, candidate: CandidateEvent) -> str | None:
        """Return an event_type for relevant candidates, else None."""

    @abstractmethod
    def normalize(self, candidate: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        """Turn a relevant candidate into a NormalizedEvent."""

    def validate(self, event: NormalizedEvent) -> bool:
        """Last chance to reject an event. Default: require a period."""
        return bool(event.reporting_period and event.company and event.event_type)

    # -- orchestration ----------------------------------------------------
    def run(self) -> tuple[list[CandidateEvent], list[NormalizedEvent]]:
        candidates = self.fetch_candidates()
        logger.info(
            "company=%s action=fetch source=%s candidates=%d",
            self.key,
            self.source_used or "unknown",
            len(candidates),
        )
        if len(candidates) < self.min_expected_candidates:
            raise ParserFailure(
                f"{self.key}: expected at least {self.min_expected_candidates} "
                f"candidate(s), got {len(candidates)} - treating as parser/layout "
                f"failure rather than absence of disclosures"
            )

        events: list[NormalizedEvent] = []
        for candidate in candidates:
            event_type = self.classify(candidate)
            if not event_type:
                continue
            event = self.normalize(candidate, event_type)
            if event is None:
                continue
            if not self.validate(event):
                logger.debug(
                    "company=%s action=rejected title=%r", self.key, candidate.title
                )
                continue
            events.append(event)

        merged = merge_events(events)
        logger.info(
            "company=%s action=classify relevant=%d", self.key, len(merged)
        )
        return candidates, merged


# --------------------------------------------------------------------------
# Event merging (within one adapter run)
# --------------------------------------------------------------------------

_MERGEABLE_FIELDS = (
    "primary_url",
    "document_url",
    "presentation_url",
    "webcast_url",
    "transcript_url",
    "financial_statements_url",
    "pdf_url",
    "analyst_tool_url",
    "saudi_exchange_announcement_url",
    "results_release_url",
    "earnings_presentation_url",
    "guid",
    "release_id",
    "report_number",
    "issuer",
    "ticker",
    "publication_date",
    "document_identifier",
)


def merge_events(events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
    """Collapse events sharing an event_key into one enriched event.

    This is what makes "PDF + HTML", "EN + ES version" and "Financial
    Statements + Earnings Presentation" a single alert.
    """
    merged: dict[str, NormalizedEvent] = {}
    for event in events:
        existing = merged.get(event.event_key)
        if existing is None:
            merged[event.event_key] = event
            continue
        for field_name in _MERGEABLE_FIELDS:
            if getattr(existing, field_name) is None:
                value = getattr(event, field_name)
                if value is not None:
                    setattr(existing, field_name, value)
        existing.metadata.setdefault("merged_titles", [])
        if event.title and event.title != existing.title:
            existing.metadata["merged_titles"].append(event.title)
        for name, url in event.extra_links():
            existing.metadata.setdefault("related_links", {})
            existing.metadata["related_links"].setdefault(name, url)
    return list(merged.values())


# --------------------------------------------------------------------------
# Reusable source components
# --------------------------------------------------------------------------

class HTMLSourceMixin:
    """Helpers for adapters that parse static HTML."""

    def fetch_soup(self, url: str, **kwargs: Any) -> BeautifulSoup:
        html = http.get_text(url, **kwargs)
        return BeautifulSoup(html, "html.parser")

    @staticmethod
    def soup_from(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    @staticmethod
    def iter_links(
        soup: BeautifulSoup, base_url: str
    ) -> Iterable[tuple[str, str, Any]]:
        """Yield (text, absolute_url, tag) for every anchor with an href.

        Deliberately structure tolerant: adapters match on link text and href
        patterns rather than on CSS classes, which change often.
        """
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            text = squash(anchor.get_text(" ", strip=True))
            yield text, urljoin(base_url, href), anchor


class RSSSourceMixin:
    """Helpers for adapters backed by an RSS/Atom feed."""

    def fetch_feed_entries(self, url: str) -> list[dict[str, Any]]:
        import feedparser

        raw = http.get_text(url, headers={"Accept": "application/rss+xml, text/xml"})
        return self.parse_feed(raw)

    @staticmethod
    def parse_feed(raw: str) -> list[dict[str, Any]]:
        import feedparser

        parsed = feedparser.parse(raw)
        entries: list[dict[str, Any]] = []
        for entry in parsed.entries:
            published = None
            for attr in ("published", "updated", "pubDate"):
                if entry.get(attr):
                    published = parse_date(entry.get(attr)) or _parse_rfc822(
                        entry.get(attr)
                    )
                    if published:
                        break
            entries.append(
                {
                    "title": squash(entry.get("title", "")),
                    "link": entry.get("link"),
                    "guid": entry.get("id") or entry.get("guid") or entry.get("link"),
                    "published": published,
                    "summary": squash(entry.get("summary", "")),
                    "raw": dict(entry),
                }
            )
        return entries


def _parse_rfc822(value: str | None) -> date | None:
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


class PlaywrightFallbackMixin:
    """Renders a page with Playwright. Strictly a fallback path."""

    def render_html(
        self,
        url: str,
        *,
        wait_for: str | None = None,
        timeout_ms: int = 30_000,
    ) -> str:
        from ..config import get_settings

        if not get_settings().playwright_enabled:
            raise ParserFailure(
                f"{getattr(self, 'key', '?')}: Playwright fallback disabled "
                "(set PLAYWRIGHT_ENABLED=true to allow it)"
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ParserFailure(
                "Playwright is not installed; run `playwright install chromium`"
            ) from exc

        from ..config import USER_AGENT

        logger.info(
            "company=%s action=playwright_fallback url=%s",
            getattr(self, "key", "?"),
            url,
        )
        with sync_playwright() as pw:  # pragma: no cover - needs a browser
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                if wait_for:
                    page.wait_for_selector(wait_for, timeout=timeout_ms)
                return page.content()
            finally:
                browser.close()


class EndpointProbeMixin:
    """Tries a list of *candidate* JSON endpoints and validates the payload.

    The instructions forbid inventing APIs. Candidate endpoints therefore live
    in ``config/companies.yaml`` (empty by default), are validated against an
    adapter-supplied shape check, and any failure falls through to the
    documented fallback for that company.
    """

    def probe_endpoints(
        self,
        urls: Sequence[str],
        validator,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any | None:
        for url in urls:
            if not url:
                continue
            try:
                payload = http.get_json(url, headers=headers)
            except Exception as exc:  # noqa: BLE001 - endpoint may not exist
                logger.info(
                    "company=%s action=endpoint_probe_failed url=%s error=%s",
                    getattr(self, "key", "?"),
                    url,
                    type(exc).__name__,
                )
                continue
            try:
                if validator(payload):
                    logger.info(
                        "company=%s action=endpoint_probe_ok url=%s",
                        getattr(self, "key", "?"),
                        url,
                    )
                    return payload
            except Exception:  # noqa: BLE001
                continue
            logger.info(
                "company=%s action=endpoint_probe_shape_mismatch url=%s",
                getattr(self, "key", "?"),
                url,
            )
        return None


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------

def candidate(
    company: str,
    source: str,
    title: str,
    *,
    url: str | None = None,
    document_url: str | None = None,
    publication_date: date | None = None,
    **raw: Any,
) -> CandidateEvent:
    return CandidateEvent(
        company=company,
        source=source,
        title=squash(title),
        url=canonical_url(url),
        document_url=canonical_url(document_url),
        publication_date=publication_date,
        raw=raw,
    )


def title_matches_any(title: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    low = slug_title(title)
    return any(pattern.search(low) for pattern in patterns)


def with_source(event: NormalizedEvent, source: str) -> NormalizedEvent:
    return replace(event, source=source)
