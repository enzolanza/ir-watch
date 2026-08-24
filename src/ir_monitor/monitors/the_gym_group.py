"""The Gym Group - Results, reports and presentations page (static HTML).

Four recurring disclosures are relevant:

    January   Pre-close trading update   -> FY_PRE_CLOSE-(YYYY-1)
    March     Full Year Results          -> FY-(YYYY-1)
    July      Pre-close trading update   -> H1_PRE_CLOSE-YYYY
    September Interim Results            -> H1-YYYY

The critical false positive is "Notice of Pre-Close Trading Update", which only
announces the date of a future update. Rejection of any "Notice of ..." title
happens before any acceptance rule, so plain substring matching never applies.
"""

from __future__ import annotations

import re
from datetime import date

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import canonical_url, parse_date, slug_title, squash
from .base import CompanyMonitor, HTMLSourceMixin, ParserFailure, candidate

SOURCE_HTML = "tgg_results_reports_html"
SOURCE_PRESS = "tgg_press_releases_html"

DEFAULT_URL = "https://www.tggplc.com/investors/results-reports-and-presentations/"
DEFAULT_PRESS_URL = "https://www.tggplc.com/investors/news/"

# Rejected first, unconditionally.
NOTICE_RE = re.compile(r"^\s*notice\s+of\b")

PRE_CLOSE_RE = re.compile(r"^pre-?\s*close\s+trading\s+update\b")
FULL_YEAR_RE = re.compile(r"^full\s+year\s+results\b")
INTERIM_RE = re.compile(r"^interim\s+results\b")

IGNORE_RE = re.compile(
    r"\b(annual\s+report(\s+and\s+accounts)?|site\s+visit|capital\s+markets\s+day|"
    r"presentation|webcast|transcript|agm|circular|prospectus)\b"
)

YEAR_RE = re.compile(r"\b(20\d{2})\b")


class TheGymGroupMonitor(HTMLSourceMixin, CompanyMonitor):
    key = "the_gym_group"
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        url = self.config.primary_url or DEFAULT_URL
        try:
            html = http.get_text(url)
            items = self.parse_results_page(html, url)
            if items:
                self.source_used = SOURCE_HTML
                return items
        except Exception:  # noqa: BLE001 - fall back to press releases
            pass

        press_url = self.config.option("press_url", DEFAULT_PRESS_URL)
        html = http.get_text(press_url)
        items = self.parse_results_page(html, press_url, source=SOURCE_PRESS)
        if not items:
            raise ParserFailure(
                "the_gym_group: neither results page nor press releases yielded items"
            )
        self.source_used = SOURCE_PRESS
        return items

    # ------------------------------------------------------------------
    def parse_results_page(
        self, html: str, base_url: str, source: str = SOURCE_HTML
    ) -> list[CandidateEvent]:
        soup = self.soup_from(html)
        out: list[CandidateEvent] = []
        seen: set[str] = set()
        for text, url, anchor in self.iter_links(soup, base_url):
            if not text or len(text) < 5:
                continue
            key = f"{slug_title(text)}|{url}"
            if key in seen:
                continue
            seen.add(key)
            out.append(
                candidate(
                    self.key,
                    source,
                    text,
                    url=url,
                    document_url=url if url.lower().endswith(".pdf") else None,
                    publication_date=_nearby_date(anchor),
                    context=_context_text(anchor),
                )
            )
        return out

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        return classify_tgg_title(cand.title)

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        published = cand.publication_date or parse_date(cand.raw.get("context", ""))
        period = tgg_period(event_type, cand.title, published)
        if not period:
            return None
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=cand.title,
            source=cand.source,
            publication_date=published,
            primary_url=cand.url,
            document_url=cand.document_url or cand.url,
            pdf_url=cand.document_url,
            document_identifier=canonical_url(cand.url),
            issuer="The Gym Group plc",
            ticker="GYM.L",
        )


def classify_tgg_title(title: str) -> str | None:
    low = slug_title(title)
    if NOTICE_RE.search(low):
        return None
    if IGNORE_RE.search(low):
        return None
    if PRE_CLOSE_RE.search(low):
        return EventType.PRE_CLOSE_TRADING_UPDATE
    if FULL_YEAR_RE.search(low):
        return EventType.FULL_YEAR_RESULTS
    if INTERIM_RE.search(low):
        return EventType.INTERIM_RESULTS
    return None


def tgg_period(event_type: str, title: str, published: date | None) -> str | None:
    """Map an event to its reporting period using the publication month."""
    explicit_year = YEAR_RE.search(slug_title(title))
    year = int(explicit_year.group(1)) if explicit_year else None
    month = published.month if published else None
    if year is None:
        if published is None:
            return None
        year = published.year

    if event_type == EventType.FULL_YEAR_RESULTS:
        # Published in March, reporting the previous financial year, unless the
        # title itself names the year (e.g. "Full Year Results 2025").
        if explicit_year:
            return f"FY-{year}"
        return f"FY-{year - 1}"

    if event_type == EventType.INTERIM_RESULTS:
        return f"H1-{year}"

    if event_type == EventType.PRE_CLOSE_TRADING_UPDATE:
        if month is None:
            return None
        if month <= 4:
            return f"FY_PRE_CLOSE-{year - 1}"
        return f"H1_PRE_CLOSE-{year}"

    return None


def _context_text(anchor, max_levels: int = 4) -> str:
    node = anchor
    for _ in range(max_levels):
        node = node.parent
        if node is None:
            break
        text = squash(node.get_text(" ", strip=True))
        if len(text) > 20:
            return text[:400]
    return ""


def _nearby_date(anchor) -> date | None:
    node = anchor
    for _ in range(5):
        node = node.parent
        if node is None:
            return None
        time_tag = node.find("time")
        if time_tag is not None:
            parsed = parse_date(time_tag.get("datetime") or time_tag.get_text(strip=True))
            if parsed:
                return parsed
        parsed = parse_date(squash(node.get_text(" ", strip=True))[:80])
        if parsed:
            return parsed
    return None
