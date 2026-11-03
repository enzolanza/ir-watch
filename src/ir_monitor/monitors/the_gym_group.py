"""The Gym Group: pre-close trading updates e resultados anual/semestral.

Fonte: a página "Results, Reports and Presentations"
(https://www.tggplc.com/investors/results-reports-and-presentations/), que
já vem pronta do servidor com as 12 divulgações mais recentes (data, título
e PDF). Não precisa de navegador simulado.

Regra (anexo original, conferida em nov/2026):
  gera alerta    -> Pre-close trading update (jan/jul), Full Year Results,
                    Interim Results
  não gera       -> "Notice of Pre-Close Trading Update" (aviso de data),
                    Annual Report and Accounts, visitas, outros relatórios

Atenção: o site escreve setembro como "Sept" ("09 Sept 2026"). A v1 não
entendia esse formato e descartava as divulgações em silêncio.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import slug_title, squash
from .base import CompanyMonitor, ParserFailure

logger = logging.getLogger(__name__)

SOURCE = "tgg_results_page"
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(20\d{2})\b")
YEAR_RE = re.compile(r"\b(20\d{2})\b")


def parse_tgg_date(text: str) -> date | None:
    match = DATE_RE.search(text or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(2).lower()[:4]) or _MONTHS.get(match.group(2).lower()[:3])
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def classify_tgg(title: str) -> str | None:
    low = slug_title(title)
    if re.search(r"\bnotice\s+of\b", low):
        return None  # aviso de data, não é a divulgação
    if re.search(r"annual\s+report|site\s+visit|presentation|pulse\s+report|agm\b", low):
        return None
    if "pre-close" in low or "pre close" in low or "preclose" in low:
        return EventType.PRE_CLOSE_TRADING_UPDATE
    if "trading update" in low or "trading statement" in low:
        return EventType.TRADING_UPDATE
    if re.search(r"\binterim\s+results\b|\bhalf[\s-]+year\s+results\b", low):
        return EventType.INTERIM_RESULTS
    if re.search(r"\bfull[\s-]+year\s+results\b|\bpreliminary\s+results\b|\bannual\s+results\b", low):
        return EventType.FULL_YEAR_RESULTS
    return None


def tgg_period(event_type: str, title: str, published: date | None) -> str | None:
    low = slug_title(title)
    year_match = YEAR_RE.search(low)
    year = int(year_match.group(1)) if year_match else None
    if event_type == EventType.PRE_CLOSE_TRADING_UPDATE:
        if published is None:
            return None
        # Pre-close de janeiro fala do ano anterior; o de julho, do 1º semestre.
        if published.month <= 3:
            return f"PRECLOSE-FY-{published.year - 1}"
        if 6 <= published.month <= 8:
            return f"PRECLOSE-H1-{published.year}"
        return f"PRECLOSE-{published.isoformat()}"
    if event_type == EventType.TRADING_UPDATE:
        return f"TU-{published.isoformat()}" if published else None
    if event_type == EventType.INTERIM_RESULTS:
        year = year or (published.year if published else None)
        return f"H1-{year}" if year else None
    if event_type == EventType.FULL_YEAR_RESULTS:
        if year is None and published:
            year = published.year - 1
        return f"FY-{year}" if year else None
    return None


def parse_tgg_html(html: str, page_url: str, company: str = "the_gym_group") -> list[CandidateEvent]:
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(".results-items__item")
    if not items:
        raise ParserFailure("the_gym_group: lista de divulgações não encontrada na página")
    out: list[CandidateEvent] = []
    for item in items:
        title_el = item.select_one(".results-items__title")
        date_el = item.select_one(".results-items__date")
        title = squash(title_el.get_text(" ", strip=True)) if title_el else ""
        if not title:
            continue
        document_url = presentation_url = webcast_url = None
        for anchor in item.find_all("a", href=True):
            text = slug_title(anchor.get_text(" ", strip=True))
            href = urljoin(page_url, anchor["href"].strip())
            if "presentation" in text and not presentation_url:
                presentation_url = href
            elif "webcast" in text and not webcast_url:
                webcast_url = href
            elif not document_url:
                document_url = href
        out.append(
            CandidateEvent(
                company=company,
                source=SOURCE,
                title=title,
                url=page_url,
                document_url=document_url,
                publication_date=parse_tgg_date(date_el.get_text(" ", strip=True) if date_el else ""),
                raw={"presentation_url": presentation_url, "webcast_url": webcast_url},
            )
        )
    return out


class TheGymGroupMonitor(CompanyMonitor):
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        url = self.config.primary_url
        self.source_used = SOURCE
        return parse_tgg_html(http.get_text(url), url, self.key)

    def classify(self, candidate: CandidateEvent) -> str | None:
        return classify_tgg(candidate.title)

    def normalize(self, candidate: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        period = tgg_period(event_type, candidate.title, candidate.publication_date)
        if not period:
            name = (candidate.document_url or candidate.title).rsplit("/", 1)[-1]
            period = f"doc-{name[:60]}"
            logger.warning("company=the_gym_group action=unknown_period title=%r", candidate.title)
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
            key_includes_event_type=False,
        )
