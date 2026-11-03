"""Basic-Fit: trading updates e resultados semestral/anual.

Fonte: a página "Financial Results"
(https://corporate.basic-fit.com/investors/financial-results). O site é
feito em Next.js e já entrega, dentro do próprio HTML (bloco
``__NEXT_DATA__``), a lista organizada de divulgações: título, data e link
do PDF. O robô lê essa lista diretamente, sem navegador simulado.

Regra (anexo original, conferida em nov/2026):
  gera alerta    -> January Trading Update, Q1 TU, Half Year, Q3 TU, Full Year
  não gera       -> Capital Markets Day, relatórios anuais, apresentações
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import slug_title, squash
from .base import CompanyMonitor, ParserFailure

logger = logging.getLogger(__name__)

SOURCE = "basic_fit_financial_results"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
QUARTER_RE = re.compile(r"\bq([1-4])\b")
YEAR_RE = re.compile(r"\b(20\d{2})\b")


def classify_basic_fit(description: str) -> str | None:
    low = slug_title(description)
    if re.search(r"capital\s+markets?\s+day|annual\s+report|presentation|agm\b", low):
        return None
    if "trading update" in low or "trading statement" in low:
        return EventType.TRADING_UPDATE
    if re.search(r"\bhalf[\s-]+year\b|\bh1\b|\binterim\b", low):
        return EventType.HALF_YEAR_RESULTS
    if re.search(r"\bfull[\s-]+year\b|\bfy\b", low):
        return EventType.FULL_YEAR_RESULTS
    return None


def basic_fit_period(event_type: str, description: str, published: date | None) -> str | None:
    low = slug_title(description)
    year_match = YEAR_RE.search(low)
    year = int(year_match.group(1)) if year_match else (published.year if published else None)
    if year is None:
        return None
    if event_type == EventType.TRADING_UPDATE:
        if "january" in low:
            # Publicado em janeiro, com números do ano anterior. Chave pelo
            # ano de publicação, como no anexo original.
            return f"JAN-TU-{published.year if published else year}"
        q = QUARTER_RE.search(low)
        if q:
            return f"TU-Q{q.group(1)}-{year}"
        return f"TU-{published.isoformat()}" if published else None
    if event_type == EventType.HALF_YEAR_RESULTS:
        return f"H1-{year}"
    if event_type == EventType.FULL_YEAR_RESULTS:
        return f"FY-{year}"
    return None


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def parse_basic_fit_html(html: str, page_url: str, company: str = "basic_fit") -> list[CandidateEvent]:
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise ParserFailure("basic_fit: bloco de dados (__NEXT_DATA__) não encontrado na página")
    try:
        data = json.loads(match.group(1))
    except ValueError as exc:
        raise ParserFailure("basic_fit: bloco de dados ilegível") from exc

    items: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("__typename") == "ReportFinancialResults":
                items.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    if not items:
        raise ParserFailure("basic_fit: nenhuma divulgação encontrada no bloco de dados")

    out: list[CandidateEvent] = []
    seen: set[str] = set()
    for item in items:
        description = squash(str(item.get("description") or ""))
        item_id = str((item.get("sys") or {}).get("id") or "")
        if not description or item_id in seen:
            continue
        seen.add(item_id)
        report = item.get("report") or {}
        presentation = item.get("presentation") or {}
        out.append(
            CandidateEvent(
                company=company,
                source=SOURCE,
                title=description,
                url=page_url,
                document_url=report.get("url") if isinstance(report, dict) else None,
                publication_date=_date(item.get("dateOfPublication") or item.get("date")),
                raw={
                    "id": item_id,
                    "presentation_url": presentation.get("url") if isinstance(presentation, dict) else None,
                    "webcast_url": item.get("webcast"),
                },
            )
        )
    return out


class BasicFitMonitor(CompanyMonitor):
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        url = self.config.primary_url
        self.source_used = SOURCE
        html = http.get_text(url)
        return parse_basic_fit_html(html, url, self.key)

    def classify(self, candidate: CandidateEvent) -> str | None:
        return classify_basic_fit(candidate.title)

    def normalize(self, candidate: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        period = basic_fit_period(event_type, candidate.title, candidate.publication_date)
        if not period:
            period = f"doc-{candidate.raw.get('id')}"
            logger.warning("company=basic_fit action=unknown_period title=%r", candidate.title)
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=candidate.title,
            source=candidate.source,
            publication_date=candidate.publication_date,
            primary_url=self.config.primary_url,
            document_url=candidate.document_url,
            presentation_url=candidate.raw.get("presentation_url"),
            webcast_url=candidate.raw.get("webcast_url"),
            document_identifier=candidate.raw.get("id") or None,
            key_includes_event_type=False,
        )
