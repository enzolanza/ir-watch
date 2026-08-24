"""PureGym / Pinnacle Bidco plc - quarterly investor reports.

Trigger: the quarterly *Report*. Presentation, webcast and transcript are stored
as extra links but never generate an alert on their own. Annual Reports are
ignored so they cannot produce a second alert after the Q4/FY results.

Source priority:

    1. structured Q4 Inc. backend endpoint (candidate URLs configured in
       companies.yaml - the code never fabricates one)
    2. the results/reports page over plain HTTP
    3. the "Latest Results" block of the Investor Overview page
    4. Playwright rendering

Logical key is company + reporting_period (no event_type), so the Report and a
later republication of the same period converge on one event.
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
    quarter_period,
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

SOURCE_ENDPOINT = "puregym_q4_endpoint"
SOURCE_RESULTS_PAGE = "puregym_results_page_html"
SOURCE_LATEST = "puregym_latest_results_html"
SOURCE_RENDERED = "puregym_results_rendered"

DEFAULT_URL = "https://corporate.puregym.com/investors/results-reports-and-presentations/default.aspx"
LEGACY_URL = "https://corporate.puregym.com/investor/financial-results/quarterly-results/default.aspx"
DEFAULT_OVERVIEW_URL = "https://corporate.puregym.com/investors/default.aspx"

REPORT_LABEL_RE = re.compile(r"\breport\b")
ANNUAL_REPORT_RE = re.compile(r"\bannual\s+report\b")
IGNORE_LABEL_RE = re.compile(
    r"\b(annual\s+report|presentation|webcast|transcript|audio|slides|"
    r"bond|indenture|covenant|prospectus)\b"
)

PERIOD_Q_RE = re.compile(r"\bq([1-4])\s*(?:20)?(\d{2})\b")
PERIOD_FY_RE = re.compile(r"\b(?:fy|full[-\s]?year)\s*(?:20)?(\d{2})\b")
PERIOD_PATH_RE = re.compile(r"/doc_financials/(20\d{2})/(q[1-4])/", re.IGNORECASE)

LABELS = {
    "report": ("report",),
    "presentation": ("presentation", "slides"),
    "webcast": ("webcast", "audio", "replay"),
    "transcript": ("transcript",),
}


class PureGymMonitor(
    EndpointProbeMixin, HTMLSourceMixin, PlaywrightFallbackMixin, CompanyMonitor
):
    key = "puregym"
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        endpoints = self.config.option("candidate_endpoints", []) or []
        payload = self.probe_endpoints(endpoints, _looks_like_q4_payload)
        if payload is not None:
            self.source_used = SOURCE_ENDPOINT
            return self.parse_endpoint_payload(payload)

        for url in self._page_urls():
            try:
                html = http.get_text(url)
            except Exception as exc:  # noqa: BLE001
                logger.info("company=%s action=page_failed url=%s error=%s", self.key, url, exc)
                continue
            items = self.parse_results_html(html, url, SOURCE_RESULTS_PAGE)
            if items:
                self.source_used = SOURCE_RESULTS_PAGE
                return items

        overview = self.config.option("overview_url", DEFAULT_OVERVIEW_URL)
        try:
            html = http.get_text(overview)
            items = self.parse_results_html(html, overview, SOURCE_LATEST)
            if items:
                self.source_used = SOURCE_LATEST
                return items
        except Exception as exc:  # noqa: BLE001
            logger.info("company=%s action=overview_failed error=%s", self.key, exc)

        html = self.render_html(self._page_urls()[0])
        items = self.parse_results_html(html, self._page_urls()[0], SOURCE_RENDERED)
        if not items:
            raise ParserFailure("puregym: no source produced quarterly reports")
        self.source_used = SOURCE_RENDERED
        return items

    def _page_urls(self) -> list[str]:
        urls = [self.config.primary_url or DEFAULT_URL]
        for extra in (DEFAULT_URL, LEGACY_URL):
            if extra not in urls:
                urls.append(extra)
        return urls

    # ------------------------------------------------------------------
    def parse_endpoint_payload(self, payload: Any) -> list[CandidateEvent]:
        rows = _extract_rows(payload)
        out: list[CandidateEvent] = []
        for row in rows:
            title = squash(str(row.get("ReportTitle") or row.get("title") or ""))
            year = row.get("ReportYear") or row.get("year")
            subtype = squash(str(row.get("ReportSubType") or row.get("subtype") or ""))
            url = (
                row.get("DocumentPath")
                or row.get("documentUrl")
                or row.get("url")
                or row.get("link")
            )
            if not (title or subtype):
                continue
            out.append(
                candidate(
                    self.key,
                    SOURCE_ENDPOINT,
                    f"{subtype} {year or ''} {title}".strip(),
                    url=url,
                    document_url=url,
                    publication_date=parse_date(
                        str(row.get("ReportDate") or row.get("date") or "")
                    ),
                    label="report",
                    raw_row=row,
                )
            )
        return out

    def parse_results_html(
        self, html: str, base_url: str, source: str
    ) -> list[CandidateEvent]:
        """Group links by the period block they belong to."""
        soup = self.soup_from(html)
        buckets: dict[str, dict] = {}
        for text, url, anchor in self.iter_links(soup, base_url):
            low = slug_title(text)
            if ANNUAL_REPORT_RE.search(low):
                continue
            period = (
                puregym_period(url)
                or puregym_period(text)
                or puregym_period(_block_text(anchor))
            )
            if not period:
                continue
            label = _label_for(text, url)
            bucket = buckets.setdefault(
                period,
                {"links": {}, "date": parse_date(_block_text(anchor)[:120]), "title": period},
            )
            bucket["links"].setdefault(label, url)

        out: list[CandidateEvent] = []
        for period, bucket in buckets.items():
            report_url = bucket["links"].get("report")
            out.append(
                candidate(
                    self.key,
                    source,
                    f"PureGym {period} Results",
                    url=report_url or next(iter(bucket["links"].values()), None),
                    document_url=report_url,
                    publication_date=bucket["date"],
                    period=period,
                    links=bucket["links"],
                )
            )
        return out

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        low = slug_title(cand.title)
        if ANNUAL_REPORT_RE.search(low):
            return None
        return EventType.QUARTERLY_RESULTS

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        period = (
            cand.raw.get("period")
            or puregym_period(cand.document_url or "")
            or puregym_period(cand.url or "")
            or puregym_period(cand.title)
        )
        if not period:
            return None
        links = cand.raw.get("links", {})
        report_url = cand.document_url or links.get("report")
        if not report_url:
            # No Report yet: presentation/webcast alone must not alert.
            logger.info(
                "company=%s action=skipped_no_report period=%s", self.key, period
            )
            return None
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=cand.title,
            source=cand.source,
            publication_date=cand.publication_date,
            primary_url=report_url,
            document_url=report_url,
            pdf_url=report_url if report_url.lower().endswith(".pdf") else None,
            document_identifier=canonical_url(report_url),
            presentation_url=links.get("presentation"),
            webcast_url=links.get("webcast"),
            transcript_url=links.get("transcript"),
            issuer="Pinnacle Bidco plc",
            key_includes_event_type=False,
        )


def puregym_period(text: str) -> str | None:
    if not text:
        return None
    path_match = PERIOD_PATH_RE.search(text)
    if path_match:
        quarter = int(path_match.group(2)[1])
        return quarter_period(quarter, int(path_match.group(1)))
    low = slug_title(text)
    match = PERIOD_Q_RE.search(low.replace(" ", ""))
    if match:
        return quarter_period(int(match.group(1)), normalize_year(match.group(2)))
    match = PERIOD_FY_RE.search(low.replace(" ", ""))
    if match:
        return quarter_period(4, normalize_year(match.group(1)))
    return None


def _label_for(text: str, url: str) -> str:
    low = slug_title(f"{text} {url}")
    for label, needles in LABELS.items():
        if any(needle in low for needle in needles):
            return label
    if url.lower().endswith(".pdf") and not IGNORE_LABEL_RE.search(low):
        return "report"
    return "other"


def _looks_like_q4_payload(payload: Any) -> bool:
    rows = _extract_rows(payload)
    if not rows:
        return False
    keys = {k.lower() for row in rows[:5] for k in row}
    return bool(keys & {"reporttitle", "reportyear", "documentpath", "title", "url"})


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in (
            "GetFinancialReportListResult",
            "items",
            "results",
            "data",
            "records",
            "reports",
        ):
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
        if PERIOD_Q_RE.search(slug_title(text).replace(" ", "")):
            return text[:400]
    return best[:400]
