"""Bodytech: demonstrações financeiras anuais.

Fonte: o catálogo interno que a página "Políticas e documentos"
(https://www.bodytech.com.br/pt/politicas/?topico=2) consulta em segundo
plano: https://www.bodytech.com.br/novo-portal/api/documents/list-all

O catálogo devolve uma lista de itens. Só interessa o item cujo título é
exatamente "Demonstrações financeiras" (há outros parecidos, como o da
Central de Balanços do SPED e "Outras publicações legais", que são
ignorados). Dentro dele, cada link "... EXERCÍCIO DE 2025" é um ano.

Não precisa de navegador simulado nem do SPED.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import slug_title, squash
from .base import CompanyMonitor, ParserFailure

logger = logging.getLogger(__name__)

DEFAULT_API = "https://www.bodytech.com.br/novo-portal/api/documents/list-all"
MEDIA_BASE = "https://medias.bodytech.com.br/"
SOURCE = "bodytech_documents_api"
_EXERCISE = re.compile(r"exerc[ií]cio\s+(?:social\s+)?(?:de\s+)?(20\d{2})", re.I)
_YEAR = re.compile(r"(20\d{2})")


def _year_of(text: str, href: str) -> int | None:
    match = _EXERCISE.search(text or "") or _YEAR.search(text or "") or _YEAR.search(
        href.rsplit("/", 1)[-1]
    )
    return int(match.group(1)) if match else None


def parse_bodytech_payload(
    payload: dict, page_url: str, company: str = "bodytech"
) -> list[CandidateEvent]:
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ParserFailure("bodytech: resposta do catálogo em formato inesperado")

    target = next(
        (
            item
            for item in items
            if isinstance(item, dict)
            and slug_title(str(item.get("title") or "")).strip() == "demonstracoes financeiras"
        ),
        None,
    )
    if target is None:
        raise ParserFailure(
            "bodytech: item 'Demonstrações financeiras' não encontrado no catálogo"
        )

    soup = BeautifulSoup(str(target.get("content") or ""), "html.parser")
    seen: set[str] = set()
    out: list[CandidateEvent] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(MEDIA_BASE, anchor["href"].strip())
        if href in seen:
            continue
        seen.add(href)
        text = squash(anchor.get_text(" ", strip=True))
        year = _year_of(text, href)
        out.append(
            CandidateEvent(
                company=company,
                source=SOURCE,
                title=f"Demonstrações financeiras {year}" if year else (text or href),
                url=page_url,
                document_url=href,
                raw={"year": year, "link_text": text},
            )
        )

    # O campo "link" aponta para o documento mais recente. Garante que ele
    # entre mesmo se, por algum motivo, não estiver no texto do item.
    latest = target.get("link")
    if latest:
        href = urljoin(MEDIA_BASE, str(latest).lstrip("/"))
        if href not in seen:
            year = _year_of("", href)
            out.append(
                CandidateEvent(
                    company=company,
                    source=SOURCE,
                    title=f"Demonstrações financeiras {year}" if year else href,
                    url=page_url,
                    document_url=href,
                    raw={"year": year, "link_text": ""},
                )
            )
    return out


class BodytechMonitor(CompanyMonitor):
    def __init__(self, config):
        super().__init__(config)
        self.min_expected_candidates = int(config.option("min_documents", 5))

    def fetch_candidates(self) -> list[CandidateEvent]:
        api = self.config.option("api_url", DEFAULT_API)
        self.source_used = SOURCE
        payload = http.get_json(api, headers={"Accept": "application/json"})
        return parse_bodytech_payload(payload, self.config.primary_url, self.key)

    def classify(self, candidate: CandidateEvent) -> str | None:
        return EventType.ANNUAL_FINANCIAL_STATEMENTS

    def normalize(self, candidate: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        year = candidate.raw.get("year")
        if year:
            period = f"Anual {year}"
        else:
            name = (candidate.document_url or candidate.title).rsplit("/", 1)[-1]
            period = f"doc-{name[:60]}"
            logger.warning("company=bodytech action=unknown_year link=%r", name)
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=candidate.title,
            source=candidate.source,
            primary_url=self.config.primary_url,
            document_url=candidate.document_url,
            key_includes_event_type=False,
        )
