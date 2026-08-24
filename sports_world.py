"""Sports World (Grupo Sports World) - Reportes Trimestrales.

Two layers:

1. Parse the "Reportes Trimestrales" section of the Centro de Reportes and pick
   up quarterly PDFs. Only that section - Reportes Trimestrales BMV, Informes
   Anuales, Reportes Anuales BMV, XBRL, comunicados, webcasts and presentations
   are out of scope.

2. A predictive probe of the next expected filename. The historical pattern is
   .../uploads/es/documents/reports_quarterly/gsw_reporte_{PERIODO}.pdf
   (gsw_reporte_1T26.pdf). This is a *second* layer only: filename conventions
   change, so it can never be the sole source.

Any probed URL is validated with PDF magic bytes plus deterministic text checks
(period, "Grupo Sports World", results language). HTTP 200 alone proves nothing.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import (
    canonical_url,
    normalize_year,
    parse_date,
    quarter_period,
    slug_title,
)
from ..util import extract_pdf_text, looks_like_pdf
from .base import CompanyMonitor, HTMLSourceMixin, ParserFailure, candidate

logger = logging.getLogger(__name__)

SOURCE_HTML = "sports_world_reportes_trimestrales_html"
SOURCE_PROBE = "sports_world_pdf_probe"

DEFAULT_URL = (
    "https://www.sportsworld.com.mx/inversionistas/centro-de-reportes/"
    "reportes-trimestrales"
)
PDF_TEMPLATE = (
    "https://www.sportsworld.com.mx/uploads/es/documents/reports_quarterly/"
    "gsw_reporte_{period}.pdf"
)

PERIOD_IN_FILENAME_RE = re.compile(r"gsw_reporte_([1-4])t(\d{2})", re.IGNORECASE)
QUARTER_LABEL_RE = re.compile(r"\b([1-4])\s*t\s*(\d{2}|20\d{2})\b")
QUARTERLY_PATH_RE = re.compile(r"reports?_quarterly|reportes?_trimestral", re.IGNORECASE)

EXCLUDE_PATH_RE = re.compile(
    r"(annual|anual|bmv|xbrl|presenta|webcast|comunicado)", re.IGNORECASE
)

# Deterministic PDF content validation (no LLM).
CONTENT_PATTERNS = [
    r"grupo\s+sports\s*world|sports\s*world",
    r"resultados",
]


class SportsWorldMonitor(HTMLSourceMixin, CompanyMonitor):
    key = "sports_world"
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        url = self.config.primary_url or DEFAULT_URL
        items: list[CandidateEvent] = []
        html_ok = False
        try:
            html = http.get_text(url)
            items = self.parse_reports_page(html, url)
            html_ok = bool(items)
        except Exception as exc:  # noqa: BLE001 - probe layer may still work
            logger.info("company=%s action=html_failed error=%s", self.key, exc)

        if html_ok:
            self.source_used = SOURCE_HTML
        probed = self.probe_next_periods(items)
        items.extend(probed)
        if not items:
            raise ParserFailure(
                "sports_world: quarterly section produced no documents and the "
                "predictive probe found nothing"
            )
        if not html_ok:
            self.source_used = SOURCE_PROBE
        return items

    # ------------------------------------------------------------------
    def parse_reports_page(self, html: str, base_url: str) -> list[CandidateEvent]:
        soup = self.soup_from(html)
        out: list[CandidateEvent] = []
        seen: set[str] = set()
        for text, url, _anchor in self.iter_links(soup, base_url):
            if not url.lower().endswith(".pdf"):
                continue
            if EXCLUDE_PATH_RE.search(url) and not QUARTERLY_PATH_RE.search(url):
                continue
            if not (QUARTERLY_PATH_RE.search(url) or PERIOD_IN_FILENAME_RE.search(url)):
                continue
            # Spanish documents are preferred; the English twin is the same event.
            if "/uploads/en/" in url.lower():
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(
                candidate(
                    self.key,
                    SOURCE_HTML,
                    text or url.rsplit("/", 1)[-1],
                    url=url,
                    document_url=url,
                    kind="html",
                )
            )
        return out

    def probe_next_periods(self, known: list[CandidateEvent]) -> list[CandidateEvent]:
        """Build the next expected filename and check whether it is live yet."""
        if not self.config.option("probe_enabled", True):
            return []
        periods = sorted(
            {
                p
                for p in (sports_world_period_tuple(c.url or c.title) for c in known)
                if p
            }
        )
        if periods:
            last_year, last_quarter = periods[-1][0], periods[-1][1]
        else:
            today = date.today()
            last_year, last_quarter = today.year, max(1, (today.month - 1) // 3)

        out: list[CandidateEvent] = []
        year, quarter = last_year, last_quarter
        for _ in range(int(self.config.option("probe_lookahead", 2))):
            quarter += 1
            if quarter > 4:
                quarter, year = 1, year + 1
            url = PDF_TEMPLATE.format(period=f"{quarter}T{str(year)[2:]}")
            if self._pdf_is_valid(url, quarter, year):
                out.append(
                    candidate(
                        self.key,
                        SOURCE_PROBE,
                        f"Reporte Trimestral {quarter}T{str(year)[2:]}",
                        url=url,
                        document_url=url,
                        kind="probe",
                        probed_quarter=quarter,
                        probed_year=year,
                    )
                )
        return out

    def _pdf_is_valid(self, url: str, quarter: int, year: int) -> bool:
        try:
            response = http.get(url, timeout=25)
        except Exception:  # noqa: BLE001 - not published yet
            return False
        if response.status_code != 200:
            return False
        content = response.content or b""
        if not looks_like_pdf(content, response.headers.get("Content-Type")):
            logger.info("company=%s action=probe_not_pdf url=%s", self.key, url)
            return False
        text = extract_pdf_text(content)
        if not text:
            # No PDF text backend available: magic bytes plus the deterministic
            # filename period are the best available evidence.
            return True
        low = text.lower()
        period_ok = bool(
            re.search(rf"{quarter}\s*t\s*{str(year)[2:]}", low)
            or re.search(rf"{quarter}t{year}", low)
            or re.search(rf"\b{year}\b", low)
        )
        content_ok = all(re.search(p, low) for p in CONTENT_PATTERNS)
        if not (period_ok and content_ok):
            logger.info(
                "company=%s action=probe_content_rejected url=%s", self.key, url
            )
        return period_ok and content_ok

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        if cand.raw.get("kind") == "probe":
            return EventType.EARNINGS_RELEASE
        if sports_world_period_tuple(cand.url or cand.title):
            return EventType.EARNINGS_RELEASE
        return None

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        parsed = sports_world_period_tuple(cand.url or "") or sports_world_period_tuple(
            cand.title
        )
        if not parsed:
            return None
        year, quarter = parsed
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=quarter_period(quarter, year),
            title=cand.title,
            source=cand.source,
            publication_date=cand.publication_date or parse_date(cand.raw.get("date")),
            primary_url=cand.url,
            document_url=cand.document_url or cand.url,
            pdf_url=cand.document_url or cand.url,
            document_identifier=canonical_url(cand.url),
            issuer="Grupo Sports World, S.A.B. de C.V.",
            ticker="SPORTS.MX",
            key_includes_event_type=False,
        )


def sports_world_period_tuple(text: str) -> tuple[int, int] | None:
    """Return (year, quarter) from a filename or a label like '2T26'."""
    if not text:
        return None
    match = PERIOD_IN_FILENAME_RE.search(text)
    if match:
        return normalize_year(match.group(2)), int(match.group(1))
    match = QUARTER_LABEL_RE.search(slug_title(text).replace(" ", ""))
    if match:
        return normalize_year(match.group(2)), int(match.group(1))
    return None
