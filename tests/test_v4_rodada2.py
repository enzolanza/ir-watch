"""Rodada 2: Basic-Fit, The Gym Group e PureGym.

Tudo offline, com amostras copiadas da estrutura real observada em nov/2026.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ir_monitor import config as config_module
from ir_monitor.emailer import build_issue_title
from ir_monitor.models import EventType
from ir_monitor.monitors.base import ParserFailure
from ir_monitor.monitors.basic_fit import (
    BasicFitMonitor,
    basic_fit_period,
    classify_basic_fit,
    parse_basic_fit_html,
)
from ir_monitor.monitors.puregym import PureGymMonitor, parse_puregym_payload, puregym_period
from ir_monitor.monitors.the_gym_group import (
    TheGymGroupMonitor,
    classify_tgg,
    parse_tgg_date,
    parse_tgg_html,
    tgg_period,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def company(key: str):
    return config_module.load_companies()[key]


# ==========================================================================
# Basic-Fit
# ==========================================================================
class TestBasicFit:
    URL = "https://corporate.basic-fit.com/investors/financial-results"

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Q3 2026 Trading Update", EventType.TRADING_UPDATE),
            ("Trading Update Q3 2022", EventType.TRADING_UPDATE),
            ("January Trading Update", EventType.TRADING_UPDATE),
            ("Half Year 2026", EventType.HALF_YEAR_RESULTS),
            ("Full Year 2025", EventType.FULL_YEAR_RESULTS),
            ("Capital Markets day 2026", None),
            (" Capital Markets Day 2023", None),
        ],
    )
    def test_classification(self, title, expected):
        assert classify_basic_fit(title) == expected

    def test_periods(self):
        tu = EventType.TRADING_UPDATE
        assert basic_fit_period(tu, "Q3 2026 Trading Update", date(2026, 10, 21)) == "TU-Q3-2026"
        assert basic_fit_period(tu, "Trading Update Q1 2022", date(2022, 4, 21)) == "TU-Q1-2022"
        assert basic_fit_period(tu, "January Trading Update", date(2026, 1, 26)) == "JAN-TU-2026"
        assert basic_fit_period(EventType.HALF_YEAR_RESULTS, "Half Year 2026", None) == "H1-2026"
        assert basic_fit_period(EventType.FULL_YEAR_RESULTS, "Full Year 2025", None) == "FY-2025"

    def test_reads_the_embedded_list(self, monkeypatch):
        from ir_monitor.monitors import basic_fit as module

        monkeypatch.setattr(module.http, "get_text", lambda url, **k: load("basic_fit_financial_results.html"))
        _, events = BasicFitMonitor(company("basic_fit")).run()
        periods = {e.reporting_period: e for e in events}
        assert len(events) == 16  # 18 itens menos os 2 Capital Markets Day
        q3 = periods["TU-Q3-2026"]
        assert q3.publication_date == date(2026, 10, 21)
        assert q3.document_url.endswith(".pdf")
        assert q3.event_key == "basic_fit|TU-Q3-2026"
        assert "JAN-TU-2026" in periods and "H1-2026" in periods and "FY-2025" in periods
        assert build_issue_title(q3) == "Basic-Fit — Trading update — TU-Q3-2026"

    def test_missing_data_block_is_a_failure(self):
        with pytest.raises(ParserFailure):
            parse_basic_fit_html("<html><body>Access denied</body></html>", self.URL)

    def test_list_without_reports_is_a_failure(self):
        html = '<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>'
        with pytest.raises(ParserFailure):
            parse_basic_fit_html(html, self.URL)

    def test_too_few_relevant_is_a_failure(self, monkeypatch):
        from ir_monitor.monitors import basic_fit as module

        only_cmd = load("basic_fit_financial_results.html")
        data = only_cmd.split('type="application/json">')[1].split("</script>")[0]
        parsed = json.loads(data)
        items = parsed["props"]["pageProps"]["page"]["bodyCollection"]["items"][1]["collection"]["items"]
        parsed["props"]["pageProps"]["page"]["bodyCollection"]["items"][1]["collection"]["items"] = items[:3]
        html = only_cmd.replace(data, json.dumps(parsed))
        monkeypatch.setattr(module.http, "get_text", lambda url, **k: html)
        with pytest.raises(ParserFailure):
            BasicFitMonitor(company("basic_fit")).run()


# ==========================================================================
# The Gym Group
# ==========================================================================
class TestTheGymGroup:
    URL = "https://www.tggplc.com/investors/results-reports-and-presentations/"

    def test_sept_is_understood(self):
        # O bug da v1: "Sept" não era reconhecido e a divulgação sumia.
        assert parse_tgg_date("09 Sept 2026") == date(2026, 9, 9)
        assert parse_tgg_date("08 Jul 2026") == date(2026, 7, 8)

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Pre-close trading update", EventType.PRE_CLOSE_TRADING_UPDATE),
            ("Notice of Pre-Close Trading Update", None),
            ("Interim Results for the six month period ended 30th June 2026", EventType.INTERIM_RESULTS),
            ("Full Year Results for the year ended 31 December 2025", EventType.FULL_YEAR_RESULTS),
            ("The Gym Group Annual Report and Accounts 2025", None),
            ("Gym Group Site Visit 2026", None),
            ("Gen Z Fitness Pulse Report 2026", None),
        ],
    )
    def test_classification(self, title, expected):
        assert classify_tgg(title) == expected

    def test_pre_close_period_comes_from_the_date(self):
        pc = EventType.PRE_CLOSE_TRADING_UPDATE
        assert tgg_period(pc, "Pre-close trading update", date(2026, 1, 13)) == "PRECLOSE-FY-2025"
        assert tgg_period(pc, "Pre-close trading update", date(2026, 7, 8)) == "PRECLOSE-H1-2026"

    def test_reads_the_page(self, monkeypatch):
        from ir_monitor.monitors import the_gym_group as module

        monkeypatch.setattr(module.http, "get_text", lambda url, **k: load("tgg_results_page.html"))
        _, events = TheGymGroupMonitor(company("the_gym_group")).run()
        periods = {e.reporting_period: e for e in events}
        assert set(periods) == {
            "H1-2026", "PRECLOSE-H1-2026", "FY-2025", "PRECLOSE-FY-2025", "H1-2025", "PRECLOSE-H1-2025", "FY-2024",
        }
        h1 = periods["H1-2026"]
        assert h1.publication_date == date(2026, 9, 9)
        assert h1.document_url == "https://www.tggplc.com/media/02sjcows/rns-hy26-final.pdf"
        assert h1.presentation_url.endswith("tgg-2026-half-year-results.pdf")
        assert build_issue_title(h1) == "The Gym Group — Resultado semestral — H1-2026"

    def test_layout_change_is_a_failure(self):
        with pytest.raises(ParserFailure):
            parse_tgg_html("<html><h1>Results</h1><a href='/x.pdf'>x</a></html>", self.URL)


# ==========================================================================
# PureGym
# ==========================================================================
class TestPureGym:
    def test_periods(self):
        assert puregym_period("Second Quarter", 2026) == "Q2-2026"
        assert puregym_period("Fourth Quarter", "2025") == "Q4/FY-2025"
        assert puregym_period("Annual Report", 2025) is None

    def test_reads_the_catalogue(self, monkeypatch):
        from ir_monitor.monitors import puregym as module

        captured = {}

        def fake_get_json(url, **kwargs):
            captured.update(kwargs)
            return json.loads(load("puregym_reports.json"))

        monkeypatch.setattr(module.http, "get_json", fake_get_json)
        _, events = PureGymMonitor(company("puregym")).run()
        periods = {e.reporting_period: e for e in events}
        assert len(events) == 10
        q2 = periods["Q2-2026"]
        assert q2.publication_date == date(2026, 8, 27)
        assert q2.document_url.endswith("PureGym-Q2-26-Report.pdf")  # o relatório, não a apresentação
        assert q2.presentation_url.endswith("Presentation.pdf")
        assert q2.event_key == "puregym|Q2-2026"
        assert periods["Q4/FY-2025"].publication_date == date(2026, 3, 19)
        assert ("pageSize", "-1") in captured["params"]
        assert build_issue_title(q2) == "PureGym — Relatório trimestral — Q2-2026"

    def test_bad_shape_is_a_failure(self):
        with pytest.raises(ParserFailure):
            parse_puregym_payload({"error": "x"})
