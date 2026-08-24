"""Benefit Systems - Reports page (static HTML).

Two independent periodic flows generate alerts:

1. Consolidated group financial reports: FY, Q1, H1, Q3.
   There is no separate Q2 or Q4 financial report - Q2 is covered by H1 and Q4
   by the annual report. Standalone versions of FY/H1 are ignored so that the
   simultaneous consolidated + standalone publication produces one alert.

2. "Quarterly information on active sport cards' number" current reports,
   published early January / April / July / October for Q4 / Q1 / Q2 / Q3.

An allowlist is used rather than a blocklist: the page carries a large volume
of corporate and regulatory current reports.
"""

from __future__ import annotations

import re
from datetime import date

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import (
    canonical_url,
    normalize_year,
    parse_date,
    slug_title,
    squash,
)
from .base import CompanyMonitor, HTMLSourceMixin, ParserFailure, candidate

SOURCE_HTML = "benefit_systems_reports_html"

DEFAULT_URL = "https://corp.benefitsystems.pl/en/for-investors/reports/"

CONSOLIDATED_RE = re.compile(r"\bconsolidated\b")
STANDALONE_RE = re.compile(r"\b(standalone|stand-alone|separate)\b")

PERIODIC_RE = re.compile(
    r"\b(annual|full[-\s]?year|interim|semi-?annual|half[-\s]?year|quarterly|"
    r"q[1-4]|first\s+quarter|third\s+quarter)\b.*\breport\b"
    r"|\breport\b.*\b(annual|interim|semi-?annual|half[-\s]?year|quarterly|q[1-4])\b"
)

ACTIVE_CARDS_RE = re.compile(
    r"quarterly\s+information\s+on\s+active\s+sport\s+cards?'?s?\s+number"
)

Q_RE = re.compile(r"\bq([1-4])\b|\b([1-4])(?:st|nd|rd|th)\s+quarter\b")
H1_RE = re.compile(r"\b(h1|first\s+half|half[-\s]?year|semi-?annual|interim)\b")
FY_RE = re.compile(r"\b(annual|full[-\s]?year|fy)\b")
YEAR_RE = re.compile(r"\b(20\d{2})\b")
REPORT_NUMBER_RE = re.compile(r"\b(?:current\s+report|report)\s*(?:no\.?|nr\.?|#)\s*(\d+/\d{4})\b")

# Period covered by the active-cards report published in a given month.
_CARDS_MONTH_TO_PERIOD = {
    1: ("Q4", -1),
    2: ("Q4", -1),
    4: ("Q1", 0),
    5: ("Q1", 0),
    7: ("Q2", 0),
    8: ("Q2", 0),
    10: ("Q3", 0),
    11: ("Q3", 0),
}


class BenefitSystemsMonitor(HTMLSourceMixin, CompanyMonitor):
    key = "benefit_systems"
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        url = self.config.primary_url or DEFAULT_URL
        html = http.get_text(url)
        items = self.parse_reports_page(html, url)
        if not items:
            raise ParserFailure("benefit_systems: reports listing produced no items")
        self.source_used = SOURCE_HTML
        return items

    def parse_reports_page(self, html: str, base_url: str) -> list[CandidateEvent]:
        soup = self.soup_from(html)
        out: list[CandidateEvent] = []
        seen: set[str] = set()
        for text, url, anchor in self.iter_links(soup, base_url):
            if len(text) < 8:
                continue
            key = slug_title(text) + "|" + url
            if key in seen:
                continue
            seen.add(key)
            block = _block_text(anchor)
            out.append(
                candidate(
                    self.key,
                    SOURCE_HTML,
                    text,
                    url=url,
                    document_url=url if url.lower().endswith(".pdf") else None,
                    publication_date=parse_date(block[:120]) or _find_date(block),
                    context=block,
                )
            )
        return out

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        return classify_benefit_title(cand.title)

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        published = cand.publication_date
        if event_type == EventType.ACTIVE_SPORT_CARDS_UPDATE:
            period = active_cards_period(cand.title, cand.raw.get("context", ""), published)
        else:
            period = financial_report_period(cand.title, published)
        if not period:
            return None
        number = REPORT_NUMBER_RE.search(slug_title(cand.title))
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
            report_number=number.group(1) if number else None,
            issuer="Benefit Systems S.A.",
            ticker="BFT.WA",
        )


def classify_benefit_title(title: str) -> str | None:
    low = slug_title(title)
    if ACTIVE_CARDS_RE.search(low):
        return EventType.ACTIVE_SPORT_CARDS_UPDATE
    if not PERIODIC_RE.search(low):
        return None
    if STANDALONE_RE.search(low) and not CONSOLIDATED_RE.search(low):
        # Standalone FY/H1 duplicates the consolidated report. Never a trigger.
        return None
    if not CONSOLIDATED_RE.search(low):
        return None
    if FY_RE.search(low):
        return EventType.FULL_YEAR_RESULTS
    if H1_RE.search(low):
        return EventType.HALF_YEAR_RESULTS
    if Q_RE.search(low):
        return EventType.QUARTERLY_RESULTS
    return None


def financial_report_period(title: str, published: date | None) -> str | None:
    low = slug_title(title)
    year_match = YEAR_RE.search(low)
    year = int(year_match.group(1)) if year_match else (published.year if published else None)
    if year is None:
        return None

    if FY_RE.search(low):
        # An annual report published in year Y usually covers Y-1 unless the
        # title names the covered year explicitly.
        if year_match and published and year == published.year and published.month <= 6:
            return f"FY-{year - 1}"
        return f"FY-{year}"
    if H1_RE.search(low):
        return f"H1/Q2-{year}"
    q_match = Q_RE.search(low)
    if q_match:
        quarter = int(q_match.group(1) or q_match.group(2))
        if quarter == 2:
            return f"H1/Q2-{year}"
        if quarter == 4:
            return f"FY-{year}"
        return f"Q{quarter}-{year}"
    return None


def active_cards_period(title: str, context: str, published: date | None) -> str | None:
    """Prefer the period named in the title/body; fall back to the month."""
    haystack = slug_title(f"{title} {context}")
    q_match = Q_RE.search(haystack)
    year_match = YEAR_RE.search(haystack)
    if q_match and year_match:
        quarter = int(q_match.group(1) or q_match.group(2))
        return f"CARDS-Q{quarter}-{normalize_year(year_match.group(1))}"
    if published:
        mapping = _CARDS_MONTH_TO_PERIOD.get(published.month)
        if mapping:
            label, offset = mapping
            return f"CARDS-{label}-{published.year + offset}"
    return None


def _block_text(anchor, max_levels: int = 4) -> str:
    node = anchor
    best = ""
    for _ in range(max_levels):
        node = node.parent
        if node is None:
            break
        text = squash(node.get_text(" ", strip=True))
        if len(text) > len(best):
            best = text
        if len(best) > 60:
            break
    return best[:500]


def _find_date(text: str) -> date | None:
    for match in re.finditer(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{2}-\d{2}", text):
        parsed = parse_date(match.group(0))
        if parsed:
            return parsed
    return None
