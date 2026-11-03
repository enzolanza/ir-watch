"""Selfit: demonstrações financeiras anuais.

Fonte: a página https://www.selfitacademias.com.br/investidores, que já vem
pronta do servidor (não precisa de navegador simulado).

A página tem várias seções, cada uma com um título <h2>. Só interessa a
seção "Demonstrações financeiras anuais", onde cada link é um ano (2016,
2017, ... 2025) apontando para um PDF. As seções seguintes (assembleias,
atas, avisos) são ignoradas.
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

SOURCE = "selfit_investidores"
_YEAR = re.compile(r"\b(20\d{2})\b")


def _is_section_title(text: str) -> bool:
    low = slug_title(text)
    return "demonstrac" in low and "financeiras anuais" in low


def parse_selfit_html(html: str, base_url: str, company: str = "selfit") -> list[CandidateEvent]:
    soup = BeautifulSoup(html, "html.parser")
    heading = next(
        (h for h in soup.find_all(["h1", "h2", "h3"]) if _is_section_title(h.get_text(" "))),
        None,
    )
    if heading is None:
        raise ParserFailure(
            "selfit: seção 'Demonstrações financeiras anuais' não encontrada na página"
        )

    out: list[CandidateEvent] = []
    for element in heading.find_all_next():
        if element.name in {"h1", "h2", "h3"} and element is not heading:
            break  # chegou na próxima seção
        if element.name != "a" or not element.get("href"):
            continue
        href = urljoin(base_url, element["href"].strip())
        text = squash(element.get_text(" ", strip=True))
        label = text or squash(element.get("title") or "")
        match = _YEAR.search(label) or _YEAR.search(element.get("title") or "")
        year = int(match.group(1)) if match else None
        out.append(
            CandidateEvent(
                company=company,
                source=SOURCE,
                title=f"Demonstrações financeiras anuais {year}" if year else (label or href),
                url=base_url,
                document_url=href,
                raw={"year": year, "link_text": label},
            )
        )
    return out


class SelfitMonitor(CompanyMonitor):
    def __init__(self, config):
        super().__init__(config)
        self.min_expected_candidates = int(config.option("min_documents", 5))

    def fetch_candidates(self) -> list[CandidateEvent]:
        url = self.config.primary_url
        self.source_used = SOURCE
        html = http.get_text(url)
        return parse_selfit_html(html, url, self.key)

    def classify(self, candidate: CandidateEvent) -> str | None:
        return EventType.ANNUAL_FINANCIAL_STATEMENTS

    def normalize(self, candidate: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        year = candidate.raw.get("year")
        if year:
            period = f"Anual {year}"
        else:
            name = (candidate.document_url or candidate.title).rsplit("/", 1)[-1]
            period = f"doc-{name[:60]}"
            logger.warning("company=selfit action=unknown_year link=%r", name)
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
