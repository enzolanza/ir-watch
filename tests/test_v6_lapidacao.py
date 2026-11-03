"""Lapidação: nova tentativa, títulos legíveis, links da SATS e gravação só
quando o estado muda."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import date

import pytest

from ir_monitor import config as config_module
from ir_monitor import database as db_module
from ir_monitor.config import CompanyConfig
from ir_monitor.database import Meta
from ir_monitor.emailer import ConsoleEmailSender, pt_period, render_issue_body
from ir_monitor.models import CandidateEvent, EventType, NormalizedEvent
from ir_monitor.monitors import REGISTRY
from ir_monitor.monitors.base import CompanyMonitor, ParserFailure
from ir_monitor.monitors.sats import SATSMonitor
from ir_monitor.state_diff import state_changed
from ir_monitor.util import now_local


# ==========================================================================
# Títulos legíveis
# ==========================================================================
@pytest.mark.parametrize(
    "code,text",
    [
        ("Q4/FY-2025", "4º trimestre e ano de 2025"),
        ("Q2/H1-2026", "2º trimestre (1º semestre) de 2026"),
        ("Q3/9M-2026", "3º trimestre (9 meses) de 2026"),
        ("Q1-2026", "1º trimestre de 2026"),
        ("TU-Q3-2026", "3º trimestre de 2026"),
        ("CARDS-Q4-2025", "4º trimestre de 2025"),
        ("3T2026", "3º trimestre de 2026"),
        ("Anual 2025", "ano de 2025"),
        ("FY-2024", "ano de 2024"),
        ("H1-2026", "1º semestre de 2026"),
        ("JAN-TU-2026", "janeiro de 2026"),
        ("PRECLOSE-FY-2025", "fechamento do ano de 2025"),
        ("PRECLOSE-H1-2026", "fechamento do 1º semestre de 2026"),
        ("doc-123", "doc-123"),
    ],
)
def test_readable_periods(code, text):
    assert pt_period(code) == text


def test_issue_body_lists_presentation_and_webcast_when_available():
    event = NormalizedEvent(
        company="the_gym_group",
        event_type=EventType.INTERIM_RESULTS,
        reporting_period="H1-2026",
        title="Interim Results",
        source="s",
        publication_date=date(2026, 9, 9),
        document_url="https://x/rns.pdf",
        presentation_url="https://x/presentation.pdf",
        webcast_url="https://x/webcast",
        key_includes_event_type=False,
    )
    body = render_issue_body(event, now_local())
    assert "**Apresentação:** https://x/presentation.pdf" in body
    assert "**Webcast:** https://x/webcast" in body
    assert "1º semestre de 2026" in body
    assert "the_gym_group|H1-2026" in body  # a chave técnica continua lá


# ==========================================================================
# SATS: links sem texto (só ícones)
# ==========================================================================
def test_sats_uses_file_names_and_skips_the_annual_report():
    html = """
    <div><h3>Q4 Report 2025</h3><p>11 Feb 2026</p>
      <a href="https://storage.mfn.se/a/sats-asa-annual-report-2025.pdf"><img/></a>
      <a href="https://storage.mfn.se/b/sats-asa-q4-2025-report.pdf"><img/></a>
      <a href="https://storage.mfn.se/c/sats-asa-q4-2025-presentation.pdf"><img/></a></div>
    <div><h3>Q3 Report 2026</h3>
      <a href="https://storage.mfn.se/d/sats-asa-q3-2026-report.pdf"></a>
      <a href="https://storage.mfn.se/e/sats-asa-q3-2026-presentation.pdf"></a></div>
    """
    monitor = SATSMonitor(CompanyConfig(key="sats", name="SATS", monitor="sats"))
    events = {}
    for cand in monitor.parse_reports_page(html, "https://satsgroup.com/"):
        event = monitor.normalize(cand, monitor.classify(cand))
        events[event.reporting_period] = event
    assert events["Q4/FY-2025"].document_url.endswith("sats-asa-q4-2025-report.pdf")
    assert events["Q4/FY-2025"].presentation_url.endswith("q4-2025-presentation.pdf")
    assert events["Q3-2026"].document_url.endswith("sats-asa-q3-2026-report.pdf")


# ==========================================================================
# Nova tentativa automática
# ==========================================================================
class Flaky(CompanyMonitor):
    min_expected_candidates = 1
    calls = 0
    fail_times = 1

    def fetch_candidates(self):
        Flaky.calls += 1
        if Flaky.calls <= Flaky.fail_times:
            raise ParserFailure("página de verificação passageira")
        return [CandidateEvent(company=self.key, source="s", title="DF 2025")]

    def classify(self, candidate):
        return EventType.ANNUAL_FINANCIAL_STATEMENTS

    def normalize(self, candidate, event_type):
        return NormalizedEvent(
            company=self.key, event_type=event_type, reporting_period="Anual 2025",
            title=candidate.title, source="s", key_includes_event_type=False,
        )


@pytest.fixture()
def flaky_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    config_module.reset_caches()
    db_module.reset_engine()
    db_module.init_db()
    with db_module.session_scope() as session:
        db_module.mark_bootstrapped(session)
    from ir_monitor.services import runner as runner_module

    REGISTRY["flaky"] = Flaky
    Flaky.calls = 0
    acme = CompanyConfig(key="acme", name="Acme", monitor="flaky", primary_url="https://acme.test")
    monkeypatch.setattr(runner_module, "select_companies", lambda only=None: [acme])
    yield runner_module
    db_module.reset_engine()
    config_module.reset_caches()


def test_a_passing_glitch_is_retried_and_not_counted(flaky_runner):
    Flaky.fail_times = 1
    summary = flaky_runner.run(mode="check", sender=ConsoleEmailSender())
    assert summary.results[0].ok and Flaky.calls == 2
    with db_module.session_scope() as session:
        assert session.get(Meta, "fail_streak:acme") is None


def test_a_persistent_failure_still_counts(flaky_runner):
    Flaky.fail_times = 99
    summary = flaky_runner.run(mode="check", sender=ConsoleEmailSender())
    assert not summary.results[0].ok and Flaky.calls == 2
    with db_module.session_scope() as session:
        assert session.get(Meta, "fail_streak:acme").value == "1"


# ==========================================================================
# Gravar só quando algo muda
# ==========================================================================
@pytest.fixture()
def state_files(tmp_path, monkeypatch):
    path = tmp_path / "before.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    config_module.reset_caches()
    db_module.reset_engine()
    db_module.init_db()
    now = now_local()
    with db_module.session_scope() as session:
        session.add(
            db_module.Event(
                company="sats", event_type="x", reporting_period="Q3-2026", event_key="sats|Q3-2026",
                is_baseline=True, first_seen=now, created_at=now, updated_at=now,
            )
        )
    db_module.reset_engine()
    after = tmp_path / "after.db"
    shutil.copy(path, after)
    yield str(path), str(after)
    config_module.reset_caches()


def test_seen_again_is_not_a_change(state_files):
    before, after = state_files
    with sqlite3.connect(after) as conn:
        conn.execute("UPDATE events SET updated_at = '2030-01-01 00:00:00'")
        conn.execute(
            "INSERT INTO monitor_runs (started_at, mode, status, companies_checked, events_found, new_events, alerts_sent)"
            " VALUES ('2030-01-01', 'check', 'ok', 12, 1, 0, 0)"
        )
    assert state_changed(before, after) is False


def test_new_failure_or_alert_is_a_change(state_files):
    before, after = state_files
    with sqlite3.connect(after) as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('fail_streak:sats', '1')")
    assert state_changed(before, after) is True


def test_missing_file_means_save(state_files):
    _, after = state_files
    assert state_changed("/nao/existe.db", after) is True
