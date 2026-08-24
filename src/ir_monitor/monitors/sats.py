"""SATS - Reports & Presentations page (static HTML).

Only "Qx Report YYYY" entries are relevant. The Report link is the trigger;
presentation, webcast and analyst tool are stored as metadata only.

Annual Report YYYY is ignored: the Q4 Report already is the Q4/full year
disclosure and the Annual Report is published later.

Pre-Close Call Scripts (separate page) are ignored: SATS itself states those
calls only restate previously public information, so they are not material
trading updates in the Basic-Fit / The Gym Group sense.
"""

from __future__ import annotations

import re

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
from .base import CompanyMonitor, HTMLSourceMixin, ParserFailure, candidate

SOURCE_HTML = "sats_reports_presentations_html"
SOURCE_NEWS = "sats_investor_news_html"

DEFAULT_URL = "https://satsgroup.com/reports-presentations/"
DEFAULT_NEWS_URL = "https://satsgroup.com/investor-news/"

QUARTER_REPORT_RE = re.compile(r"\bq([1-4])\s*report\s*(20\d{2})\b")
ANNUAL_REPORT_RE = re.compile(r"\bannual\s+report\b")
PRE_CLOSE_RE = re.compile(r"\bpre-?\s*close\b")

# Fallback (Investor News): "SATS ASA Q3 2026: ..."
NEWS_RESULT_RE = re.compile(r"\bsats\s+asa\s+q([1-4])\s+(20\d{2})\b")
NEWS_IGNORE_RE = re.compile(
    r"\b(dividend|buy-?back|share\s+repurchase|mandatory\s+notification|"
    r"annual\s+general\s+meeting|primary\s+insider|financial\s+calendar)\b"
)

DOC_LABELS = {
    "report": ("report", "rapport", "kvartalsrapport"),
    "presentation": ("presentation", "presentasjon"),
    "webcast": ("webcast", "webinar", "broadcast"),
    "analyst_tool": ("analyst tool", "analystverktoy", "analyst"),
}


class SATSMonitor(HTMLSourceMixin, CompanyMonitor):
    key = "sats"
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        url = self.config.primary_url or DEFAULT_URL
        try:
            html = http.get_text(url)
            items = self.parse_reports_page(html, url)
            if items:
                self.source_used = SOURCE_HTML
                return items
        except Exception:  # noqa: BLE001 - fall through to investor news
            pass

        news_url = self.config.option("news_url", DEFAULT_NEWS_URL)
        html = http.get_text(news_url)
        items = self.parse_news_page(html, news_url)
        if not items:
            raise ParserFailure("sats: reports page and investor news both empty")
        self.source_used = SOURCE_NEWS
        return items

    # ------------------------------------------------------------------
    def parse_reports_page(self, html: str, base_url: str) -> list[CandidateEvent]:
        """Group every link under the "Qx Report YYYY" block it belongs to."""
        soup = self.soup_from(html)
        buckets: dict[str, dict] = {}

        for text, url, anchor in self.iter_links(soup, base_url):
            block_text = _block_text(anchor)
            haystack = slug_title(f"{block_text} {text}")
            match = QUARTER_REPORT_RE.search(haystack)
            if not match:
                continue
            if ANNUAL_REPORT_RE.search(slug_title(text)):
                continue
            label = _document_label(text)
            bucket_key = f"Q{match.group(1)} Report {match.group(2)}"
            bucket = buckets.setdefault(
                bucket_key,
                {
                    "title": bucket_key,
                    "links": {},
                    "date": parse_date(block_text[:120]),
                    "block": block_text,
                },
            )
            bucket["links"].setdefault(label, url)

        out: list[CandidateEvent] = []
        for title, bucket in buckets.items():
            out.append(
                candidate(
                    self.key,
                    SOURCE_HTML,
                    title,
                    url=bucket["links"].get("report") or bucket["links"].get("other"),
                    document_url=bucket["links"].get("report"),
                    publication_date=bucket["date"],
                    links=bucket["links"],
                    kind="reports_page",
                )
            )
        return out

    def parse_news_page(self, html: str, base_url: str) -> list[CandidateEvent]:
        soup = self.soup_from(html)
        out: list[CandidateEvent] = []
        seen: set[str] = set()
        for text, url, anchor in self.iter_links(soup, base_url):
            low = slug_title(text)
            if not NEWS_RESULT_RE.search(low):
                continue
            if NEWS_IGNORE_RE.search(low):
                continue
            match = NEWS_RESULT_RE.search(low)
            dedupe = f"{match.group(1)}-{match.group(2)}"
            if dedupe in seen:  # EN and NO versions of the same release
                continue
            seen.add(dedupe)
            out.append(
                candidate(
                    self.key,
                    SOURCE_NEWS,
                    text,
                    url=url,
                    publication_date=parse_date(_block_text(anchor)[:120]),
                    kind="news",
                )
            )
        return out

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        return classify_sats_title(cand.title)

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        match = QUARTER_REPORT_RE.search(slug_title(cand.title)) or NEWS_RESULT_RE.search(
            slug_title(cand.title)
        )
        if not match:
            return None
        period = quarter_period(int(match.group(1)), normalize_year(match.group(2)))
        links = cand.raw.get("links", {})
        report_url = links.get("report") or cand.url
        if cand.raw.get("kind") == "reports_page" and not report_url:
            # No Report document => not a trigger yet.
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
            document_identifier=canonical_url(report_url),
            presentation_url=links.get("presentation"),
            webcast_url=links.get("webcast"),
            analyst_tool_url=links.get("analyst_tool"),
            issuer="SATS ASA",
            ticker="SATS.OL",
            key_includes_event_type=False,
        )


def classify_sats_title(title: str) -> str | None:
    low = slug_title(title)
    if PRE_CLOSE_RE.search(low):
        return None
    if ANNUAL_REPORT_RE.search(low):
        return None
    if QUARTER_REPORT_RE.search(low) or NEWS_RESULT_RE.search(low):
        return EventType.QUARTERLY_RESULTS
    return None


def _document_label(text: str) -> str:
    low = slug_title(text)
    for label, needles in DOC_LABELS.items():
        if any(needle in low for needle in needles):
            return label
    return "other"


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
        if QUARTER_REPORT_RE.search(slug_title(text)):
            return text[:600]
    return best[:600]
