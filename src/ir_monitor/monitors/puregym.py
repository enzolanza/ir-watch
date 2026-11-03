"""PureGym: relatórios trimestrais (o do 4º trimestre é o anual).

Fonte: o catálogo de relatórios da plataforma Q4 do site, o mesmo que a
página "Quarterly Results"
(https://corporate.puregym.com/investor/financial-results/quarterly-results/default.aspx)
usa para montar a lista. Devolve todos os relatórios desde 2018, cada um com
o PDF do relatório, a apresentação, a gravação e a transcrição.

A v1 apontava para um endereço que foi desativado (erro 404).

Regra (anexo original, conferida em nov/2026): gera alerta o relatório
trimestral (Q1 a Q4). Relatório anual, apresentação ou gravação isolados
não geram alerta próprio.
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urljoin

from .. import http
from ..models import CandidateEvent, EventType, NormalizedEvent
from .base import CompanyMonitor, ParserFailure

logger = logging.getLogger(__name__)

SOURCE = "puregym_q4_report_feed"
BASE = "https://corporate.puregym.com"
DEFAULT_API = BASE + "/feed/FinancialReport.svc/GetFinancialReportList"
SUBTYPES = ("First Quarter", "Second Quarter", "Third Quarter", "Fourth Quarter")
_SUB_TO_Q = {s.lower(): i + 1 for i, s in enumerate(SUBTYPES)}


def _params() -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [
        ("LanguageId", "1"),
        ("reportTypes", "|".join(SUBTYPES) + "|"),
    ]
    for sub in SUBTYPES:
        params.append(("reportSubType[]", sub))
    params.append(("reportSubType[]", ""))
    for sub in SUBTYPES:
        params.append(("reportSubTypeList[]", sub))
    params += [
        ("reportSubTypeList[]", ""),
        ("pageSize", "-1"),
        ("pageNumber", "0"),
        ("tagList", ""),
        ("includeTags", "true"),
        ("year", "-1"),
        ("excludeSelection", "1"),
    ]
    return params


def _parse_date(value):
    """O catálogo usa o formato americano: "08/27/2026 00:00:00"."""
    text = str(value or "").strip()
    for fmt, size in (("%m/%d/%Y", 10), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:size], fmt).date()
        except ValueError:
            continue
    return None


def puregym_period(subtype: str, year) -> str | None:
    q = _SUB_TO_Q.get(str(subtype or "").strip().lower())
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    if not q:
        return None
    return f"Q4/FY-{y}" if q == 4 else f"Q{q}-{y}"


def parse_puregym_payload(payload: dict, company: str = "puregym") -> list[CandidateEvent]:
    reports = payload.get("GetFinancialReportListResult") if isinstance(payload, dict) else None
    if not isinstance(reports, list):
        raise ParserFailure("puregym: resposta do catálogo em formato inesperado")
    out: list[CandidateEvent] = []
    for report in reports:
        if not isinstance(report, dict):
            continue
        docs = [d for d in (report.get("Documents") or []) if isinstance(d, dict)]

        def pick(*words):
            for doc in docs:
                label = f"{doc.get('DocumentTitle', '')} {doc.get('DocumentCategory', '')}".lower()
                if any(w in label for w in words) and doc.get("DocumentPath"):
                    return urljoin(BASE, doc["DocumentPath"])
            return None

        report_url = pick("report")
        presentation = pick("presentation")
        webcast = pick("webcast")
        if report_url is None:
            pdfs = [urljoin(BASE, d["DocumentPath"]) for d in docs if str(d.get("DocumentPath", "")).lower().endswith(".pdf")]
            report_url = next((p for p in pdfs if p != presentation), None)
        title = f"{report.get('ReportSubType', '')} {report.get('ReportYear', '')}".strip()
        out.append(
            CandidateEvent(
                company=company,
                source=SOURCE,
                title=title or str(report.get("ReportTitle") or "Relatório"),
                url=BASE + "/investor/financial-results/quarterly-results/default.aspx",
                document_url=report_url,
                publication_date=_parse_date(report.get("ReportDate")),
                raw={
                    "subtype": report.get("ReportSubType"),
                    "year": report.get("ReportYear"),
                    "id": report.get("ReportId"),
                    "presentation_url": presentation,
                    "webcast_url": webcast,
                },
            )
        )
    return out


class PureGymMonitor(CompanyMonitor):
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        api = self.config.option("api_url", DEFAULT_API)
        self.source_used = SOURCE
        payload = http.get_json(api, params=_params(), headers={"Accept": "application/json"})
        return parse_puregym_payload(payload, self.key)

    def classify(self, candidate: CandidateEvent) -> str | None:
        if puregym_period(candidate.raw.get("subtype"), candidate.raw.get("year")):
            return EventType.QUARTERLY_RESULTS
        return None

    def normalize(self, candidate: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        period = puregym_period(candidate.raw.get("subtype"), candidate.raw.get("year"))
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=f"PureGym {period} Report",
            source=candidate.source,
            publication_date=candidate.publication_date,
            primary_url=self.config.primary_url,
            document_url=candidate.document_url,
            presentation_url=candidate.raw.get("presentation_url"),
            webcast_url=candidate.raw.get("webcast_url"),
            document_identifier=str(candidate.raw.get("id") or "") or None,
            key_includes_event_type=False,
        )
