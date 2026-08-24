"""Leejam Sports Company (Saudi Exchange, ticker 1830).

Two official sources feeding ONE event stream:

    1. Saudi Exchange issuer announcements (primary detection). Allowlist:
       "Interim Consolidated Financial Results" / "Annual Consolidated
       Financial Results". Dividends, centre openings, meetings and contracts
       are ignored.
    2. Leejam IR Result Center (secondary, enrichment).

Logical key is company + reporting_period. The period result - not each file -
is the event: Financial Statements, Results Release and Earnings Presentation
for the same period are one alert. If the second source confirms a period
already alerted, the existing record is enriched and no email is sent.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import (
    canonical_url,
    normalize_year,
    parse_date,
    slug_title,
    squash,
)
from .base import (
    CompanyMonitor,
    EndpointProbeMixin,
    HTMLSourceMixin,
    ParserFailure,
    PlaywrightFallbackMixin,
    candidate,
)

logger = logging.getLogger(__name__)

SOURCE_TADAWUL = "saudi_exchange_announcements"
SOURCE_IR = "leejam_result_center"

DEFAULT_TADAWUL_URL = (
    "https://www.saudiexchange.sa/wps/portal/saudiexchange/newsandreports/"
    "issuer-news/company-announcement?companySymbol=1830"
)
DEFAULT_IR_URL = "https://leejam.com.sa/investor-relations/result-center-and-reports/"

RESULTS_ALLOWLIST = [
    re.compile(r"\binterim\s+consolidated\s+financial\s+results\b"),
    re.compile(r"\bannual\s+consolidated\s+financial\s+results\b"),
]
IGNORE_RE = re.compile(
    r"\b(dividend|board\s+of\s+directors|general\s+assembly|shareholders?\s+meeting|"
    r"contract|opening\s+of|new\s+center|zakat|sukuk|capital\s+increase|"
    r"resignation|appointment|invitation)\b"
)

PERIOD_END_RE = re.compile(r"(20\d{2})[-/](\d{2})[-/](\d{2})")
QUARTER_WORD_RE = re.compile(r"\b([1-4])(?:st|nd|rd|th)\s+quarter\b")
ANNUAL_RE = re.compile(r"\bannual\b")

# Month of period end -> normalized reporting period label
_MONTH_END_TO_PERIOD = {3: "Q1", 6: "Q2/H1", 9: "Q3/9M", 12: "Q4/FY"}
_QUARTER_TO_LABEL = {1: "Q1", 2: "Q2/H1", 3: "Q3/9M", 4: "Q4/FY"}

DOC_LABELS = {
    "financial_statements": ("financial statement", "financial statements", "consolidated financial"),
    "presentation": ("presentation", "earnings presentation"),
    "results_release": ("results release", "press release", "earnings release"),
    "transcript": ("transcript", "recording"),
}


class LeejamMonitor(
    EndpointProbeMixin, HTMLSourceMixin, PlaywrightFallbackMixin, CompanyMonitor
):
    key = "leejam"
    # Either source alone is enough; both failing is a parser failure.
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        items: list[CandidateEvent] = []
        used: list[str] = []

        try:
            tadawul = self.fetch_tadawul()
            if tadawul:
                items.extend(tadawul)
                used.append(SOURCE_TADAWUL)
        except Exception as exc:  # noqa: BLE001 - the IR site may still answer
            logger.info("company=%s source=tadawul error=%s", self.key, exc)

        try:
            ir_items = self.fetch_result_center()
            if ir_items:
                items.extend(ir_items)
                used.append(SOURCE_IR)
        except Exception as exc:  # noqa: BLE001
            logger.info("company=%s source=result_center error=%s", self.key, exc)

        if not items:
            raise ParserFailure(
                "leejam: neither Saudi Exchange announcements nor the IR Result "
                "Center produced items"
            )
        self.source_used = "+".join(used)
        return items

    # ------------------------------------------------------------------
    def fetch_tadawul(self) -> list[CandidateEvent]:
        endpoints = self.config.option("tadawul_candidate_endpoints", []) or []
        payload = self.probe_endpoints(endpoints, _looks_like_announcement_payload)
        if payload is not None:
            return self.parse_tadawul_payload(payload)

        url = self.config.option("tadawul_url", DEFAULT_TADAWUL_URL)
        html = http.get_text(url)
        return self.parse_tadawul_html(html, url)

    def parse_tadawul_payload(self, payload: Any) -> list[CandidateEvent]:
        out: list[CandidateEvent] = []
        for row in _extract_rows(payload):
            title = squash(
                str(row.get("announcementTitle") or row.get("title") or row.get("subject") or "")
            )
            if not title:
                continue
            out.append(
                candidate(
                    self.key,
                    SOURCE_TADAWUL,
                    title,
                    url=row.get("url") or row.get("link") or row.get("announcementUrl"),
                    publication_date=parse_date(
                        str(row.get("announcementDate") or row.get("date") or "")
                    ),
                    raw_row=row,
                )
            )
        return out

    def parse_tadawul_html(self, html: str, base_url: str) -> list[CandidateEvent]:
        soup = self.soup_from(html)
        out: list[CandidateEvent] = []
        seen: set[str] = set()
        for text, url, anchor in self.iter_links(soup, base_url):
            low = slug_title(text)
            if len(low) < 15:
                continue
            if not any(p.search(low) for p in RESULTS_ALLOWLIST):
                continue
            if url in seen:
                continue
            seen.add(url)
            block = _block_text(anchor)
            out.append(
                candidate(
                    self.key,
                    SOURCE_TADAWUL,
                    text,
                    url=url,
                    publication_date=parse_date(block[:120]),
                    context=block,
                )
            )
        return out

    # ------------------------------------------------------------------
    def fetch_result_center(self) -> list[CandidateEvent]:
        url = self.config.option("ir_url", DEFAULT_IR_URL)
        try:
            html = http.get_text(url)
            items = self.parse_result_center(html, url)
            if items:
                return items
        except Exception as exc:  # noqa: BLE001
            logger.info("company=%s action=ir_static_failed error=%s", self.key, exc)
        html = self.render_html(url)
        return self.parse_result_center(html, url)

    def parse_result_center(self, html: str, base_url: str) -> list[CandidateEvent]:
        soup = self.soup_from(html)
        buckets: dict[str, dict] = {}
        for text, url, anchor in self.iter_links(soup, base_url):
            block = _block_text(anchor)
            period = leejam_period(f"{text} {block}") or leejam_period(url)
            if not period:
                continue
            low = slug_title(f"{text} {url}")
            if not (url.lower().endswith(".pdf") or "download" in low or "report" in low):
                continue
            label = _document_label(f"{text} {url}")
            bucket = buckets.setdefault(
                period, {"links": {}, "date": parse_date(block[:120])}
            )
            bucket["links"].setdefault(label, url)

        return [
            candidate(
                self.key,
                SOURCE_IR,
                f"Leejam {period} Results",
                url=bucket["links"].get("financial_statements")
                or next(iter(bucket["links"].values()), None),
                publication_date=bucket["date"],
                period=period,
                links=bucket["links"],
            )
            for period, bucket in buckets.items()
        ]

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        if cand.source == SOURCE_IR:
            return EventType.QUARTERLY_RESULTS
        low = slug_title(cand.title)
        if IGNORE_RE.search(low):
            return None
        if any(pattern.search(low) for pattern in RESULTS_ALLOWLIST):
            return EventType.QUARTERLY_RESULTS
        return None

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        period = cand.raw.get("period") or leejam_period(
            f"{cand.title} {cand.raw.get('context', '')}"
        )
        if not period:
            return None
        links = cand.raw.get("links", {})
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=cand.title,
            source=cand.source,
            publication_date=cand.publication_date,
            primary_url=cand.url,
            document_url=links.get("financial_statements") or cand.url,
            document_identifier=canonical_url(cand.url),
            financial_statements_url=links.get("financial_statements"),
            earnings_presentation_url=links.get("presentation"),
            results_release_url=links.get("results_release"),
            transcript_url=links.get("transcript"),
            saudi_exchange_announcement_url=(
                cand.url if cand.source == SOURCE_TADAWUL else None
            ),
            issuer="Leejam Sports Company",
            ticker="1830",
            key_includes_event_type=False,
        )


def leejam_period(text: str) -> str | None:
    """Normalize to Q1-YYYY, Q2/H1-YYYY, Q3/9M-YYYY or Q4/FY-YYYY."""
    if not text:
        return None
    low = slug_title(text)

    # Announcements name the period end date, e.g. "... Ending on 2026-03-31".
    match = PERIOD_END_RE.search(low)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        label = _MONTH_END_TO_PERIOD.get(month)
        if label:
            return f"{label}-{year}"

    year_match = re.search(r"\b(20\d{2})\b", low)
    year = int(year_match.group(1)) if year_match else None

    quarter_match = QUARTER_WORD_RE.search(low) or re.search(r"\bq([1-4])\b", low)
    if quarter_match and year:
        quarter = int(quarter_match.group(1))
        return f"{_QUARTER_TO_LABEL[quarter]}-{year}"
    if ANNUAL_RE.search(low) and year:
        return f"Q4/FY-{year}"
    return None


def _document_label(text: str) -> str:
    low = slug_title(text)
    for label, needles in DOC_LABELS.items():
        if any(needle in low for needle in needles):
            return label
    return "other"


def _looks_like_announcement_payload(payload: Any) -> bool:
    rows = _extract_rows(payload)
    if not rows:
        return False
    keys = {k.lower() for row in rows[:5] for k in row}
    return bool(keys & {"announcementtitle", "title", "subject"})


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "announcements", "records", "content"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
            if isinstance(value, dict):
                nested = _extract_rows(value)
                if nested:
                    return nested
    return []


def _block_text(anchor, max_levels: int = 5) -> str:
    node = anchor
    best = ""
    for _ in range(max_levels):
        node = node.parent
        if node is None:
            break
        text = squash(node.get_text(" ", strip=True))
        if len(text) > len(best):
            best = text
        if len(best) > 100:
            break
    return best[:600]
