"""Benefit Systems (bolsa de Varsóvia): relatórios consolidados e cartões ativos.

Fonte: o serviço ESPI/EBI da PAP (agência de notícias oficial da Polônia),
repositório público dos comunicados obrigatórios das empresas listadas:
https://espiebi.pap.pl/wyszukiwarka?search=BENEFIT%20SYSTEMS

Por que não o site da empresa: corp.benefitsystems.pl bloqueia acessos
automáticos (erro 403) e, em nov/2026, nem abria no navegador do usuário.

Regra (anexo original, conferida em nov/2026):
  gera alerta  -> relatórios periódicos CONSOLIDADOS (trimestral QSr,
                  semestral PSr, anual SRR) e a "informação trimestral sobre
                  o número de cartões esportivos ativos"
  não gera     -> versões standalone, transações de diretores, assembleias,
                  demais comunicados

Os títulos vêm em polonês quando o acesso é feito sem navegador; o leitor
reconhece polonês e inglês.

Os relatórios periódicos não trazem o período no título, então o período é
deduzido da data de publicação, seguindo o calendário legal polonês:
  QSr em abr-jun -> 1º tri; QSr em out-dez -> 3º tri; PSr -> 1º semestre;
  SRR -> ano anterior; cartões ativos -> trimestre que acabou de terminar.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import slug_title, squash
from ..util import now_local
from .base import CompanyMonitor, ParserFailure

logger = logging.getLogger(__name__)

SOURCE = "espi_pap"
BASE = "https://espiebi.pap.pl"
SEARCH_URL = BASE + "/wyszukiwarka"
DEFAULT_SEARCH = "BENEFIT SYSTEMS"
MAX_PAGES = 6

_CARDS = re.compile(
    r"liczbie\s+aktywnych\s+kart|active\s+sports?\s+cards?"
)
_CONSOLIDATED = re.compile(r"\bskonsolidowany\b|\bconsolidated\b")
_STANDALONE = re.compile(r"\bjednostkowy\b|\bstandalone\b|\bseparate\b")
_QUARTERLY = re.compile(r"raport\s+kwartalny|quarterly\s+report|\bqsr\b")
_HALF = re.compile(r"raport\s+p[oó]łroczny|raport\s+polroczny|semi-annual\s+report|half-year(?:ly)?\s+report|\bpsr\b")
_ANNUAL = re.compile(r"raport\s+roczny|annual\s+report|\bsrr\b|\brs\b")


def classify_benefit(title: str) -> str | None:
    low = slug_title(title)
    if _CARDS.search(low):
        return EventType.ACTIVE_SPORT_CARDS_UPDATE
    # "Terminy publikacji raportów okresowych" (calendário) e semelhantes
    if re.search(r"terminy\s+publikacji|publication\s+dates|uzupe[lł]nienie|correction|korekta", low):
        return None
    if _STANDALONE.search(low) or not _CONSOLIDATED.search(low):
        return None
    if _QUARTERLY.search(low):
        return EventType.QUARTERLY_RESULTS
    if _HALF.search(low):
        return EventType.HALF_YEAR_RESULTS
    if _ANNUAL.search(low):
        return EventType.FULL_YEAR_RESULTS
    return None


def benefit_period(event_type: str, published: date | None) -> str | None:
    if published is None:
        return None
    y, m = published.year, published.month
    if event_type == EventType.ACTIVE_SPORT_CARDS_UPDATE:
        # Publicada logo após o fim do trimestre (01/10 -> 3º tri).
        ended = published - timedelta(days=20)
        q = (ended.month - 1) // 3 + 1
        return f"CARDS-Q{q}-{ended.year}"
    if event_type == EventType.QUARTERLY_RESULTS:
        if 4 <= m <= 6:
            return f"Q1-{y}"
        if 10 <= m <= 12:
            return f"Q3-{y}"
        return None
    if event_type == EventType.HALF_YEAR_RESULTS:
        return f"H1-{y}" if 7 <= m <= 10 else None
    if event_type == EventType.FULL_YEAR_RESULTS:
        return f"FY-{y - 1}" if m <= 6 else None
    return None


def parse_espi_html(html: str, company: str = "benefit_systems") -> list[CandidateEvent]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[CandidateEvent] = []
    for day in soup.select("div.day"):
        heading = day.find("h2", class_="date", recursive=False)
        if heading is None:
            continue
        try:
            published = date.fromisoformat(heading.get_text(strip=True)[:10])
        except ValueError:
            published = None
        news_list = day.find("ul", class_="newsList", recursive=False)
        if news_list is None:
            continue
        for item in news_list.find_all("li", class_="news", recursive=False):
            link = item.find("a", class_="link")
            if link is None or not link.get("href"):
                continue
            title = squash(link.get_text(" ", strip=True))
            badge = item.find(class_="badge")
            out.append(
                CandidateEvent(
                    company=company,
                    source=SOURCE,
                    title=title,
                    url=urljoin(BASE, link["href"]),
                    document_url=urljoin(BASE, link["href"]),
                    publication_date=published,
                    raw={
                        "node": link["href"].rsplit("/", 1)[-1],
                        "kind": badge.get_text(strip=True) if badge else "",
                    },
                )
            )
    return out


def _describe(html: str) -> str:
    """Resumo curto do que chegou, para diagnóstico no relatório da execução."""
    title = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    text = squash(re.sub(r"<[^>]+>", " ", html or ""))[:150]
    return f"título: {squash(title.group(1))[:80] if title else '-'}; início: {text or '-'}"


class BenefitSystemsMonitor(CompanyMonitor):
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        self.source_used = SOURCE
        search = self.config.option("search", DEFAULT_SEARCH)
        days = int(self.config.option("lookback_days", 400))
        today = now_local().date()
        start = today - timedelta(days=days)
        # Só comunicados da própria Benefit Systems S.A. (não de outras
        # empresas do grupo, como "BENEFIT SYSTEMS PARTNER sp. z o.o.").
        own = re.compile(rf"^{re.escape(slug_title(search))}(\s+s\.?\s?a\.?)?\s*[-–]")
        seen: set[str] = set()
        out: list[CandidateEvent] = []
        for page in range(MAX_PAGES):
            params = {
                "created": start.isoformat(),
                "enddate": (today + timedelta(days=1)).isoformat(),
                "search": search,
            }
            if page:
                params["page"] = str(page)
            html = http.get_text(SEARCH_URL, params=params)
            if page == 0 and "newsList" not in html and "view-wszukiwarka" not in html:
                raise ParserFailure(
                    "benefit_systems: página de busca do ESPI em formato inesperado "
                    f"({_describe(html)})"
                )
            items = parse_espi_html(html, self.key)
            fresh = [c for c in items if c.url not in seen]
            if not fresh:
                break
            for candidate in fresh:
                seen.add(candidate.url)
                # A busca é por texto: garante que o comunicado é mesmo da empresa.
                if own.match(slug_title(candidate.title)):
                    out.append(candidate)
        return out

    def classify(self, candidate: CandidateEvent) -> str | None:
        return classify_benefit(candidate.title)

    def normalize(self, candidate: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        period = benefit_period(event_type, candidate.publication_date)
        if not period:
            period = f"doc-{candidate.raw.get('node')}"
            logger.warning(
                "company=benefit_systems action=unknown_period title=%r date=%s",
                candidate.title,
                candidate.publication_date,
            )
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=candidate.title,
            source=candidate.source,
            publication_date=candidate.publication_date,
            primary_url=candidate.url,
            document_url=candidate.document_url,
            document_identifier=candidate.raw.get("node"),
            key_includes_event_type=False,
        )
