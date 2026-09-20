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

import logging
import re
from datetime import date

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import canonical_url, parse_date, slug_title, squash
from .base import CompanyMonitor, HTMLSourceMixin, ParserFailure, candidate

logger = logging.getLogger(__name__)

SOURCE_HTML = "tgg_results_reports_html"
SOURCE_PRESS = "tgg_press_releases_html"

DEFAULT_URL = "https://www.tggplc.com/investors/results-reports-and-presentations/"
DEFAULT_PRESS_URL = "https://www.tggplc.com/investors/news/"

# Rejected first, unconditionally.
NOTICE_RE = re.compile(r"^\s*notice\s+of\b")

# Not anchored to the start of the title: real link text on the results page
# routinely carries a date/filetype prefix or a trailing suffix (e.g. "07 Mar
# 2025 - Full Year Results" or "Full Year Results 2025.pdf"), and NOTICE_RE
# above already rejects "Notice of ..." titles before these ever run, so a
# leading-anchor here only causes false negatives, not false positives.
PRE_CLOSE_RE = re.compile(r"\bpre-?\s*close\s+trading\s+update\b")
FULL_YEAR_RE = re.compile(r"\bfull\s+year\s+results\b")
INTERIM_RE = re.compile(r"\binterim\s+results\b")

IGNORE_RE = re.compile(
    r"\b(annual\s+report(\s+and\s+accounts)?|site\s+visit|capital\s+markets\s+day|"
    r"presentation|webcast|transcript|agm|circular|prospectus)\b"
)

YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Confirmed via a live inspect-validate DEBUG dump: every real link's own
# visible text and its DOM context are both just "Download PDF (461.22 kb)"
# or "View presentation (2.35 mb)" - none of the phrases classify_tgg_title
# looks for ("Full Year Results", "Interim Results", ...) are anywhere in
# the page's text at all. They are, however, encoded in the file's own name
# ("rns-hy26-final.pdf", "rns-fy25-final.pdf",
# "pre-close-trading-statement-jul-26-final.pdf",
# "full-year-results-mar-2026-presentation.pdf"), which is what
# tgg_period_from_filename() below reads instead.
_FILENAME_RNS_HY_RE = re.compile(r"\brns-hy(\d{2})\b")
_FILENAME_RNS_FY_RE = re.compile(r"\brns-fy(\d{2})\b")
_FILENAME_HALF_YEAR_RE = re.compile(r"\bhalf-year-results\b")
_FILENAME_FULL_YEAR_RE = re.compile(r"\bfull-year-results\b")
_FILENAME_PRE_CLOSE_RE = re.compile(
    r"\bpre-close-trading-statement-([a-z]{3})-(\d{2})\b"
)
# Real, but not financial-results, PDFs seen on the same page (a gender pay
# gap report, a "Gen Z fitness pulse" survey, the Annual Report/Accounts
# document, a site-visit deck) - excluded so filename matching never turns
# every PDF on the page into a candidate.
_FILENAME_EXCLUDE_RE = re.compile(
    r"\bannual-report\b|\bsite-visit\b|\bgen-z\b|\bpay-gap\b|\bsurvey-report\b|"
    r"\bfitness-pulse\b"
)
_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def tgg_period_from_filename(url: str | None) -> tuple[str, str] | None:
    """(event_type, reporting_period), read from the document's own filename."""
    if not url:
        return None
    filename = slug_title(url.rsplit("/", 1)[-1])
    if _FILENAME_EXCLUDE_RE.search(filename):
        return None

    match = _FILENAME_RNS_HY_RE.search(filename)
    if match:
        return EventType.INTERIM_RESULTS, f"H1-{2000 + int(match.group(1))}"
    match = _FILENAME_RNS_FY_RE.search(filename)
    if match:
        return EventType.FULL_YEAR_RESULTS, f"FY-{2000 + int(match.group(1))}"

    match = _FILENAME_PRE_CLOSE_RE.search(filename)
    if match:
        month = _MONTH_ABBR.get(match.group(1))
        year = 2000 + int(match.group(2))
        if month:
            if month <= 4:
                return EventType.PRE_CLOSE_TRADING_UPDATE, f"FY_PRE_CLOSE-{year - 1}"
            return EventType.PRE_CLOSE_TRADING_UPDATE, f"H1_PRE_CLOSE-{year}"

    year_match = YEAR_RE.search(filename)
    if _FILENAME_HALF_YEAR_RE.search(filename) and year_match:
        return EventType.INTERIM_RESULTS, f"H1-{year_match.group(1)}"
    if _FILENAME_FULL_YEAR_RE.search(filename) and year_match:
        year = int(year_match.group(1))
        # "full-year-results-mar-2026-presentation" reports FY2025 (results
        # published in March report the prior fiscal year), matching
        # tgg_period()'s own March-publication rule below.
        month_match = re.search(
            r"-(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)-", filename
        )
        if month_match and _MONTH_ABBR[month_match.group(1)] <= 4:
            year -= 1
        return EventType.FULL_YEAR_RESULTS, f"FY-{year}"
    return None


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
        # TEMP DEBUG - remove before merging. The first 20 links were all
        # generic site nav; PDF-like or digit-bearing ones are more likely
        # to be actual report links, so surface those specifically too.
        logger.info("DEBUG company=%s total_candidates=%d", self.key, len(out))
        interesting = [
            c for c in out
            if c.url.lower().endswith(".pdf") or any(ch.isdigit() for ch in c.title)
        ]
        for c in interesting[:40]:
            logger.info(
                "DEBUG company=%s title=%r context=%r url=%s",
                self.key, c.title, c.raw.get("context", ""), c.url,
            )
        return out

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        # Filename first (see tgg_period_from_filename()'s docstring above):
        # confirmed via a live run to be the only signal actually present
        # for real documents on this page.
        by_filename = tgg_period_from_filename(cand.document_url or cand.url)
        if by_filename:
            return by_filename[0]
        # parse_results_page() captures the surrounding block as `context`
        # (used by normalize()/tgg_period() below to find the publication
        # date) precisely because the anchor's own text is often generic
        # ("Download", "PDF", a bare date) while the qualifying phrase
        # ("Full Year Results", "Interim Results", ...) sits in a nearby
        # heading. classify() was still title-only, so those candidates
        # were extracted (82 of them) but never matched here (0 relevant).
        return classify_tgg_title(f"{cand.title} {cand.raw.get('context', '')}")

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        published = cand.publication_date or parse_date(cand.raw.get("context", ""))
        by_filename = tgg_period_from_filename(cand.document_url or cand.url)
        if by_filename and by_filename[0] == event_type:
            period = by_filename[1]
        else:
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
