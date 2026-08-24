"""Selfit - annual financial statements.

Selfit does not appear to publish a separate earnings release, so the annual
financial statements are treated as the equivalent results disclosure.

Only the "Demonstrações financeiras anuais" section matters. Assembleias/Atas,
Avisos ao Mercado, debenture documents and fiduciary agent reports are ignored.

The documents are PDFs hosted on Selfit's own domain, normally under /pdfs/.
Source priority: configured endpoint -> plain HTTP -> Playwright rendering.

A new URL or a new PDF version for an already known year is recorded as an
extra source observation but does not generate a second alert (unless
ALERT_ON_REVISION is turned on).
"""

from __future__ import annotations

import logging
import re

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

SOURCE_ENDPOINT = "selfit_documents_endpoint"
SOURCE_HTML = "selfit_investidores_html"
SOURCE_RENDERED = "selfit_investidores_rendered"

DEFAULT_URL = "https://www.selfitacademias.com.br/investidores"

SECTION_RE = re.compile(
    r"demonstra[c\u00e7][o\u00f5]es\s+financeiras(\s+anuais)?"
)
ANNUAL_DOC_RE = re.compile(
    r"demonstra[c\u00e7][o\u00f5]es\s+financeiras|dfp|balan[c\u00e7]o\s+patrimonial"
)
EXCLUDE_RE = re.compile(
    r"\b(assembleia|ata|aviso\s+ao\s+mercado|deb[e\u00ea]ntures?|agente\s+fiduci|"
    r"escritura|fato\s+relevante|estatuto|pol[i\u00ed]tica|c[o\u00f3]digo|"
    r"regimento|convoca[c\u00e7])\b"
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")


class SelfitMonitor(
    EndpointProbeMixin, HTMLSourceMixin, PlaywrightFallbackMixin, CompanyMonitor
):
    key = "selfit"
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        endpoints = self.config.option("candidate_endpoints", []) or []
        payload = self.probe_endpoints(endpoints, _looks_like_documents_payload)
        if payload is not None:
            self.source_used = SOURCE_ENDPOINT
            return self.parse_payload(payload)

        url = self.config.primary_url or DEFAULT_URL
        try:
            html = http.get_text(url)
            items = self.parse_investor_page(html, url, SOURCE_HTML)
            if items:
                self.source_used = SOURCE_HTML
                return items
        except Exception as exc:  # noqa: BLE001
            logger.info("company=%s action=static_failed error=%s", self.key, exc)

        html = self.render_html(url, wait_for="a[href*='.pdf']")
        items = self.parse_investor_page(html, url, SOURCE_RENDERED)
        if not items:
            raise ParserFailure(
                "selfit: annual financial statements section produced no documents"
            )
        self.source_used = SOURCE_RENDERED
        return items

    # ------------------------------------------------------------------
    def parse_payload(self, payload) -> list[CandidateEvent]:
        out: list[CandidateEvent] = []
        for row in _extract_rows(payload):
            title = squash(str(row.get("title") or row.get("name") or ""))
            url = row.get("url") or row.get("file") or row.get("path")
            if not title or not url:
                continue
            out.append(
                candidate(
                    self.key,
                    SOURCE_ENDPOINT,
                    title,
                    url=url,
                    document_url=url,
                    publication_date=parse_date(str(row.get("date") or "")),
                    section=squash(str(row.get("section") or row.get("category") or "")),
                )
            )
        return out

    def parse_investor_page(
        self, html: str, base_url: str, source: str
    ) -> list[CandidateEvent]:
        soup = self.soup_from(html)
        out: list[CandidateEvent] = []
        seen: set[str] = set()
        for text, url, anchor in self.iter_links(soup, base_url):
            if not url.lower().endswith(".pdf"):
                continue
            if url in seen:
                continue
            seen.add(url)
            section = _section_for(anchor)
            out.append(
                candidate(
                    self.key,
                    source,
                    text or url.rsplit("/", 1)[-1],
                    url=url,
                    document_url=url,
                    publication_date=parse_date(section[:120]),
                    section=section,
                )
            )
        return out

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        return classify_selfit_document(
            cand.title, cand.raw.get("section", ""), cand.url or ""
        )

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        haystack = f"{cand.title} {cand.url or ''} {cand.raw.get('section', '')}"
        years = [int(y) for y in YEAR_RE.findall(haystack)]
        if not years:
            return None
        year = max(years)
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=f"FY-{year}",
            title=cand.title,
            source=cand.source,
            publication_date=cand.publication_date,
            primary_url=cand.url,
            document_url=cand.document_url or cand.url,
            pdf_url=cand.document_url or cand.url,
            financial_statements_url=cand.document_url or cand.url,
            document_identifier=canonical_url(cand.url),
            issuer="Selfit Academias",
            key_includes_event_type=False,
        )


def classify_selfit_document(title: str, section: str, url: str) -> str | None:
    """Section membership determines the type; content reading is a fallback."""
    low_title = slug_title(title)
    low_section = slug_title(section)
    low_url = slug_title(url)

    if EXCLUDE_RE.search(low_title) or EXCLUDE_RE.search(low_url):
        return None
    if EXCLUDE_RE.search(low_section) and not SECTION_RE.search(low_section):
        return None
    if SECTION_RE.search(low_section) or ANNUAL_DOC_RE.search(low_title) or ANNUAL_DOC_RE.search(low_url):
        return EventType.ANNUAL_FINANCIAL_STATEMENTS
    return None


def _section_for(anchor, max_levels: int = 6) -> str:
    """Return the nearest ancestor text that names a document section."""
    node = anchor
    best = ""
    for _ in range(max_levels):
        node = node.parent
        if node is None:
            break
        text = squash(node.get_text(" ", strip=True))
        if SECTION_RE.search(slug_title(text)):
            return text[:300]
        if len(text) > len(best):
            best = text
    return best[:300]


def _looks_like_documents_payload(payload) -> bool:
    rows = _extract_rows(payload)
    if not rows:
        return False
    keys = {k.lower() for row in rows[:5] for k in row}
    return bool(keys & {"title", "name"}) and bool(keys & {"url", "file", "path"})


def _extract_rows(payload):
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "documents", "files"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
            if isinstance(value, dict):
                nested = _extract_rows(value)
                if nested:
                    return nested
    return []
