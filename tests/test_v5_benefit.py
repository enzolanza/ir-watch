"""Rodada 3: Benefit Systems via ESPI/EBI (PAP).

Tudo offline, com amostras copiadas da estrutura real observada em nov/2026.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ir_monitor import config as config_module
from ir_monitor.emailer import build_issue_title
from ir_monitor.models import EventType
from ir_monitor.monitors.base import ParserFailure
from ir_monitor.monitors.benefit_systems import (
    BenefitSystemsMonitor,
    benefit_period,
    classify_benefit,
    parse_espi_html,
)

FIXTURES = Path(__file__).parent / "fixtures"
P = "BENEFIT SYSTEMS S.A. - "


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "title,expected",
    [
        (P + "Informacja kwartalna o liczbie aktywnych kart sportowych", EventType.ACTIVE_SPORT_CARDS_UPDATE),
        (P + "Quarterly information on the number of active sports cards", EventType.ACTIVE_SPORT_CARDS_UPDATE),
        (P + "Skonsolidowany raport kwartalny QSr", EventType.QUARTERLY_RESULTS),
        (P + "Skonsolidowany raport półroczny  PSr", EventType.HALF_YEAR_RESULTS),
        (P + "Consolidated semi-annual report PSr", EventType.HALF_YEAR_RESULTS),
        (P + "Skonsolidowany raport roczny  SRR", EventType.FULL_YEAR_RESULTS),
        (P + "Raport roczny  R", None),  # standalone: não gera alerta
        (P + "Terminy publikacji raportów okresowych w 2026 r.", None),
        (P + "Powiadomienie o transakcjach dokonanych przez osoby pełniące obowiązki zarządcze", None),
    ],
)
def test_classification(title, expected):
    assert classify_benefit(title) == expected


def test_periods_follow_the_polish_calendar():
    assert benefit_period(EventType.ACTIVE_SPORT_CARDS_UPDATE, date(2026, 10, 1)) == "CARDS-Q3-2026"
    assert benefit_period(EventType.ACTIVE_SPORT_CARDS_UPDATE, date(2026, 1, 7)) == "CARDS-Q4-2025"
    assert benefit_period(EventType.QUARTERLY_RESULTS, date(2026, 5, 14)) == "Q1-2026"
    assert benefit_period(EventType.QUARTERLY_RESULTS, date(2025, 11, 14)) == "Q3-2025"
    assert benefit_period(EventType.HALF_YEAR_RESULTS, date(2026, 8, 20)) == "H1-2026"
    assert benefit_period(EventType.FULL_YEAR_RESULTS, date(2026, 3, 20)) == "FY-2025"


def test_parses_the_search_page():
    items = parse_espi_html(load("espi_benefit_page0.html"))
    assert len(items) == 9
    first = items[0]
    assert first.publication_date == date(2026, 10, 20)
    assert first.url == "https://espiebi.pap.pl/node/739701"


@pytest.fixture()
def espi(monkeypatch):
    from ir_monitor.monitors import benefit_systems as module

    pages = {
        None: load("espi_benefit_page0.html"),
        "1": load("espi_benefit_page1.html"),
    }
    calls = []

    def fake_get_text(url, **kwargs):
        params = kwargs.get("params") or {}
        calls.append(params)
        return pages.get(params.get("page"), load("espi_benefit_empty.html"))

    monkeypatch.setattr(module.http, "get_text", fake_get_text)
    return calls


def test_monitor_reads_all_pages_and_keeps_only_relevant(espi):
    monitor = BenefitSystemsMonitor(config_module.load_companies()["benefit_systems"])
    _, events = monitor.run()
    by_period = {e.reporting_period: e for e in events}
    assert set(by_period) == {
        "CARDS-Q3-2026", "H1-2026", "CARDS-Q2-2026", "Q1-2026", "CARDS-Q1-2026",
        "FY-2025", "CARDS-Q4-2025", "Q3-2025", "CARDS-Q3-2025",
    }
    # Leu a página 0, a 1 e parou na primeira vazia.
    assert [c.get("page") for c in espi] == [None, "1", "2"]
    assert espi[0]["search"] == "BENEFIT SYSTEMS"
    cards = by_period["CARDS-Q3-2026"]
    assert cards.event_key == "benefit_systems|CARDS-Q3-2026"
    assert cards.document_url == "https://espiebi.pap.pl/node/738487"
    assert build_issue_title(cards) == (
        "Benefit Systems — Número de cartões esportivos ativos — CARDS-Q3-2026"
    )
    assert build_issue_title(by_period["H1-2026"]) == (
        "Benefit Systems — Relatório semestral consolidado — H1-2026"
    )


def test_other_companies_with_similar_names_are_ignored(espi):
    monitor = BenefitSystemsMonitor(config_module.load_companies()["benefit_systems"])
    candidates, _ = monitor.run()
    assert all("PARTNER" not in c.title for c in candidates)


def test_unexpected_page_is_a_failure(monkeypatch):
    from ir_monitor.monitors import benefit_systems as module

    monkeypatch.setattr(module.http, "get_text", lambda url, **k: "<html>Service unavailable</html>")
    with pytest.raises(ParserFailure):
        BenefitSystemsMonitor(config_module.load_companies()["benefit_systems"]).run()
