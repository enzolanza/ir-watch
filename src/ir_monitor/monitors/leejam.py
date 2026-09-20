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

# The IR Result Center's own filename convention for its older archive, e.g.
# "18Q3.pdf" = Q3 2018, "22Q1.pdf" = Q1 2022. A document's own filename is
# the least contamination-prone period signal available (unlike surrounding
# DOM text, which a shared table/grid layout can spread across several
# quarters' links), so this is checked before the generic year/quarter scan.
_SHORT_YQ_RE = re.compile(r"\b(\d{2})[-_]?q([1-4])\b")

# A year immediately next to the quarter/annual marker, rather than just the
# first 20xx year found anywhere in the combined text. A report for period X
# published early in year X+1 routinely shows both years close together (the
# publish/upload date and the period itself), and taking "the first year in
# the string" silently preferred whichever of the two came first in the DOM.
#
# The "quarter immediately followed by year" shape ("Q4 2025", "Q4-2025") is
# the natural labelling convention and is checked first/tightest, since a
# looser bidirectional search (matching a year *before* the quarter too)
# would otherwise let an unrelated, earlier "2026" publish-date win just for
# appearing first in the string, even when "Q4 2025" is right there.
_QUARTER_THEN_YEAR_RE = re.compile(r"\bq([1-4])\b[\s-]{0,3}(20\d{2})\b")
_YEAR_NEAR_QUARTER_RE = re.compile(
    r"\bq([1-4])\b[^0-9]{0,20}(20\d{2})|(20\d{2})[^0-9]{0,20}\bq([1-4])\b"
)
_YEAR_NEAR_ANNUAL_RE = re.compile(
    r"\bannual\b[^0-9]{0,25}(20\d{2})|(20\d{2})[^0-9]{0,25}\bannual\b"
)

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
            # Filename first: it is the single most specific, least
            # contamination-prone signal (see leejam_filename_period()'s
            # docstring), ahead of the page's own text/block, which a
            # shared table/grid archive layout can spread across several
            # quarters' links.
            period = (
                leejam_filename_period(url)
                or leejam_period(text, block)
                or leejam_filename_period(text)
            )
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
            cand.title, cand.raw.get("context", "")
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


def leejam_period(text: str, context: str = "") -> str | None:
    """Normalize to Q1-YYYY, Q2/H1-YYYY, Q3/9M-YYYY or Q4/FY-YYYY.

    ``context`` is broader surrounding page text (e.g. the DOM block around a
    link) that may legitimately carry an explicit date or quarter number, but
    is also where unrelated page chrome leaks in - a nearby "Annual Reports"
    navigation heading, for instance. The bare "annual" fallback below is
    therefore only trusted in ``text`` (the item's own title/link text), never
    in ``context`` alone, so a Q3 document sitting next to an "Annual
    Reports" section link is never relabelled Q4/FY.
    """
    if not text and not context:
        return None
    low = slug_title(f"{text} {context}")
    low_text = slug_title(text)

    # 1. Explicit period-end date (Tadawul announcements) - unambiguous.
    match = PERIOD_END_RE.search(low)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        label = _MONTH_END_TO_PERIOD.get(month)
        if label:
            return f"{label}-{year}"

    # 2. The document's own short filename convention ("18Q3" -> Q3 2018),
    #    when that text is visible on the page itself (see
    #    leejam_filename_period() below for the URL/filename equivalent,
    #    which callers should try first - it is not routed through the
    #    PERIOD_END_RE date-triplet check above, which is meant for prose
    #    dates and can misread a "/uploads/YYYY/MM/" folder in a URL path as
    #    the document's own period end date).
    short = _SHORT_YQ_RE.search(low_text) or _SHORT_YQ_RE.search(low)
    if short:
        year, quarter = normalize_year(short.group(1)), int(short.group(2))
        return f"{_QUARTER_TO_LABEL[quarter]}-{year}"

    # 3. A quarter number with a year stated close to it, preferred over the
    #    first year found anywhere (see _QUARTER_THEN_YEAR_RE above).
    tight_quarter = _QUARTER_THEN_YEAR_RE.search(low)
    if tight_quarter:
        quarter, year = int(tight_quarter.group(1)), int(tight_quarter.group(2))
        return f"{_QUARTER_TO_LABEL[quarter]}-{year}"
    near_quarter = _YEAR_NEAR_QUARTER_RE.search(low)
    if near_quarter:
        quarter = int(near_quarter.group(1) or near_quarter.group(4))
        year = int(near_quarter.group(2) or near_quarter.group(3))
        return f"{_QUARTER_TO_LABEL[quarter]}-{year}"

    # 4. "Annual"/FY, same principle: a year close to the word wins over the
    #    first year anywhere. Only trusted in the item's own text (never in
    #    context alone - see the class docstring above on nav contamination).
    if ANNUAL_RE.search(low_text):
        near_annual = _YEAR_NEAR_ANNUAL_RE.search(low_text) or _YEAR_NEAR_ANNUAL_RE.search(
            low
        )
        if near_annual:
            year = int(near_annual.group(1) or near_annual.group(2))
            return f"Q4/FY-{year}"

    year_match = re.search(r"\b(20\d{2})\b", low)
    year = int(year_match.group(1)) if year_match else None

    quarter_match = QUARTER_WORD_RE.search(low) or re.search(r"\bq([1-4])\b", low)
    if quarter_match and year:
        quarter = int(quarter_match.group(1))
        return f"{_QUARTER_TO_LABEL[quarter]}-{year}"
    if ANNUAL_RE.search(low_text) and year:
        return f"Q4/FY-{year}"
    return None


def leejam_filename_period(url: str | None) -> str | None:
    """The archive's short filename convention, read from the filename only.

    Deliberately narrower than leejam_period(): it does not run the
    PERIOD_END_RE date-triplet check, which is meant for prose like "Ending
    on 2026-03-31" and would otherwise misread a "/uploads/YYYY/MM/" upload
    folder in the URL's path as the document's own period-end date (e.g.
    ".../uploads/2023/09/20Q1.pdf" contains the literal substring
    "2023-09-20", a false but well-formed date, for a document that is
    actually Q1 2020). Call this first when a URL is available; it returns
    None for anything that is not this specific "YYq[1-4]" filename shape,
    so it is safe to fall back to leejam_period(text, context) otherwise.
    """
    if not url:
        return None
    filename = url.rsplit("/", 1)[-1]
    match = _SHORT_YQ_RE.search(slug_title(filename))
    if not match:
        return None
    year, quarter = normalize_year(match.group(1)), int(match.group(2))
    return f"{_QUARTER_TO_LABEL[quarter]}-{year}"


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
