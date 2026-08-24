"""Persistence behaviour: bootstrap, deduplication, republication, new events.

No network. Adapters are replaced by a fake monitor that returns a scripted
list of events.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from ir_monitor import config as config_module
from ir_monitor import database as db_module
from ir_monitor.database import Event, SourceObservation
from ir_monitor.emailer import ConsoleEmailSender
from ir_monitor.models import CandidateEvent, EventType, NormalizedEvent
from ir_monitor.monitors import base as base_module


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'test.db'}")
    monkeypatch.setenv("ALERT_ON_REVISION", "false")
    config_module.reset_caches()
    db_module.reset_engine()
    db_module.init_db()
    yield
    db_module.reset_engine()
    config_module.reset_caches()


def make_event(period="Q1-2026", **kwargs) -> NormalizedEvent:
    base = dict(
        company="acme",
        event_type=EventType.QUARTERLY_RESULTS,
        reporting_period=period,
        title=f"Acme {period} Results",
        source="source_a",
        publication_date=date(2026, 4, 20),
        primary_url="https://acme.test/results/q1",
        document_url="https://acme.test/results/q1.pdf",
        key_includes_event_type=False,
    )
    base.update(kwargs)
    return NormalizedEvent(**base)


class FakeMonitor(base_module.CompanyMonitor):
    """Returns a scripted set of normalized events."""

    scripted: list[NormalizedEvent] = []
    min_expected_candidates = 0

    def fetch_candidates(self):
        return [
            CandidateEvent(company=self.key, source=e.source, title=e.title)
            for e in self.scripted
        ]

    def classify(self, candidate):
        return EventType.QUARTERLY_RESULTS

    def normalize(self, candidate, event_type):
        for event in self.scripted:
            if event.title == candidate.title:
                return event
        return None


@pytest.fixture()
def runner(db, monkeypatch):
    from ir_monitor.config import CompanyConfig
    from ir_monitor.monitors import REGISTRY
    from ir_monitor.services import runner as runner_module

    REGISTRY["fake"] = FakeMonitor
    company = CompanyConfig(key="acme", name="Acme", monitor="fake")
    monkeypatch.setattr(
        runner_module, "select_companies", lambda only=None: [company]
    )
    return runner_module


# ==========================================================================
class TestBootstrap:
    def test_bootstrap_sends_no_email_and_records_baseline(self, runner):
        FakeMonitor.scripted = [make_event("Q1-2026"), make_event("Q2-2026")]
        sender = ConsoleEmailSender()

        summary = runner.run(mode="bootstrap", sender=sender)

        assert summary.new_events == 2
        assert summary.alerts_sent == 0
        assert sender.sent == []
        with db_module.session_scope() as session:
            rows = session.execute(select(Event)).scalars().all()
            assert len(rows) == 2
            assert all(row.is_baseline for row in rows)
            assert all(row.alert_sent_at is None for row in rows)

    def test_check_without_bootstrap_refuses_to_run(self, runner):
        FakeMonitor.scripted = [make_event("Q1-2026")]
        with pytest.raises(runner.BootstrapRequired):
            runner.run(mode="check", sender=ConsoleEmailSender())

    def test_check_after_bootstrap_alerts_only_on_new(self, runner):
        FakeMonitor.scripted = [make_event("Q1-2026")]
        runner.run(mode="bootstrap", sender=ConsoleEmailSender())

        sender = ConsoleEmailSender()
        summary = runner.run(mode="check", sender=sender)
        assert summary.new_events == 0
        assert summary.alerts_sent == 0

        FakeMonitor.scripted = [make_event("Q1-2026"), make_event("Q2-2026")]
        sender = ConsoleEmailSender()
        summary = runner.run(mode="check", sender=sender)
        assert summary.new_events == 1
        assert summary.alerts_sent == 1
        assert "Q2-2026" in sender.sent[0][0]


# ==========================================================================
class TestDeduplication:
    def test_second_source_enriches_without_second_email(self, runner):
        FakeMonitor.scripted = [make_event("Q1-2026")]
        runner.run(mode="bootstrap", sender=ConsoleEmailSender())

        FakeMonitor.scripted = [make_event("Q3-2026", source="source_a")]
        sender = ConsoleEmailSender()
        summary = runner.run(mode="check", sender=sender)
        assert summary.alerts_sent == 1

        # Same economic disclosure, second source, different URL.
        FakeMonitor.scripted = [
            make_event(
                "Q3-2026",
                source="source_b",
                title="Acme Q3-2026 Results (Tadawul)",
                primary_url="https://exchange.test/announcement/999",
                document_url="https://exchange.test/announcement/999.pdf",
            )
        ]
        sender = ConsoleEmailSender()
        summary = runner.run(mode="check", sender=sender)

        assert summary.new_events == 0
        assert summary.alerts_sent == 0
        assert sender.sent == []

        with db_module.session_scope() as session:
            events = session.execute(
                select(Event).where(Event.reporting_period == "Q3-2026")
            ).scalars().all()
            assert len(events) == 1
            observations = session.execute(
                select(SourceObservation).where(
                    SourceObservation.event_id == events[0].id
                )
            ).scalars().all()
            assert {o.source_name for o in observations} == {"source_a", "source_b"}

    def test_republication_does_not_alert_by_default(self, runner):
        FakeMonitor.scripted = [make_event("Q1-2026")]
        runner.run(mode="bootstrap", sender=ConsoleEmailSender())
        FakeMonitor.scripted = [make_event("Q4/FY-2025")]
        runner.run(mode="check", sender=ConsoleEmailSender())

        FakeMonitor.scripted = [
            make_event(
                "Q4/FY-2025",
                document_url="https://acme.test/results/q4-v2.pdf",
            )
        ]
        sender = ConsoleEmailSender()
        summary = runner.run(mode="check", sender=sender)

        assert summary.alerts_sent == 0
        assert sender.sent == []
        with db_module.session_scope() as session:
            event = session.execute(
                select(Event).where(Event.reporting_period == "Q4/FY-2025")
            ).scalar_one()
            assert "q4-v2.pdf" in str(event.metadata_dict.get("alternate_documents"))

    def test_pdf_and_html_of_same_period_are_one_event(self, runner):
        FakeMonitor.scripted = [make_event("Q1-2026")]
        runner.run(mode="bootstrap", sender=ConsoleEmailSender())
        FakeMonitor.scripted = [
            make_event("Q1-2027", document_url="https://acme.test/q1.html"),
            make_event(
                "Q1-2027",
                title="Acme Q1-2027 Results PDF",
                document_url="https://acme.test/q1.pdf",
            ),
        ]
        sender = ConsoleEmailSender()
        summary = runner.run(mode="check", sender=sender)
        assert summary.alerts_sent == 1
        assert len(sender.sent) == 1

    def test_new_company_without_history_is_auto_baselined(self, runner):
        """A company added after bootstrap must not email its whole history."""
        FakeMonitor.scripted = [make_event("Q1-2026")]
        runner.run(mode="bootstrap", sender=ConsoleEmailSender())

        FakeMonitor.scripted = [
            make_event("Q1-2020", company="newco"),
            make_event("Q2-2020", company="newco"),
        ]

        from ir_monitor.config import CompanyConfig
        from ir_monitor.services import runner as runner_module

        original = runner_module.select_companies
        runner_module.select_companies = lambda only=None: [
            CompanyConfig(key="newco", name="NewCo", monitor="fake")
        ]
        try:
            sender = ConsoleEmailSender()
            summary = runner_module.run(mode="check", sender=sender)
        finally:
            runner_module.select_companies = original

        assert summary.new_events == 2
        assert summary.alerts_sent == 0
        assert summary.results[0].auto_baselined is True

    def test_alert_on_revision_can_be_enabled(self, runner, monkeypatch):
        FakeMonitor.scripted = [make_event("Q1-2026")]
        runner.run(mode="bootstrap", sender=ConsoleEmailSender())
        FakeMonitor.scripted = [make_event("Q2-2027")]
        runner.run(mode="check", sender=ConsoleEmailSender())

        monkeypatch.setenv("ALERT_ON_REVISION", "true")
        config_module.reset_caches()
        FakeMonitor.scripted = [
            make_event("Q2-2027", document_url="https://acme.test/q2-revised.pdf")
        ]
        sender = ConsoleEmailSender()
        summary = runner.run(mode="check", sender=sender)
        assert summary.alerts_sent == 1


# ==========================================================================
class TestFailSafe:
    def test_zero_candidates_is_a_parser_failure_not_silence(self):
        from ir_monitor.config import CompanyConfig

        class EmptyMonitor(FakeMonitor):
            min_expected_candidates = 1

            def fetch_candidates(self):
                return []

        monitor = EmptyMonitor(CompanyConfig(key="acme", name="Acme", monitor="fake"))
        with pytest.raises(base_module.ParserFailure):
            monitor.run()

    def test_one_company_failure_does_not_stop_the_others(self, db, monkeypatch):
        from ir_monitor.config import CompanyConfig
        from ir_monitor.monitors import REGISTRY
        from ir_monitor.services import runner as runner_module

        class BrokenMonitor(FakeMonitor):
            def fetch_candidates(self):
                raise RuntimeError("source exploded")

        class WorkingMonitor(FakeMonitor):
            scripted = [make_event("Q1-2026", company="working")]

            def normalize(self, candidate, event_type):
                return make_event("Q1-2026", company="working")

        REGISTRY["broken"] = BrokenMonitor
        REGISTRY["working"] = WorkingMonitor
        companies = [
            CompanyConfig(key="broken", name="Broken", monitor="broken"),
            CompanyConfig(key="working", name="Working", monitor="working"),
        ]
        monkeypatch.setattr(
            runner_module, "select_companies", lambda only=None: companies
        )

        summary = runner_module.run(mode="bootstrap", sender=ConsoleEmailSender())
        assert len(summary.errors) == 1
        assert summary.errors[0].company == "broken"
        assert summary.companies_checked == 2
        assert summary.new_events == 1


# ==========================================================================
class TestMergeEvents:
    def test_merge_fills_missing_links(self):
        a = make_event("Q1-2026", presentation_url=None)
        b = make_event(
            "Q1-2026",
            title="Presentation",
            presentation_url="https://acme.test/pres.pdf",
        )
        merged = base_module.merge_events([a, b])
        assert len(merged) == 1
        assert merged[0].presentation_url == "https://acme.test/pres.pdf"

    def test_distinct_periods_are_not_merged(self):
        merged = base_module.merge_events([make_event("Q1-2026"), make_event("Q2-2026")])
        assert len(merged) == 2
