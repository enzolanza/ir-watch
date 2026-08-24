"""Basic-Fit - financial results and trading updates.

Relevant events (all five generate alerts):

    January Trading Update -> JAN-TU-YYYY
    Q1 Trading Update      -> Q1-YYYY
    Half Year Results      -> H1-YYYY
    Q3 Trading Update      -> Q3-YYYY
    Full Year Results      -> FY-YYYY

Source priority, per the specification:

    1. structured endpoint used by the Financial Results page (candidate URLs
       are configured in companies.yaml, never invented in code)
    2. Playwright rendering of the Financial Results page
    3. the general Press Releases page

Capital Markets Day, annual reports, presentations and webcasts are ignored.
The Financial Calendar is never a trigger.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import canonical_url, parse_date, slug_title, squash
from .base import (
    CompanyMonitor,
    EndpointProbeMixin,
    HTMLSourceMixin,
    ParserFailure,
    PlaywrightFallbackMixin,
    candidate,
)

logger = logging.getLogger(__name__)

SOURCE_ENDPOINT = "basic_fit_results_endpoint"
SOURCE_RENDERED = "basic_fit_results_rendered"
SOURCE_PRESS = "basic_fit_press_releases"

DEFAULT_URL = "https://corporate.basic-fit.com/investors/financial-results"
DEFAULT_PRESS_URL = "https://corporate.basic-fit.com/investors/press-releases"

IGNORE_RE = re.compile(
    r"\b(capital\s+markets\s+day|annual\s+report|presentation|webcast|transcript|"
    r"agenda|agm|general\s+meeting|prospectus|sustainability\s+report|factsheet)\b"
)
NOTICE_RE = re.compile(r"^\s*(notice|invitation)\s+(of|to)\b")

TRADING_UPDATE_RE = re.compile(r"\btrading\s+update\b")
HALF_YEAR_RE = re.compile(r"\bhalf[-\s]?year\s+results\b|\bh1\s+results\b")
FULL_YEAR_RE = re.compile(r"\bfull[-\s]?year\s+results\b|\bfy\s+results\b")

Q_RE = re.compile(r"\bq([1-4])\b")
JANUARY_RE = re.compile(r"\bjanuary\b")
YEAR_RE = re.compile(r"\b(20\d{2})\b")


class BasicFitMonitor(
    EndpointProbeMixin, HTMLSourceMixin, PlaywrightFallbackMixin, CompanyMonitor
):
    key = "basic_fit"
    min_expected_candidates = 1

    # ------------------------------------------------------------------
    def fetch_candidates(self) -> list[CandidateEvent]:
        # 1. structured endpoint (configured, validated, never invented)
        endpoints = self.config.option("candidate_endpoints", []) or []
        payload = self.probe_endpoints(endpoints, _looks_like_results_payload)
        if payload is not None:
            self.source_used = SOURCE_ENDPOINT
            return self.parse_endpoint_payload(payload)

        url = self.config.primary_url or DEFAULT_URL

        # 2. Playwright rendering of the same page
        try:
            html = self.render_html(url)
            items = self.parse_results_html(html, url, SOURCE_RENDERED)
            if items:
                self.source_used = SOURCE_RENDERED
                return items
        except Exception as exc:  # noqa: BLE001
            logger.info("company=%s action=render_failed error=%s", self.key, exc)

        # 2b. plain HTTP on the same page (some records may be server rendered)
        try:
            html = http.get_text(url)
            items = self.parse_results_html(html, url, SOURCE_RENDERED)
            if items:
                self.source_used = SOURCE_RENDERED
                return items
        except Exception as exc:  # noqa: BLE001
            logger.info("company=%s action=static_failed error=%s", self.key, exc)

        # 3. press releases fallback
        press_url = self.config.option("press_url", DEFAULT_PRESS_URL)
        html = http.get_text(press_url)
        items = self.parse_results_html(html, press_url, SOURCE_PRESS)
        if not items:
            raise ParserFailure("basic_fit: all configured sources produced no items")
        self.source_used = SOURCE_PRESS
        return items

    # ------------------------------------------------------------------
    def parse_endpoint_payload(self, payload: Any) -> list[CandidateEvent]:
        rows = _extract_rows(payload)
        out: list[CandidateEvent] = []
        for row in rows:
            title = squash(
                str(
                    row.get("title")
                    or row.get("description")
                    or row.get("name")
                    or ""
                )
            )
            if not title:
                continue
            out.append(
                candidate(
                    self.key,
                    SOURCE_ENDPOINT,
                    title,
                    url=_first_url(row, ("url", "link", "permalink", "detailUrl")),
                    document_url=_first_url(
                        row, ("report", "reportUrl", "documentUrl", "pdf", "pdfUrl", "file")
                    ),
                    publication_date=parse_date(
                        str(row.get("date") or row.get("publicationDate") or "")
                    ),
                    presentation_url=_first_url(
                        row, ("presentation", "presentationUrl")
                    ),
                    webcast_url=_first_url(row, ("webcast", "webcastUrl")),
                    raw_row=row,
                )
            )
        return out

    def parse_results_html(
        self, html: str, base_url: str, source: str
    ) -> list[CandidateEvent]:
        soup = self.soup_from(html)
        out: list[CandidateEvent] = []
        seen: set[str] = set()
        for text, url, anchor in self.iter_links(soup, base_url):
            block = _block_text(anchor)
            title = text if len(text) > 8 else block[:160]
            low = slug_title(f"{text} {block}")
            if not (
                TRADING_UPDATE_RE.search(low)
                or HALF_YEAR_RE.search(low)
                or FULL_YEAR_RE.search(low)
            ):
                continue
            key = f"{slug_title(title)}|{url}"
            if key in seen:
                continue
            seen.add(key)
            out.append(
                candidate(
                    self.key,
                    source,
                    title,
                    url=url,
                    document_url=url if url.lower().endswith(".pdf") else None,
                    publication_date=parse_date(block[:120]),
                    context=block,
                    link_text=text,
                )
            )
        return out

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        return classify_basic_fit_title(f"{cand.title} {cand.raw.get('link_text', '')}")

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        period = basic_fit_period(
            event_type,
            f"{cand.title} {cand.raw.get('context', '')}",
            cand.publication_date,
        )
        if not period:
            return None
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=cand.title,
            source=cand.source,
            publication_date=cand.publication_date,
            primary_url=cand.url,
            document_url=cand.document_url or cand.url,
            pdf_url=cand.document_url,
            document_identifier=canonical_url(cand.document_url or cand.url),
            presentation_url=cand.raw.get("presentation_url"),
            webcast_url=cand.raw.get("webcast_url"),
            issuer="Basic-Fit N.V.",
            ticker="BFIT.AS",
        )


# --------------------------------------------------------------------------
# Deterministic classification
# --------------------------------------------------------------------------

def classify_basic_fit_title(title: str) -> str | None:
    low = slug_title(title)
    if NOTICE_RE.search(low):
        return None
    if IGNORE_RE.search(low) and not (
        TRADING_UPDATE_RE.search(low) or HALF_YEAR_RE.search(low) or FULL_YEAR_RE.search(low)
    ):
        return None
    if IGNORE_RE.search(low) and re.search(r"\b(presentation|webcast|transcript)\b", low):
        # "Q1 Trading Update presentation" is supporting material, not the event.
        return None
    if TRADING_UPDATE_RE.search(low):
        return EventType.TRADING_UPDATE
    if HALF_YEAR_RE.search(low):
        return EventType.HALF_YEAR_RESULTS
    if FULL_YEAR_RE.search(low):
        return EventType.FULL_YEAR_RESULTS
    return None


def basic_fit_period(event_type: str, text: str, published: date | None) -> str | None:
    low = slug_title(text)
    year_match = YEAR_RE.search(low)
    year = int(year_match.group(1)) if year_match else (published.year if published else None)
    if year is None:
        return None

    if event_type == EventType.HALF_YEAR_RESULTS:
        return f"H1-{year}"
    if event_type == EventType.FULL_YEAR_RESULTS:
        # An FY release published in Q1 of year Y reports FY(Y-1) unless the
        # title states the year explicitly.
        if not year_match and published and published.month <= 6:
            return f"FY-{year - 1}"
        return f"FY-{year}"
    if event_type == EventType.TRADING_UPDATE:
        q_match = Q_RE.search(low)
        if q_match:
            return f"Q{q_match.group(1)}-{year}"
        if JANUARY_RE.search(low) or (published and published.month == 1):
            return f"JAN-TU-{published.year if published else year}"
        if published:
            quarter = (published.month - 1) // 3 + 1
            return f"Q{quarter}-{published.year}"
    return None


# --------------------------------------------------------------------------
# Endpoint payload helpers
# --------------------------------------------------------------------------

def _looks_like_results_payload(payload: Any) -> bool:
    rows = _extract_rows(payload)
    if not rows:
        return False
    keys = {k.lower() for row in rows[:5] for k in row}
    return bool(keys & {"title", "description", "name"})


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("items", "results", "data", "records", "entries", "documents"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
            if isinstance(value, dict):
                nested = _extract_rows(value)
                if nested:
                    return nested
    return []


def _first_url(row: dict[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = row.get(name)
        if isinstance(value, str) and value.startswith(("http", "/")):
            return value
        if isinstance(value, dict):
            for inner in ("url", "href", "link", "file"):
                candidate_value = value.get(inner)
                if isinstance(candidate_value, str) and candidate_value:
                    return candidate_value
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
        if len(best) > 80:
            break
    return best[:500]
