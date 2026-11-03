"""v2: Bluefit, Selfit e Bodytech + proteções novas.

Tudo offline: as respostas dos sites vêm de tests/fixtures, copiadas da
estrutura real observada em nov/2026.
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
from ir_monitor.emailer import (
    ConsoleEmailSender,
    EmailSender,
    EmailSendError,
    build_issue_title,
    render_issue_body,
)
from ir_monitor.models import EventType, NormalizedEvent
from ir_monitor.monitors import REGISTRY
from ir_monitor.monitors.base import CompanyMonitor, ParserFailure
from ir_monitor.monitors.bluefit import BluefitMonitor, bluefit_period, parse_bluefit_payload
from ir_monitor.monitors.bodytech import BodytechMonitor, parse_bodytech_payload
from ir_monitor.monitors.selfit import SelfitMonitor, parse_selfit_html

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def company(key: str, **options) -> CompanyConfig:
    return config_module.load_companies()[key] if not options else CompanyConfig(
        key=key, name=key.capitalize(), monitor=key, options=options
    )


# ==========================================================================
# Bluefit
# ==========================================================================
class TestBluefit:
    def test_period_from_quarter(self):
        assert bluefit_period(2, 2026) == "2T2026"
        assert bluefit_period(4, 2025) == "Anual 2025"
        assert bluefit_period(None, None, "Demonstrações Financeiras Intermediárias- 1ITR26") == "1T2026"

    def test_every_dfs_document_is_an_event(self, monkeypatch):
        payloads = {
            "2026": json.loads(load("bluefit_dfs_2026.json")),
            "2025": json.loads(load("bluefit_dfs_2025.json")),
        }

        class FakeResponse:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self.data

        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, kwargs["json"]))
            return FakeResponse(payloads[kwargs["json"]["year"]])

        from ir_monitor.monitors import bluefit as bluefit_module

        monkeypatch.setattr(bluefit_module.http, "request", fake_request)
        monkeypatch.setattr(BluefitMonitor, "_years", lambda self: [2026, 2025])

        monitor = BluefitMonitor(config_module.load_companies()["bluefit"])
        _, events = monitor.run()

        assert [c[0] for c in calls] == ["POST", "POST"]
        assert calls[0][1]["categories"] == ["dfs"]
        periods = {e.reporting_period: e for e in events}
        assert set(periods) == {"2T2026", "1T2026", "Anual 2025", "3T2025"}
        assert periods["Anual 2025"].event_type == EventType.ANNUAL_FINANCIAL_STATEMENTS
        assert periods["2T2026"].event_type == EventType.QUARTERLY_RESULTS
        assert periods["2T2026"].publication_date == date(2026, 8, 13)
        assert "origin=2" in periods["2T2026"].document_url
        assert periods["2T2026"].event_key == "bluefit|2T2026"

    def test_bad_shape_is_a_failure_not_silence(self):
        with pytest.raises(ParserFailure):
            parse_bluefit_payload({"success": True, "data": []})

    def test_zero_documents_is_a_failure(self, monkeypatch):
        class Empty:
            def raise_for_status(self):
                pass

            def json(self):
                return {"success": True, "data": {"document_metas": []}}

        from ir_monitor.monitors import bluefit as bluefit_module

        monkeypatch.setattr(bluefit_module.http, "request", lambda *a, **k: Empty())
        monitor = BluefitMonitor(config_module.load_companies()["bluefit"])
        with pytest.raises(ParserFailure):
            monitor.run()


# ==========================================================================
# Selfit
# ==========================================================================
class TestSelfit:
    URL = "https://www.selfitacademias.com.br/investidores"

    def test_only_annual_statements_section(self):
        candidates = parse_selfit_html(load("selfit_investidores.html"), self.URL)
        years = [c.raw["year"] for c in candidates]
        assert years == list(range(2025, 2015, -1))
        assert all("ata" not in (c.document_url or "").lower() for c in candidates)
        assert all("aviso" not in (c.document_url or "").lower() for c in candidates)
        assert candidates[0].document_url == (
            "https://www.selfitacademias.com.br/pdfs/Self-it-Dez25-DF-Relatorio-v1.pdf"
        )

    def test_events_are_keyed_by_year(self, monkeypatch):
        from ir_monitor.monitors import selfit as selfit_module

        monkeypatch.setattr(
            selfit_module.http, "get_text", lambda url: load("selfit_investidores.html")
        )
        _, events = SelfitMonitor(config_module.load_companies()["selfit"]).run()
        keys = {e.event_key for e in events}
        assert len(events) == 10
        assert "selfit|Anual 2025" in keys and "selfit|Anual 2016" in keys

    def test_missing_section_is_a_failure(self):
        with pytest.raises(ParserFailure):
            parse_selfit_html("<html><h2>Outra coisa</h2><a href='/x.pdf'>2025</a></html>", self.URL)

    def test_new_year_appears(self):
        html = load("selfit_investidores.html").replace(
            '<div class="row">\n',
            '<div class="row">\n<div class="col grid_3"><a href="/pdfs/DF-2026.pdf" title="2026">2026</a></div>\n',
            1,
        )
        years = [c.raw["year"] for c in parse_selfit_html(html, self.URL)]
        assert years[0] == 2026 and len(years) == 11


# ==========================================================================
# Bodytech
# ==========================================================================
class TestBodytech:
    PAGE = "https://www.bodytech.com.br/pt/politicas/?topico=2"

    def test_only_the_financial_statements_item(self):
        payload = json.loads(load("bodytech_documents.json"))
        candidates = parse_bodytech_payload(payload, self.PAGE)
        years = sorted(c.raw["year"] for c in candidates)
        assert years == list(range(2019, 2026))
        urls = " ".join(c.document_url for c in candidates)
        assert "sped" not in urls and "edital" not in urls
        assert "medias.bodytech.com.br/balances/ABodytech_Balanco_2025.pdf" in urls

    def test_events(self, monkeypatch):
        from ir_monitor.monitors import bodytech as bodytech_module

        payload = json.loads(load("bodytech_documents.json"))
        monkeypatch.setattr(bodytech_module.http, "get_json", lambda url, **k: payload)
        _, events = BodytechMonitor(config_module.load_companies()["bodytech"]).run()
        assert len(events) == 7
        assert all(e.event_type == EventType.ANNUAL_FINANCIAL_STATEMENTS for e in events)
        assert "bodytech|Anual 2025" in {e.event_key for e in events}

    def test_latest_link_is_included_even_if_missing_from_text(self):
        payload = json.loads(load("bodytech_documents.json"))
        payload["data"][1]["link"] = "/balances/ABodytech_Balanco_2026.pdf"
        years = {c.raw["year"] for c in parse_bodytech_payload(payload, self.PAGE)}
        assert 2026 in years

    def test_item_missing_is_a_failure(self):
        payload = json.loads(load("bodytech_documents.json"))
        payload["data"] = [d for d in payload["data"] if d["id"] != 13]
        with pytest.raises(ParserFailure):
            parse_bodytech_payload(payload, self.PAGE)


# ==========================================================================
# Texto do alerta
# ==========================================================================
def test_issue_is_in_portuguese():
    event = NormalizedEvent(
        company="bluefit",
        event_type=EventType.QUARTERLY_RESULTS,
        reporting_period="3T2026",
        title="Demonstrações Financeiras Intermediárias - 30/09/2026",
        source="mz_catalog",
        publication_date=date(2026, 11, 13),
        document_url="https://api.mziq.com/x.pdf",
        primary_url="https://ri.bluefit.com.br/",
        key_includes_event_type=False,
    )
    assert build_issue_title(event) == (
        "Bluefit — Demonstrações financeiras trimestrais (ITR) — 3º trimestre de 2026"
    )
    from ir_monitor.util import now_local

    body = render_issue_body(event, now_local())
    assert "13/11/2026" in body and "https://api.mziq.com/x.pdf" in body


# ==========================================================================
# Proteções do runner
# ==========================================================================
@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'test.db'}")
    monkeypatch.setenv("BLIND_AFTER_FAILURES", "3")
    config_module.reset_caches()
    db_module.reset_engine()
    db_module.init_db()
    yield
    db_module.reset_engine()
    config_module.reset_caches()


def make_event(period: str, key: str = "acme") -> NormalizedEvent:
    return NormalizedEvent(
        company=key,
        event_type=EventType.ANNUAL_FINANCIAL_STATEMENTS,
        reporting_period=period,
        title=f"DF {period}",
        source="s",
        document_url=f"https://acme.test/{period.replace(' ', '')}.pdf",
        key_includes_event_type=False,
    )


class Scripted(CompanyMonitor):
    min_expected_candidates = 0
    events: list[NormalizedEvent] = []
    fail: bool = False

    def fetch_candidates(self):
        if Scripted.fail:
            raise ParserFailure("site mudou")
        from ir_monitor.models import CandidateEvent

        return [CandidateEvent(company=self.key, source="s", title=e.title) for e in Scripted.events]

    def classify(self, candidate):
        return EventType.ANNUAL_FINANCIAL_STATEMENTS

    def normalize(self, candidate, event_type):
        return next(e for e in Scripted.events if e.title == candidate.title)


class FlakySender(ConsoleEmailSender):
    """Falha no envio enquanto `broken` for True."""

    broken = True

    def send_event_alert(self, event, detected_at):
        if self.broken:
            raise EmailSendError("GitHub fora do ar")
        super().send_event_alert(event, detected_at)


@pytest.fixture()
def runner(db, monkeypatch):
    from ir_monitor.services import runner as runner_module

    REGISTRY["scripted"] = Scripted
    Scripted.events = []
    Scripted.fail = False
    acme = CompanyConfig(key="acme", name="Acme", monitor="scripted", primary_url="https://acme.test")
    monkeypatch.setattr(runner_module, "select_companies", lambda only=None: [acme])
    return runner_module


class TestRunnerProtections:
    def _bootstrap(self, runner):
        Scripted.events = [make_event("Anual 2024")]
        runner.run(mode="bootstrap", sender=ConsoleEmailSender())

    def test_failed_alert_is_retried_next_run(self, runner):
        self._bootstrap(runner)
        Scripted.events = [make_event("Anual 2024"), make_event("Anual 2025")]
        sender = FlakySender()
        summary = runner.run(mode="check", sender=sender)
        assert summary.alerts_sent == 0  # falhou

        sender.broken = False
        summary = runner.run(mode="check", sender=sender)
        assert summary.alerts_sent == 1  # reenviado
        assert "Anual 2025" in sender.sent[0][0]

        summary = runner.run(mode="check", sender=sender)
        assert summary.alerts_sent == 0  # e só uma vez

    def test_dry_run_writes_nothing(self, runner):
        self._bootstrap(runner)
        Scripted.events = [make_event("Anual 2024"), make_event("Anual 2025")]
        runner.run(mode="check", sender=ConsoleEmailSender(), dry_run=True)
        with db_module.session_scope() as session:
            periods = [e.reporting_period for e in session.execute(select(Event)).scalars()]
        assert periods == ["Anual 2024"]

        sender = ConsoleEmailSender()
        summary = runner.run(mode="check", sender=sender)
        assert summary.alerts_sent == 1  # o dry-run não "consumiu" o alerta

    def test_blind_notice_opens_after_three_failures_and_closes_on_recovery(self, runner):
        self._bootstrap(runner)
        sender = ConsoleEmailSender()
        Scripted.fail = True
        for _ in range(2):
            runner.run(mode="check", sender=sender)
        assert sender.notices == []
        runner.run(mode="check", sender=sender)
        assert len(sender.notices) == 1 and sender.notices[0][0] == "open"
        assert "Acme sem monitoramento" in sender.notices[0][1]
        runner.run(mode="check", sender=sender)
        assert len(sender.notices) == 1  # não repete o aviso

        Scripted.fail = False
        Scripted.events = [make_event("Anual 2024")]
        runner.run(mode="check", sender=sender)
        assert sender.notices[-1][0] == "close"

    def test_first_reading_records_baseline_and_asks_for_manual_check(self, runner):
        self._bootstrap(runner)
        from ir_monitor.services import runner as runner_module

        newco = CompanyConfig(key="newco", name="NewCo", monitor="scripted", primary_url="https://n")
        runner_module.select_companies = lambda only=None: [newco]
        Scripted.events = [make_event("Anual 2023", "newco"), make_event("Anual 2025", "newco")]
        sender = ConsoleEmailSender()
        summary = runner_module.run(mode="check", sender=sender)
        assert summary.alerts_sent == 0
        assert len(sender.notices) == 1
        assert "Linha de base registrada — NewCo" == sender.notices[0][1]
        assert "ano de 2025" in sender.notices[0][2]

    def test_v2_reset_deletes_old_rows_of_the_three_companies_once(self, runner):
        self._bootstrap(runner)
        from ir_monitor.services import runner as runner_module
        from ir_monitor.util import now_local

        with db_module.session_scope() as session:
            session.delete(session.get(Meta, runner_module.V2_RESET_FLAG))
            now = now_local()
            session.add(
                Event(
                    company="selfit", event_type="x", reporting_period="FY-2025",
                    event_key="selfit|FY-2025", is_baseline=True, first_seen=now,
                    created_at=now, updated_at=now,
                )
            )
        runner.run(mode="check", sender=ConsoleEmailSender())
        with db_module.session_scope() as session:
            companies = {e.company for e in session.execute(select(Event)).scalars()}
            assert "selfit" not in companies and "acme" in companies
            assert session.get(Meta, runner_module.V2_RESET_FLAG) is not None
