"""Leejam Sports Company (bolsa saudita, código 1830).

Fonte: o painel "News and Announcements" do site de RI da Leejam
(https://leejam.com.sa/investor-relations/disclosures-and-events/). Ele é
fornecido pela Euroland e reproduz, no mesmo dia, os comunicados oficiais
que a Leejam faz à bolsa saudita. O robô consulta a mesma lista que o painel
usa: https://ksatools.eurolandir.com/tools/Pressreleases/Main/GetNews/

Por que não a página "Result Center": ela é atualizada dias ou semanas
depois do comunicado (o 3º trimestre de 2026 saiu na bolsa em 02/11/2026 e
ainda não estava lá no dia seguinte). A bolsa em si bloqueia o acesso.

Regra: alerta nos comunicados "Announces Its Interim/Annual Consolidated
Financial Results ...". Dividendos, aberturas de academias, assembleias e
contratos são ignorados.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import slug_title, squash
from ..util import now_local
from .base import CompanyMonitor, ParserFailure

logger = logging.getLogger(__name__)

SOURCE = "euroland_announcements"
DEFAULT_API = "https://ksatools.eurolandir.com/tools/Pressreleases/Main/GetNews/"
DEFAULT_COMPANY_CODE = "sa-1830"
RELEASE_URL = (
    "https://ksatools.eurolandir.com/tools/Pressreleases/GetPressRelease/"
    "?ID={id}&lang=en-GB&companycode={code}"
)

RESULTS_RE = re.compile(r"\b(interim|annual)\s+consolidated\s+financial\s+results\b")
REJECT_RE = re.compile(r"\b(dividend|standalone|stand-alone)\b")
MONTH_END_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(march|june|september|december)\s+(20\d{2})\b"
)
ISO_END_RE = re.compile(r"\b(20\d{2})-(03|06|09|12)-(\d{2})\b")
SPAN_RE = re.compile(r"\((three|six|nine|twelve)\s+months?\)")
YEAR_RE = re.compile(r"\b(20\d{2})\b")
_MONTH_TO_Q = {"march": 1, "june": 2, "september": 3, "december": 4}
_ISO_TO_Q = {"03": 1, "06": 2, "09": 3, "12": 4}
_SPAN_TO_Q = {"three": 1, "six": 2, "nine": 3, "twelve": 4}
_LABEL = {1: "Q1-{y}", 2: "Q2/H1-{y}", 3: "Q3/9M-{y}", 4: "Q4/FY-{y}"}


def leejam_period(title: str) -> str | None:
    """Período a partir da data de encerramento citada no título."""
    low = slug_title(title)
    match = MONTH_END_RE.search(low)
    if match:
        return _LABEL[_MONTH_TO_Q[match.group(2)]].format(y=match.group(3))
    match = ISO_END_RE.search(low)
    if match:
        return _LABEL[_ISO_TO_Q[match.group(2)]].format(y=match.group(1))
    span = SPAN_RE.search(low)
    year = YEAR_RE.search(low)
    if span and year:
        return _LABEL[_SPAN_TO_Q[span.group(1)]].format(y=year.group(1))
    return None


def is_results_announcement(title: str) -> bool:
    low = slug_title(title)
    return bool(RESULTS_RE.search(low)) and not REJECT_RE.search(low)


def _parse_ms_date(value):
    """Formato da Euroland: "/Date(1765090859000)/"."""
    match = re.search(r"(\d{10,})", str(value or ""))
    if not match:
        return None
    try:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc).date()
    except (OverflowError, ValueError, OSError):
        return None


def parse_euroland_payload(
    payload: dict, company: str = "leejam", code: str = DEFAULT_COMPANY_CODE
) -> list[CandidateEvent]:
    if not isinstance(payload, dict) or not isinstance(payload.get("News"), list):
        raise ParserFailure("leejam: resposta do painel de comunicados em formato inesperado")
    out: list[CandidateEvent] = []
    for item in payload["News"]:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        news_id = item.get("ID")
        out.append(
            CandidateEvent(
                company=company,
                source=SOURCE,
                title=squash(str(item["title"])),
                url=RELEASE_URL.format(id=news_id, code=code) if news_id else None,
                publication_date=_parse_ms_date(item.get("date")),
                raw={"id": news_id, "type": item.get("type")},
            )
        )
    return out


class LeejamMonitor(CompanyMonitor):
    min_expected_candidates = 1

    def _years(self) -> list[int]:
        year = now_local().year
        return [year, year - 1]

    def fetch_candidates(self) -> list[CandidateEvent]:
        api = self.config.option("api_url", DEFAULT_API)
        code = self.config.option("company_code", DEFAULT_COMPANY_CODE)
        self.source_used = SOURCE
        out: list[CandidateEvent] = []
        for year in self._years():
            form = {
                "strDateFrom": f"01/01/{year}",
                "strDateTo": f"31/12/{year}",
                "typeFilter": "",
                "orderBy": "0",
                "pageIndex": "0",
                "pageJummp": "100",
                "hasTypeFilter": "false",
                "searchPhrase": "",
                "companyCode": code,
                "onlyInsiderInfo": "false",
                "lang": "en-GB",
                "v": "",
                "alwaysIncludeInsiders": "false",
                "strYears": "",
            }
            response = http.request(
                "POST",
                api,
                data=form,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*",
                },
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise ParserFailure("leejam: o painel de comunicados não respondeu em JSON") from exc
            out.extend(parse_euroland_payload(payload, self.key, code))
        return out

    def classify(self, candidate: CandidateEvent) -> str | None:
        if not is_results_announcement(candidate.title):
            return None
        if "annual" in slug_title(candidate.title).split("consolidated")[0]:
            return EventType.FULL_YEAR_RESULTS
        return EventType.INTERIM_RESULTS

    def normalize(self, candidate: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        period = leejam_period(candidate.title)
        if not period:
            # Nunca descartar em silêncio: usa o número do comunicado.
            period = f"doc-{candidate.raw.get('id')}"
            logger.warning(
                "company=leejam action=unknown_period title=%r -> %s", candidate.title, period
            )
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=candidate.title,
            source=candidate.source,
            publication_date=candidate.publication_date,
            primary_url=candidate.url,
            document_url=candidate.url,
            document_identifier=str(candidate.raw.get("id") or "") or None,
            key_includes_event_type=False,
        )
