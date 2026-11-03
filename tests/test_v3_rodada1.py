"""Rodada 1: Planet Fitness, Xponential, Sports World, SATS e Leejam.

Tudo offline, com amostras copiadas da estrutura real observada em nov/2026.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from ir_monitor import config as config_module
from ir_monitor import database as db_module
from ir_monitor.config import CompanyConfig
from ir_monitor.database import Event, Meta
from ir_monitor.emailer import ConsoleEmailSender, build_issue_title
from ir_monitor.models import CandidateEvent, EventType, NormalizedEvent
from ir_monitor.monitors import REGISTRY
from ir_monitor.monitors.base import CompanyMonitor, ParserFailure
from ir_monitor.monitors.leejam import (
    LeejamMonitor,
    is_results_announcement,
    leejam_period,
    parse_euroland_payload,
)
from ir_monitor.monitors.xponential import XponentialMonitor

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ==========================================================================
# Leejam
# ==========================================================================
class TestLeejamPeriods:
    def test_periods_from_the_period_end_date(self):
        p = "Leejam Sports Company Announces Its Interim Consolidated Financial Results "
        assert leejam_period(p + "for the Period Ended on 30 September 2026 (Nine Months)") == "Q3/9M-2026"
        assert leejam_period(p + "for the Period Ended On 30th June 2025 (Six months)") == "Q2/H1-2025"
        assert leejam_period(p + "for the Period Ended On 31 March 2026 (Three months)") == "Q1-2026"
        assert (
            leejam_period(
                "Leejam Sports Company Announces Its Annual Consolidated Financial Results "
                "for the year Ended in On 31 December 2025 (Twelve months)"
            )
            == "Q4/FY-2025"
        )

    def test_only_results_announcements_are_relevant(self):
        assert is_results_announcement(
            "Leejam Sports Company Announces Its Interim Consolidated Financial Results for the Period Ended on 30 September 2026 (Nine Months)"
        )
        assert not is_results_announcement(
            "Leejam Sports Company Announces the Distribution of Cash Dividend for The Third Quarter of 2026"
        )
        assert not is_results_announcement(
            "Leejam Sports Company Announces the Opening of a New Men Center in Riyadh"
        )


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


@pytest.fixture()
def leejam_api(monkeypatch):
    from ir_monitor.monitors import leejam as leejam_module

    pages = {
        "2026": json.loads(load("leejam_euroland_2026.json")),
        "2025": json.loads(load("leejam_euroland_2025.json")),
    }
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, kwargs["data"]))
        return FakeResponse(pages[kwargs["data"]["strDateFrom"][-4:]])

    monkeypatch.setattr(leejam_module.http, "request", fake_request)
    monkeypatch.setattr(LeejamMonitor, "_years", lambda self: [2026, 2025])
    return calls


class TestLeejamMonitor:
    def test_reads_the_announcements_panel(self, leejam_api):
        monitor = LeejamMonitor(config_module.load_companies()["leejam"])
        _, events = monitor.run()
        by_period = {e.reporting_period: e for e in events}
        assert set(by_period) == {"Q3/9M-2026", "Q2/H1-2026", "Q1-2026", "Q4/FY-2025", "Q3/9M-2025", "Q2/H1-2025"}
        q3 = by_period["Q3/9M-2026"]
        assert q3.event_key == "leejam|Q3/9M-2026"
        assert q3.publication_date == date(2026, 11, 2)
        assert "ID=8166368" in q3.document_url
        assert by_period["Q4/FY-2025"].event_type == EventType.FULL_YEAR_RESULTS
        assert q3.event_type == EventType.INTERIM_RESULTS
        assert [c[0] for c in leejam_api] == ["POST", "POST"]
        assert leejam_api[0][1]["companyCode"] == "sa-1830"

    def test_bad_shape_is_a_failure(self):
        with pytest.raises(ParserFailure):
            parse_euroland_payload({"total": 0})

    def test_no_results_announcements_is_a_failure(self, monkeypatch):
        from ir_monitor.monitors import leejam as leejam_module

        only_noise = {"News": [{"title": "Leejam Sports Company Announces the Opening of a New Center", "ID": 1, "date": "/Date(1765090859000)/"}]}
        monkeypatch.setattr(leejam_module.http, "request", lambda *a, **k: FakeResponse(only_noise))
        monitor = LeejamMonitor(config_module.load_companies()["leejam"])
        with pytest.raises(ParserFailure):
            monitor.run()

    def test_issue_title(self, leejam_api):
        _, events = LeejamMonitor(config_module.load_companies()["leejam"]).run()
        q3 = next(e for e in events if e.reporting_period == "Q3/9M-2026")
        assert build_issue_title(q3) == "Leejam — Resultado trimestral — Q3/9M-2026"


# ==========================================================================
# Xponential: página + feed
# ==========================================================================
class TestXponentialBothSources:
    def _monitor(self, monkeypatch, page_ok=True, rss_ok=True):
        from ir_monitor.monitors import xponential as xpof_module

        def fake_get_text(url, **kwargs):
            if "rss" in url:
                if not rss_ok:
                    raise RuntimeError("HTTP 503")
                return load("xponential_rss.xml")
            if not page_ok:
                raise RuntimeError("HTTP 503")
            return load("xponential_quarterly.html")

        monkeypatch.setattr(xpof_module.http, "get_text", fake_get_text)
        from ir_monitor.monitors import base as base_module

        monkeypatch.setattr(base_module.http, "get_text", fake_get_text)
        config = CompanyConfig(
            key="xponential",
            name="Xponential Fitness",
            monitor="xponential",
            primary_url="https://investor.xponential.com/financial-information/quarterly-results",
            options={"rss_url": "https://investor.xponential.com/news/rss"},
        )
        return XponentialMonitor(config)

    def test_feed_result_counts_even_before_the_page_is_updated(self, monkeypatch):
        _, events = self._monitor(monkeypatch).run()
        periods = {e.reporting_period for e in events}
        # Página (amostra): Q1-2026 e Q4/FY-2025. Feed: Q3-2026 e Q2-2026.
        assert {"Q3-2026", "Q2-2026", "Q1-2026", "Q4/FY-2025"} <= periods
        # "to Announce ..." (aviso de data) não é resultado.
        assert all("to announce" not in e.title.lower() for e in events)

    def test_same_quarter_from_both_sources_is_one_event(self, monkeypatch):
        from ir_monitor.monitors import xponential as xpof_module

        rss = load("xponential_rss.xml").replace(
            "Announces Second Quarter 2026", "Announces First Quarter 2026"
        )
        monitor = self._monitor(monkeypatch)
        original = xpof_module.http.get_text
        monkeypatch.setattr(
            xpof_module.http, "get_text", lambda url, **k: rss if "rss" in url else original(url)
        )
        from ir_monitor.monitors import base as base_module

        monkeypatch.setattr(
            base_module.http, "get_text", lambda url, **k: rss if "rss" in url else original(url)
        )
        _, events = monitor.run()
        keys = [e.event_key for e in events]
        assert keys.count("xponential|earnings_release|Q1-2026") == 1

    def test_one_source_down_still_works(self, monkeypatch):
        _, events = self._monitor(monkeypatch, page_ok=False).run()
        assert "Q3-2026" in {e.reporting_period for e in events}
        _, events = self._monitor(monkeypatch, rss_ok=False).run()
        assert "Q1-2026" in {e.reporting_period for e in events}

    def test_both_sources_down_is_a_failure(self, monkeypatch):
        with pytest.raises(ParserFailure):
            self._monitor(monkeypatch, page_ok=False, rss_ok=False).run()


# ==========================================================================
# Proteção geral: "ok" sem nenhum documento relevante = falha
# ==========================================================================
class OnlyNoise(CompanyMonitor):
    min_expected_candidates = 1

    def fetch_candidates(self):
        return [CandidateEvent(company=self.key, source="s", title="Menu: Contact us")]

    def classify(self, candidate):
        return None

    def normalize(self, candidate, event_type):  # pragma: no cover
        return None


def test_zero_relevant_is_a_failure_when_configured():
    noisy = CompanyConfig(key="x", name="X", monitor="x", options={"min_relevant": 1})
    with pytest.raises(ParserFailure):
        OnlyNoise(noisy).run()
    quiet_ok = CompanyConfig(key="x", name="X", monitor="x", options={"min_relevant": 0})
    assert OnlyNoise(quiet_ok).run()[1] == []


def test_every_enabled_company_except_planet_fitness_has_a_floor():
    companies = config_module.load_companies()
    for key, company in companies.items():
        if not company.enabled or key == "planet_fitness":
            continue
        floor = int(company.option("min_relevant", 0) or 0) or int(
            company.option("min_documents", 0) or 0
        )
        assert floor >= 1, f"{key} sem piso mínimo de documentos"


# ==========================================================================
# Limpeza única do histórico da Leejam
# ==========================================================================
@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'test.db'}")
    config_module.reset_caches()
    db_module.reset_engine()
    db_module.init_db()
    yield
    db_module.reset_engine()
    config_module.reset_caches()


def test_leejam_history_is_reset_once_and_others_are_kept(db, leejam_api, monkeypatch):
    from ir_monitor.services import runner as runner_module
    from ir_monitor.util import now_local

    now = now_local()
    with db_module.session_scope() as session:
        db_module.mark_bootstrapped(session)
        for company, key in (("leejam", "leejam|Q4/FY-2026"), ("sats", "sats|Q3-2026")):
            session.add(
                Event(
                    company=company, event_type="x", reporting_period=key.split("|")[1],
                    event_key=key, is_baseline=True, first_seen=now, created_at=now, updated_at=now,
                )
            )
        # As limpezas da v2 já rodaram neste banco.
        session.add(Meta(key=runner_module.V2_RESET_FLAG, value=now.isoformat()))

    leejam = config_module.load_companies()["leejam"]
    monkeypatch.setattr(runner_module, "select_companies", lambda only=None: [leejam])
    sender = ConsoleEmailSender()
    summary = runner_module.run(mode="check", sender=sender)

    assert summary.alerts_sent == 0
    assert [n[1] for n in sender.notices] == ["Linha de base registrada — Leejam"]
    assert "Q3/9M-2026" in sender.notices[0][2]
    with db_module.session_scope() as session:
        keys = {e.event_key for e in session.execute(select(Event)).scalars()}
        assert "leejam|Q4/FY-2026" not in keys  # o registro errado sumiu
        assert "sats|Q3-2026" in keys  # as outras empresas não são tocadas
        assert "leejam|Q3/9M-2026" in keys
        assert session.get(Meta, "v3_reset_leejam_done") is not None

    # Segunda execução: não limpa de novo, não alerta de novo.
    sender = ConsoleEmailSender()
    summary = runner_module.run(mode="check", sender=sender)
    assert summary.alerts_sent == 0 and sender.notices == []
