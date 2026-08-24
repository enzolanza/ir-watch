"""Bodytech (A!Bodytech Participações S.A., CNPJ 07.737.623/0001-90).

There is no traditional IR area and no separate earnings release, so the
consolidated annual financial statements are the equivalent results document.

Two sources run in parallel and converge on ONE event:

    1. bodytech.com.br -> "Demonstrações financeiras" category only
       ("Outras publicações legais" and every other category are ignored)
    2. SPED Central de Balanços for the CNPJ

The logical key is company + reporting_period (fiscal year), precisely because
the same statements can show up in both places. The first source to reveal a
fiscal year triggers a single alert; the second one only enriches the record.

Page "last updated" timestamps are never a trigger.

NOTE on SPED: the Central de Balanços search flow is interactive and protected.
The adapter consumes a configured, validated endpoint when one is supplied
(``sped_candidate_endpoints`` in companies.yaml) and otherwise skips the source
with a warning. No anti-bot or CAPTCHA circumvention is implemented, and the
site source alone keeps the monitor functional.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import canonical_url, parse_date, slug_title, squash
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

SOURCE_SITE = "bodytech_politicas_html"
SOURCE_SITE_RENDERED = "bodytech_politicas_rendered"
SOURCE_SPED = "sped_central_de_balancos"

DEFAULT_URL = "https://www.bodytech.com.br/pt/politicas/?topico=2"
CNPJ = "07.737.623/0001-90"
CNPJ_DIGITS = "07737623000190"

FINANCIAL_SECTION_RE = re.compile(
    r"demonstra[c\u00e7][o\u00f5]es\s+financeiras"
)
EXCLUDE_RE = re.compile(
    r"outras\s+publica[c\u00e7][o\u00f5]es\s+legais|\bata\b|assembleia|edital|"
    r"convoca[c\u00e7]|pol[i\u00ed]tica\s+de\s+privacidade|termos\s+de\s+uso"
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")
EXERCISE_RE = re.compile(
    r"exerc[i\u00ed]cio(?:s)?\s+(?:social\s+)?(?:findo(?:s)?\s+)?"
    r"em\s+31\s+de\s+dezembro\s+de\s+(20\d{2})"
)

VALIDATION_PATTERNS = [
    r"bodytech",
    r"31\s+de\s+dezembro",
]


class BodytechMonitor(
    EndpointProbeMixin, HTMLSourceMixin, PlaywrightFallbackMixin, CompanyMonitor
):
    key = "bodytech"
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        items: list[CandidateEvent] = []
        used: list[str] = []

        try:
            site_items = self.fetch_site()
            if site_items:
                items.extend(site_items)
                used.append(self.source_used or SOURCE_SITE)
        except Exception as exc:  # noqa: BLE001 - SPED may still answer
            logger.info("company=%s source=site error=%s", self.key, exc)

        try:
            sped_items = self.fetch_sped()
            if sped_items:
                items.extend(sped_items)
                used.append(SOURCE_SPED)
        except Exception as exc:  # noqa: BLE001
            logger.info("company=%s source=sped error=%s", self.key, exc)

        if not items:
            raise ParserFailure(
                "bodytech: neither the site category nor SPED produced documents"
            )
        self.source_used = "+".join(used) or SOURCE_SITE
        return items

    # ------------------------------------------------------------------
    def fetch_site(self) -> list[CandidateEvent]:
        endpoints = self.config.option("candidate_endpoints", []) or []
        payload = self.probe_endpoints(endpoints, _looks_like_docs_payload)
        if payload is not None:
            self.source_used = SOURCE_SITE
            return self.parse_payload(payload, SOURCE_SITE)

        url = self.config.primary_url or DEFAULT_URL
        try:
            html = http.get_text(url)
            items = self.parse_site_html(html, url, SOURCE_SITE)
            if items:
                self.source_used = SOURCE_SITE
                return items
        except Exception as exc:  # noqa: BLE001
            logger.info("company=%s action=static_failed error=%s", self.key, exc)

        html = self.render_html(url, wait_for="a[href*='.pdf']")
        items = self.parse_site_html(html, url, SOURCE_SITE_RENDERED)
        self.source_used = SOURCE_SITE_RENDERED
        return items

    def fetch_sped(self) -> list[CandidateEvent]:
        endpoints = self.config.option("sped_candidate_endpoints", []) or []
        if not endpoints:
            logger.warning(
                "company=%s source=sped action=skipped reason=no_validated_endpoint "
                "detail=Central de Balancos requires an interactive protected flow; "
                "configure sped_candidate_endpoints to enable this source",
                self.key,
            )
            return []
        payload = self.probe_endpoints(endpoints, _looks_like_sped_payload)
        if payload is None:
            logger.warning(
                "company=%s source=sped action=endpoint_unavailable", self.key
            )
            return []
        return self.parse_payload(payload, SOURCE_SPED)

    # ------------------------------------------------------------------
    def parse_payload(self, payload: Any, source: str) -> list[CandidateEvent]:
        out: list[CandidateEvent] = []
        for row in _extract_rows(payload):
            title = squash(
                str(row.get("title") or row.get("nome") or row.get("descricao") or "")
            )
            url = row.get("url") or row.get("arquivo") or row.get("link")
            if not title:
                continue
            out.append(
                candidate(
                    self.key,
                    source,
                    title,
                    url=url,
                    document_url=url,
                    publication_date=parse_date(
                        str(row.get("data") or row.get("date") or "")
                    ),
                    category=squash(str(row.get("categoria") or row.get("topico") or "")),
                    cnpj=row.get("cnpj") or CNPJ,
                )
            )
        return out

    def parse_site_html(
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
                    category=section,
                )
            )
        return out

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        return classify_bodytech_document(
            cand.title, cand.raw.get("category", ""), cand.url or ""
        )

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        haystack = f"{cand.title} {cand.url or ''} {cand.raw.get('category', '')}"
        year = bodytech_fiscal_year(haystack)
        if year is None and self.config.option("pdf_validation", True) and cand.document_url:
            year = self._year_from_pdf(cand.document_url)
        if year is None:
            return None
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
            issuer="A!Bodytech Participacoes S.A.",
            metadata={"cnpj": CNPJ},
            key_includes_event_type=False,
        )

    def _year_from_pdf(self, url: str) -> int | None:
        """Deterministic content check: company + fiscal year ended 31 Dec."""
        try:
            response = http.get(url, timeout=30)
        except Exception:  # noqa: BLE001
            return None
        if response.status_code != 200 or not looks_like_pdf(
            response.content, response.headers.get("Content-Type")
        ):
            return None
        text = extract_pdf_text(response.content, max_pages=3).lower()
        if not text:
            return None
        if not all(re.search(p, text) for p in VALIDATION_PATTERNS):
            return None
        match = EXERCISE_RE.search(text)
        if match:
            return int(match.group(1))
        years = [int(y) for y in YEAR_RE.findall(text)]
        return max(years) if years else None


def classify_bodytech_document(title: str, category: str, url: str) -> str | None:
    low = slug_title(f"{title} {url}")
    low_category = slug_title(category)
    if EXCLUDE_RE.search(low):
        return None
    if EXCLUDE_RE.search(low_category) and not FINANCIAL_SECTION_RE.search(low_category):
        return None
    if FINANCIAL_SECTION_RE.search(low) or FINANCIAL_SECTION_RE.search(low_category):
        return EventType.ANNUAL_FINANCIAL_STATEMENTS
    return None


def bodytech_fiscal_year(text: str) -> int | None:
    low = slug_title(text)
    match = EXERCISE_RE.search(low)
    if match:
        return int(match.group(1))
    years = [int(y) for y in YEAR_RE.findall(low)]
    return max(years) if years else None


def _section_for(anchor, max_levels: int = 6) -> str:
    node = anchor
    best = ""
    for _ in range(max_levels):
        node = node.parent
        if node is None:
            break
        text = squash(node.get_text(" ", strip=True))
        if FINANCIAL_SECTION_RE.search(slug_title(text)):
            return text[:300]
        if len(text) > len(best):
            best = text
    return best[:300]


def _looks_like_docs_payload(payload: Any) -> bool:
    rows = _extract_rows(payload)
    if not rows:
        return False
    keys = {k.lower() for row in rows[:5] for k in row}
    return bool(keys & {"title", "nome", "descricao"})


def _looks_like_sped_payload(payload: Any) -> bool:
    rows = _extract_rows(payload)
    if not rows:
        return False
    blob = slug_title(str(rows[:3]))
    return CNPJ_DIGITS in blob.replace(".", "").replace("/", "").replace("-", "") or bool(
        {k.lower() for row in rows[:3] for k in row} & {"cnpj", "nome", "titulo", "title"}
    )


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "documentos", "documents", "conteudo"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
            if isinstance(value, dict):
                nested = _extract_rows(value)
                if nested:
                    return nested
    return []
