"""Bluefit - Central de Resultados (MZ platform).

Only the *results release* generates an alert. Presentations, financial
statements (DFP/ITR), CVM documents and any other file are ignored.

The release is not guaranteed to contain the word "Release": Bluefit has also
used "Comentário de Desempenho" and "Relatório da Administração" for the
document performing that role, so the allowlist covers all three.

Source priority: MZ backend endpoint (configured candidates, validated) ->
Playwright rendering of the Central de Resultados page. The MZ document UUID is
the technical identifier.

Classification uses MZ metadata (title, category, period) first. PDF content
inspection is the documented fallback when the metadata cannot separate the
release from other documents of the same period.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import (
    canonical_url,
    extract_quarter_and_year,
    parse_date,
    quarter_period,
    slug_title,
    squash,
)
from ..util import extract_pdf_text, looks_like_pdf
from .base import (
    CompanyMonitor,
    EndpointProbeMixin,
    HTMLSourceMixin,
    ParserFailure,
    PlaywrightFallbackMixin,
    candidate,
)

logger = logging.getLogger(__name__)

SOURCE_ENDPOINT = "bluefit_mz_endpoint"
SOURCE_RENDERED = "bluefit_results_center_rendered"

DEFAULT_URL = "https://ri.bluefit.com.br/informacoes-financeiras/central-de-resultados/"

MZ_UUID_RE = re.compile(
    r"mzfilemanager/v2/d/([0-9a-f-]{36})/([0-9a-f-]{36})", re.IGNORECASE
)

RELEASE_PATTERNS = [
    re.compile(r"\brelease\b"),
    re.compile(r"\bcoment[a']?rio\s+de\s+desempenho\b"),
    re.compile(r"\brelat[o']?rio\s+da\s+administra[c\u00e7][a\u00e3]o\b"),
    re.compile(r"\bearnings\s+release\b"),
    re.compile(r"\bdivulga[c\u00e7][a\u00e3]o\s+de\s+resultados\b"),
]

EXCLUDE_PATTERNS = [
    re.compile(r"\bapresenta[c\u00e7][a\u00e3]o\b|\bpresentation\b"),
    re.compile(r"\bdemonstra[c\u00e7][o\u00f5]es\s+financeiras\b|\bdfp\b|\bitr\b"),
    re.compile(r"\bformul[a']?rio\s+de\s+refer[e\u00ea]ncia\b|\bcvm\b"),
    re.compile(r"\btranscri[c\u00e7][a\u00e3]o\b|\btranscript\b|\bteleconfer"),
    re.compile(r"\bata\b|\bassembleia\b|\bedital\b|\bdebentures?\b|\bdeb[e\u00ea]ntures?\b"),
    re.compile(r"\bfato\s+relevante\b|\bcomunicado\s+ao\s+mercado\b"),
    re.compile(r"\bparecer\b|\baudito"),
    re.compile(r"\bplanilha\b|\bspreadsheet\b|\bxlsx?\b"),
]

# Deterministic textual evidence used by the PDF fallback.
PDF_RELEASE_PATTERNS = [
    r"resultados?\s+d[oe]",
    r"receita\s+l[i\u00ed]quida|ebitda|desempenho",
]


class BluefitMonitor(
    EndpointProbeMixin, HTMLSourceMixin, PlaywrightFallbackMixin, CompanyMonitor
):
    key = "bluefit"
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        endpoints = self.config.option("candidate_endpoints", []) or []
        payload = self.probe_endpoints(endpoints, _looks_like_mz_payload)
        if payload is not None:
            self.source_used = SOURCE_ENDPOINT
            return self.parse_mz_payload(payload)

        url = self.config.primary_url or DEFAULT_URL

        # Plain HTTP first (cheap); the MZ page is dynamic, so this usually
        # yields nothing and we fall through to the documented Playwright path.
        try:
            html = http.get_text(url)
            items = self.parse_documents_html(html, url, SOURCE_RENDERED)
            if items:
                self.source_used = SOURCE_RENDERED
                return items
        except Exception as exc:  # noqa: BLE001
            logger.info("company=%s action=static_failed error=%s", self.key, exc)

        html = self.render_html(url, wait_for="a[href*='mzfilemanager']")
        items = self.parse_documents_html(html, url, SOURCE_RENDERED)
        if not items:
            raise ParserFailure(
                "bluefit: Central de Resultados returned no documents - treating "
                "as parser/layout failure"
            )
        self.source_used = SOURCE_RENDERED
        return items

    # ------------------------------------------------------------------
    def parse_mz_payload(self, payload: Any) -> list[CandidateEvent]:
        out: list[CandidateEvent] = []
        for row in _extract_rows(payload):
            title = squash(
                str(row.get("title") or row.get("name") or row.get("fileName") or "")
            )
            if not title:
                continue
            url = (
                row.get("url")
                or row.get("fileUrl")
                or row.get("downloadUrl")
                or row.get("path")
            )
            out.append(
                candidate(
                    self.key,
                    SOURCE_ENDPOINT,
                    title,
                    url=url,
                    document_url=url,
                    publication_date=parse_date(
                        str(row.get("date") or row.get("publishedAt") or "")
                    ),
                    category=squash(str(row.get("category") or row.get("categoryName") or "")),
                    period=squash(str(row.get("period") or row.get("periodName") or "")),
                    uuid=row.get("id") or row.get("uuid") or _uuid_from_url(url),
                )
            )
        return out

    def parse_documents_html(
        self, html: str, base_url: str, source: str
    ) -> list[CandidateEvent]:
        soup = self.soup_from(html)
        out: list[CandidateEvent] = []
        seen: set[str] = set()
        for text, url, anchor in self.iter_links(soup, base_url):
            if "mzfilemanager" not in url and not url.lower().endswith(".pdf"):
                continue
            uuid = _uuid_from_url(url)
            dedupe = uuid or url
            if dedupe in seen:
                continue
            seen.add(dedupe)
            block = _block_text(anchor)
            out.append(
                candidate(
                    self.key,
                    source,
                    text or block[:120],
                    url=url,
                    document_url=url,
                    publication_date=parse_date(block[:120]),
                    category=block[:200],
                    period=block[:200],
                    uuid=uuid,
                )
            )
        return out

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        haystack = " ".join(
            filter(
                None,
                [cand.title, cand.raw.get("category"), cand.raw.get("period")],
            )
        )
        verdict = classify_bluefit_document(haystack)
        if verdict is not None:
            return verdict
        # Metadata was inconclusive: documented PDF-content fallback.
        if self.config.option("pdf_fallback", True) and cand.document_url:
            if self._pdf_looks_like_release(cand.document_url):
                return EventType.EARNINGS_RELEASE
        return None

    def _pdf_looks_like_release(self, url: str) -> bool:
        try:
            response = http.get(url, timeout=30)
        except Exception:  # noqa: BLE001
            return False
        if response.status_code != 200:
            return False
        if not looks_like_pdf(response.content, response.headers.get("Content-Type")):
            return False
        text = extract_pdf_text(response.content)
        if not text:
            return False
        low = text.lower()
        if any(re.search(p.pattern, low) for p in EXCLUDE_PATTERNS):
            return False
        return all(re.search(p, low) for p in PDF_RELEASE_PATTERNS)

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        haystack = " ".join(
            filter(None, [cand.title, cand.raw.get("period"), cand.raw.get("category")])
        )
        parsed = extract_quarter_and_year(haystack) or extract_quarter_and_year(
            cand.url or ""
        )
        if not parsed:
            return None
        quarter, year = parsed
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=quarter_period(quarter, year),
            title=cand.title,
            source=cand.source,
            publication_date=cand.publication_date,
            primary_url=cand.url,
            document_url=cand.document_url or cand.url,
            pdf_url=cand.document_url or cand.url,
            document_identifier=cand.raw.get("uuid") or canonical_url(cand.url),
            issuer="Bluefit Academias de Ginastica e Participacoes S.A.",
            key_includes_event_type=False,
        )


def classify_bluefit_document(text: str) -> str | None:
    """Return earnings_release, or None when the metadata is not conclusive."""
    low = slug_title(text)
    if any(pattern.search(low) for pattern in EXCLUDE_PATTERNS):
        return None
    if any(pattern.search(low) for pattern in RELEASE_PATTERNS):
        return EventType.EARNINGS_RELEASE
    return None


def _uuid_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = MZ_UUID_RE.search(url)
    return match.group(2) if match else None


def _looks_like_mz_payload(payload: Any) -> bool:
    rows = _extract_rows(payload)
    if not rows:
        return False
    keys = {k.lower() for row in rows[:5] for k in row}
    return bool(keys & {"title", "name", "filename"})


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "documents", "files", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
            if isinstance(value, dict):
                nested = _extract_rows(value)
                if nested:
                    return nested
    return []


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
    return best[:400]
