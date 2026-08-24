"""HTML/RSS parsing against local fixtures, plus email rendering.

Nothing here touches the network.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from ir_monitor.config import CompanyConfig
from ir_monitor.emailer import build_subject, render_html, render_plain_text
from ir_monitor.models import EventType, NormalizedEvent
from ir_monitor.monitors.benefit_systems import BenefitSystemsMonitor
from ir_monitor.monitors.planet_fitness import PlanetFitnessMonitor
from ir_monitor.monitors.sats import SATSMonitor
from ir_monitor.monitors.sports_world import SportsWorldMonitor
from ir_monitor.monitors.the_gym_group import TheGymGroupMonitor
from ir_monitor.monitors.xponential import XponentialMonitor
from ir_monitor.util import now_local

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def cfg(key: str, **options) -> CompanyConfig:
    return CompanyConfig(key=key, name=key, monitor=key, options=options)


# ==========================================================================
class TestPlanetFitnessFeed:
    def test_only_the_real_release_is_relevant(self):
        monitor = PlanetFitnessMonitor(cfg("planet_fitness"))
        entries = monitor.parse_feed(fixture("planet_fitness_rss.xml"))
        assert len(entries) == 4

        relevant = []
        for entry in entries:
            from ir_monitor.monitors.base import candidate

            cand = candidate(
                "planet_fitness",
                "test",
                entry["title"],
                url=entry["link"],
                publication_date=entry["published"],
                guid=entry["guid"],
            )
            event_type = monitor.classify(cand)
            if event_type:
                relevant.append(monitor.normalize(cand, event_type))

        assert len(relevant) == 1
        assert relevant[0].reporting_period == "Q2-2026"
        assert relevant[0].event_type == EventType.EARNINGS_RELEASE
        assert relevant[0].guid


# ==========================================================================
class TestTheGymGroupPage:
    def test_notice_is_excluded_and_real_events_extracted(self):
        monitor = TheGymGroupMonitor(cfg("the_gym_group"))
        candidates = monitor.parse_results_page(
            fixture("tgg_results.html"), "https://www.tggplc.com/"
        )
        assert len(candidates) >= 4

        events = []
        for cand in candidates:
            event_type = monitor.classify(cand)
            if event_type:
                event = monitor.normalize(cand, event_type)
                if event:
                    events.append(event)

        periods = {e.reporting_period for e in events}
        assert "FY_PRE_CLOSE-2025" in periods
        assert "FY-2025" in periods
        assert "H1-2026" in periods
        # The "Notice of Pre-Close Trading Update" entry produced no event.
        titles = {e.title.lower() for e in events}
        assert not any(t.startswith("notice of") for t in titles)


# ==========================================================================
class TestSATSPage:
    def test_report_links_grouped_by_period(self):
        monitor = SATSMonitor(cfg("sats"))
        candidates = monitor.parse_reports_page(
            fixture("sats_reports.html"), "https://satsgroup.com/"
        )
        titles = {c.title for c in candidates}
        assert "Q1 Report 2026" in titles
        assert "Q4 Report 2025" in titles

        events = []
        for cand in candidates:
            event_type = monitor.classify(cand)
            if event_type:
                event = monitor.normalize(cand, event_type)
                if event:
                    events.append(event)
        by_period = {e.reporting_period: e for e in events}
        assert "Q1-2026" in by_period
        assert "Q4/FY-2025" in by_period
        assert by_period["Q1-2026"].presentation_url is not None
        assert by_period["Q1-2026"].document_url.endswith("q1-2026-report.pdf")
        # Annual Report 2025 must not appear as a separate event.
        assert len(events) == 2


# ==========================================================================
class TestBenefitSystemsPage:
    def test_consolidated_only_and_active_cards(self):
        monitor = BenefitSystemsMonitor(cfg("benefit_systems"))
        candidates = monitor.parse_reports_page(
            fixture("benefit_systems_reports.html"), "https://corp.benefitsystems.pl/"
        )
        events = []
        for cand in candidates:
            event_type = monitor.classify(cand)
            if event_type:
                event = monitor.normalize(cand, event_type)
                if event:
                    events.append(event)

        types = [e.event_type for e in events]
        assert EventType.ACTIVE_SPORT_CARDS_UPDATE in types
        assert EventType.FULL_YEAR_RESULTS in types
        # Standalone annual report must not create a second event.
        assert types.count(EventType.FULL_YEAR_RESULTS) == 1
        # Unrelated current reports were filtered out by the allowlist.
        assert all("management board" not in e.title.lower() for e in events)


# ==========================================================================
class TestXponentialPage:
    def test_only_earnings_release_links(self):
        monitor = XponentialMonitor(cfg("xponential"))
        candidates = monitor.parse_quarterly_page(
            fixture("xponential_quarterly.html"), "https://investor.xponential.com/"
        )
        assert len(candidates) == 2
        events = [
            monitor.normalize(c, monitor.classify(c))
            for c in candidates
            if monitor.classify(c)
        ]
        periods = {e.reporting_period for e in events if e}
        assert periods == {"Q1-2026", "Q4/FY-2025"}
        assert any(e.release_id for e in events if e)


# ==========================================================================
class TestSportsWorldPage:
    def test_spanish_quarterly_pdfs_only(self):
        monitor = SportsWorldMonitor(cfg("sports_world"))
        candidates = monitor.parse_reports_page(
            fixture("sports_world_reportes.html"), "https://www.sportsworld.com.mx/"
        )
        urls = [c.url for c in candidates]
        assert any("gsw_reporte_1T26" in u for u in urls)
        assert not any("/uploads/en/" in u for u in urls)
        assert not any("anual" in u.lower() for u in urls)

        events = [
            monitor.normalize(c, monitor.classify(c))
            for c in candidates
            if monitor.classify(c)
        ]
        periods = {e.reporting_period for e in events if e}
        assert "Q1-2026" in periods


# ==========================================================================
class TestEmailRendering:
    def _event(self) -> NormalizedEvent:
        return NormalizedEvent(
            company="basic_fit",
            event_type=EventType.TRADING_UPDATE,
            reporting_period="Q1-2026",
            title="Basic-Fit Q1 2026 Trading Update",
            source="basic_fit_results_endpoint",
            publication_date=date(2026, 4, 22),
            primary_url="https://corporate.basic-fit.com/q1-2026",
            document_url="https://corporate.basic-fit.com/q1-2026.pdf",
            presentation_url="https://corporate.basic-fit.com/q1-2026-pres.pdf",
        )

    def test_subject_format(self):
        subject = build_subject(self._event())
        assert subject == "[IR Watch] Basic Fit — Trading Update — Q1-2026"

    def test_plain_text_contains_required_fields(self):
        text = render_plain_text(self._event(), now_local())
        for needle in (
            "Company:",
            "Event type:",
            "Reporting period:",
            "Publication date:",
            "Detected at:",
            "Title:",
            "Source:",
            "Primary link:",
            "Document/PDF:",
            "Presentation:",
        ):
            assert needle in text, needle

    def test_html_is_escaped(self):
        event = self._event()
        event.title = 'Result <script>alert("x")</script>'
        html = render_html(event, now_local())
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_planet_fitness_subject_example(self):
        event = NormalizedEvent(
            company="planet_fitness",
            event_type=EventType.EARNINGS_RELEASE,
            reporting_period="Q2-2026",
            title="Planet Fitness, Inc. Announces Second Quarter 2026 Results",
            source="rss",
        )
        assert (
            build_subject(event)
            == "[IR Watch] Planet Fitness — Earnings Release — Q2-2026"
        )
