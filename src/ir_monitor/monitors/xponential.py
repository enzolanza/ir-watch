"""Xponential Fitness - Quarterly Results page (static HTML).

Only links explicitly labelled "Earnings Release" are relevant. Webcast, audio,
10-Q/10-K, XBRL, presentations and annual reports are ignored.

Parsing strategy: rather than depending on CSS classes (which change), the
adapter walks anchors whose *text* identifies the document type and resolves
the reporting period from the nearest enclosing block that carries a period
label ("Q1 2026", "First Quarter 2026", "FY 2025").
"""

from __future__ import annotations

import logging
import re

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import (
    canonical_url,
    extract_year,
    normalize_year,
    quarter_period,
    slug_title,
    squash,
)
from .base import (
    CompanyMonitor,
    HTMLSourceMixin,
    ParserFailure,
    RSSSourceMixin,
    candidate,
)

SOURCE_HTML = "xponential_quarterly_results_html"
SOURCE_RSS = "xponential_news_rss"

DEFAULT_URL = "https://investor.xponential.com/financial-information/quarterly-results"
DEFAULT_RSS = "https://investor.xponential.com/news/rss"

EARNINGS_RELEASE_LABEL = re.compile(r"\bearnings\s+release\b")
IGNORE_LABELS = re.compile(
    r"\b(webcast|audio|10-?q|10-?k|xbrl|presentation|annual\s+report|transcript|"
    r"proxy|8-?k|supplement)\b"
)

PERIOD_Q_RE = re.compile(r"\bq([1-4])\s*(20\d{2})\b")
PERIOD_WORD_RE = re.compile(
    r"\b(first|second|third|fourth)\s+quarter\s+(20\d{2})\b"
)
PERIOD_FY_RE = re.compile(r"\b(?:fy|full\s+year)\s*(20\d{2})\b")
RELEASE_ID_RE = re.compile(r"/news/detail/(\d+)/")

WORD_TO_Q = {"first": 1, "second": 2, "third": 3, "fourth": 4}

# RSS fallback: real results vs. date pre-announcement.
RSS_ACCEPT_RE = re.compile(r"\bannounce[sd]\b.*\bfinancial\s+results\b")
RSS_REJECT_RE = re.compile(r"\bto\s+announce\b|\bto\s+report\b|\bwill\s+report\b")

logger = logging.getLogger(__name__)


class XponentialMonitor(HTMLSourceMixin, RSSSourceMixin, CompanyMonitor):
    key = "xponential"
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        url = self.config.primary_url or DEFAULT_URL
        try:
            html = http.get_text(url)
            candidates = self.parse_quarterly_page(html, url)
            if candidates:
                self.source_used = SOURCE_HTML
                return candidates
        except Exception as exc:  # noqa: BLE001 - fall through to RSS fallback
            logger.info("company=%s action=html_failed error=%s", self.key, exc)

        # Documented fallback: official news RSS.
        entries = self.fetch_feed_entries(self.config.option("rss_url", DEFAULT_RSS))
        if not entries:
            raise ParserFailure("xponential: quarterly page and RSS both empty")
        self.source_used = SOURCE_RSS
        return [
            candidate(
                self.key,
                SOURCE_RSS,
                entry["title"],
                url=entry.get("link"),
                publication_date=entry.get("published"),
                guid=entry.get("guid"),
                kind="rss",
            )
            for entry in entries
        ]

    # ------------------------------------------------------------------
    def parse_quarterly_page(self, html: str, base_url: str) -> list[CandidateEvent]:
        soup = self.soup_from(html)
        out: list[CandidateEvent] = []
        for text, url, anchor in self.iter_links(soup, base_url):
            low = slug_title(text)
            if not EARNINGS_RELEASE_LABEL.search(low):
                continue
            if IGNORE_LABELS.search(low):
                continue
            period_source = _nearest_period_text(anchor) or text
            out.append(
                candidate(
                    self.key,
                    SOURCE_HTML,
                    f"Earnings Release - {squash(period_source)}",
                    url=url,
                    document_url=url if url.lower().endswith(".pdf") else None,
                    period_text=period_source,
                    link_text=text,
                    kind="html",
                )
            )
        return out

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        if cand.raw.get("kind") == "rss":
            low = slug_title(cand.title)
            if RSS_REJECT_RE.search(low):
                return None
            if RSS_ACCEPT_RE.search(low):
                return EventType.EARNINGS_RELEASE
            return None
        return EventType.EARNINGS_RELEASE

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        text = cand.raw.get("period_text") or cand.title
        period = xponential_period(text) or xponential_period(cand.url or "")
        if not period:
            return None
        release_id = None
        if cand.url:
            match = RELEASE_ID_RE.search(cand.url)
            if match:
                release_id = match.group(1)
        is_pdf = bool(cand.url and cand.url.lower().endswith(".pdf"))
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=cand.title,
            source=cand.source,
            publication_date=cand.publication_date,
            primary_url=cand.url,
            document_url=cand.url,
            pdf_url=cand.url if is_pdf else None,
            document_identifier=canonical_url(cand.url),
            release_id=release_id,
            ticker="XPOF",
            issuer="Xponential Fitness, Inc.",
        )


def xponential_period(text: str) -> str | None:
    low = slug_title(text)
    match = PERIOD_Q_RE.search(low)
    if match:
        return quarter_period(int(match.group(1)), normalize_year(match.group(2)))
    match = PERIOD_WORD_RE.search(low)
    if match:
        return quarter_period(WORD_TO_Q[match.group(1)], normalize_year(match.group(2)))
    match = PERIOD_FY_RE.search(low)
    if match:
        # The site labels the fourth quarter block as "FY YYYY"; internally
        # this is the combined Q4/FY event.
        return quarter_period(4, normalize_year(match.group(1)))
    year = extract_year(low)
    if year and re.search(r"\bfourth\b|\bfull\s+year\b", low):
        return quarter_period(4, year)
    return None


def _nearest_period_text(anchor, max_levels: int = 6) -> str | None:
    """Walk up the DOM looking for a block whose text carries a period label."""
    node = anchor
    for _ in range(max_levels):
        node = node.parent
        if node is None:
            return None
        text = squash(node.get_text(" ", strip=True))
        if not text:
            continue
        low = slug_title(text)
        if PERIOD_Q_RE.search(low) or PERIOD_WORD_RE.search(low) or PERIOD_FY_RE.search(low):
            # Keep it short: return the first matching fragment, not the whole
            # page text.
            for pattern in (PERIOD_Q_RE, PERIOD_WORD_RE, PERIOD_FY_RE):
                found = pattern.search(low)
                if found:
                    return found.group(0)
    return None
