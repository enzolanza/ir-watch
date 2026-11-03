"""Bluefit: demonstrações financeiras (ITR trimestral e DFP anual).

Fonte: o catálogo de documentos da plataforma MZ, o mesmo que a página
"Central de Resultados" (https://ri.bluefit.com.br/informacoes-financeiras/
central-de-resultados/) consulta em segundo plano para montar a tabela.

Regra (definida em nov/2026): a Bluefit não publica "release de resultados".
A única divulgação periódica dela são as demonstrações financeiras, então
TODO documento da categoria "dfs" é um evento que gera alerta.

Não precisa de navegador simulado (Playwright): é uma consulta direta.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import squash
from ..util import now_local
from .base import CompanyMonitor, ParserFailure

logger = logging.getLogger(__name__)

DEFAULT_API = (
    "https://apicatalog.mziq.com/filemanager/company/"
    "d83ecf03-07f6-4822-8e26-922775bc0a72/filter/categories/year/meta"
)
SOURCE = "mz_catalog"


def bluefit_period(quarter, year, title: str = "") -> str | None:
    """1..3 -> '3T2026'; 4 -> 'Anual 2025'. Falls back to the title."""
    try:
        q = int(quarter)
        y = int(year)
    except (TypeError, ValueError):
        q = y = None
    if q and y:
        if q == 4:
            return f"Anual {y}"
        if 1 <= q <= 3:
            return f"{q}T{y}"
    # Fallback: something like "1ITR26" or "3T26" in the title.
    match = re.search(r"\b([1-3])\s*(?:I?TR|T)\s*(\d{2}|\d{4})\b", title or "", re.I)
    if match:
        yy = match.group(2)
        full = int(yy) + 2000 if len(yy) == 2 else int(yy)
        return f"{match.group(1)}T{full}"
    return None


def parse_bluefit_payload(payload: dict, company: str = "bluefit") -> list[CandidateEvent]:
    """Turn one API answer into candidates. Raises ParserFailure on a bad shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ParserFailure("bluefit: resposta da API em formato inesperado")
    metas = payload["data"].get("document_metas")
    if not isinstance(metas, list):
        raise ParserFailure("bluefit: resposta da API sem 'document_metas'")

    out: list[CandidateEvent] = []
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        if meta.get("is_published") is False:
            continue
        title = squash(meta.get("file_title") or meta.get("file_name_original") or "")
        published = None
        raw_date = meta.get("file_published_date") or meta.get("file_date")
        if raw_date:
            try:
                published = date.fromisoformat(str(raw_date)[:10])
            except ValueError:
                published = None
        out.append(
            CandidateEvent(
                company=company,
                source=SOURCE,
                title=title or "Demonstrações financeiras",
                url=None,
                document_url=meta.get("file_url") or meta.get("permalink"),
                publication_date=published,
                raw={
                    "id": meta.get("id"),
                    "quarter": meta.get("file_quarter"),
                    "year": meta.get("file_year"),
                },
            )
        )
    return out


class BluefitMonitor(CompanyMonitor):
    def __init__(self, config):
        super().__init__(config)
        self.min_expected_candidates = int(config.option("min_documents", 1))

    def _years(self) -> list[int]:
        this_year = now_local().year
        return [this_year, this_year - 1]

    def fetch_candidates(self) -> list[CandidateEvent]:
        api = self.config.option("api_url", DEFAULT_API)
        category = self.config.option("category", "dfs")
        self.source_used = SOURCE
        candidates: list[CandidateEvent] = []
        for year in self._years():
            response = http.request(
                "POST",
                api,
                json={
                    "year": str(year),
                    "categories": [category],
                    "language": "pt_BR",
                    "published": True,
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise ParserFailure("bluefit: a API não respondeu em JSON") from exc
            candidates.extend(parse_bluefit_payload(payload, self.key))
        return candidates

    def classify(self, candidate: CandidateEvent) -> str | None:
        quarter = candidate.raw.get("quarter")
        if str(quarter) == "4":
            return EventType.ANNUAL_FINANCIAL_STATEMENTS
        return EventType.QUARTERLY_RESULTS

    def normalize(self, candidate: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        period = bluefit_period(
            candidate.raw.get("quarter"), candidate.raw.get("year"), candidate.title
        )
        if not period:
            # Never drop a document silently: key it by its own id instead.
            period = f"doc-{candidate.raw.get('id') or candidate.title[:40]}"
            logger.warning(
                "company=bluefit action=unknown_period title=%r -> %s",
                candidate.title,
                period,
            )
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=candidate.title,
            source=candidate.source,
            publication_date=candidate.publication_date,
            primary_url=self.config.primary_url,
            document_url=candidate.document_url,
            document_identifier=str(candidate.raw.get("id") or "") or None,
            key_includes_event_type=False,
        )
