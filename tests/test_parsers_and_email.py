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
from ir_monitor.monitors.basic_fit import BasicFitMonitor
from ir_monitor.monitors.benefit_systems import BenefitSystemsMonitor
from ir_monitor.monitors.bluefit import BluefitMonitor
from ir_monitor.monitors.bodytech import BodytechMonitor
from ir_monitor.monitors.planet_fitness import PlanetFitnessMonitor
from ir_monitor.monitors.puregym import PureGymMonitor
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

        # Real link text on the live site carries prefixes/suffixes ("PDF -
        # Full Year Results 2024", "Download: Interim Results 2025 (PDF,
        # 1.2MB)") that a title starting with anything other than the exact
        # phrase must still classify - this is what produced 82 candidates /
        # 0 relevant events against the real page before the anchors were
        # loosened from "^phrase" to "\bphrase".
        assert "FY-2024" in periods
        assert "H1-2025" in periods

        # And when the anchor's own text is fully generic ("Download") and
        # the phrase only exists in a nearby heading, classify() must also
        # pick it up from `context` - the same signal normalize()/tgg_period
        # already read for the publication date. This is what still produced
        # 82 candidates / 0 relevant after the anchoring fix alone.
        assert "FY-2023" in periods

    def test_classify_reads_context_not_just_the_bare_link_text(self):
        monitor = TheGymGroupMonitor(cfg("the_gym_group"))
        candidates = monitor.parse_results_page(
            fixture("tgg_results.html"), "https://www.tggplc.com/"
        )
        download_only = [c for c in candidates if c.title.strip().lower() == "download"]
        assert download_only, "fixture must contain a generic 'Download' link"
        cand = download_only[0]
        assert monitor.classify(cand) == EventType.FULL_YEAR_RESULTS


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

    def test_http_error_is_a_clear_parser_failure_not_a_raw_traceback(self, monkeypatch):
        # Reproduces the production incident: the reports page answered 403
        # and the unhandled requests.HTTPError crashed the company with a raw
        # traceback instead of the usual, clearly-labelled ParserFailure every
        # other adapter produces on a fetch problem.
        import requests

        from ir_monitor.monitors import benefit_systems as module

        def _raise(url, **kwargs):
            raise requests.exceptions.HTTPError(
                "403 Client Error: Forbidden for url: " + url
            )

        monkeypatch.setattr(module.http, "get_text", _raise)
        monitor = BenefitSystemsMonitor(cfg("benefit_systems"))
        with pytest.raises(module.ParserFailure, match="benefit_systems"):
            monitor.fetch_candidates()


# ==========================================================================
class TestPureGymDiscovery:
    def test_moved_results_page_is_recovered_from_the_investors_root(self, monkeypatch):
        # Reproduces the production incident: both hardcoded deep links now
        # 404. Instead of a third hardcoded guess, the adapter should recover
        # by following the link to the results page from the stable investors
        # root, then parse that page normally.
        import requests

        from ir_monitor.monitors import puregym as module

        root_html = """
        <html><body>
        <nav>
          <a href="/investors/annual-report/default.aspx">Annual Report 2025</a>
          <a href="/investors/results-and-reports/default.aspx">Results, Reports and Presentations</a>
        </nav>
        </body></html>
        """
        discovered_url = "https://corporate.puregym.com/investors/results-and-reports/default.aspx"
        results_html = """
        <html><body>
        <div>Q1 2026
          <a href="/doc_financials/2026/q1/PureGym-Q126-Report.pdf">Report</a>
          <a href="/doc_financials/2026/q1/PureGym-Q126-Presentation.pdf">Presentation</a>
        </div>
        </body></html>
        """

        def _get_text(url, **kwargs):
            if url == module.DISCOVERY_ROOT_URL:
                return root_html
            if url == discovered_url:
                return results_html
            raise requests.exceptions.HTTPError(f"404 Client Error: Not Found for url: {url}")

        monkeypatch.setattr(module.http, "get_text", _get_text)
        monitor = PureGymMonitor(cfg("puregym"))
        candidates = monitor.fetch_candidates()

        assert monitor.source_used == module.SOURCE_RESULTS_PAGE
        assert len(candidates) == 1
        assert candidates[0].raw.get("period") == "Q1-2026"
        assert candidates[0].document_url.endswith("PureGym-Q126-Report.pdf")

    def test_domain_wide_404_produces_a_distinct_diagnostic(self, monkeypatch):
        # Reproduces the actual production incident: primary_url, the
        # LEGACY_URL, the discovery root, and overview_url all 404 (even
        # though a search engine still indexes primary_url as live), and
        # Playwright (enabled) reaches the page fine but finds no matching
        # report links either. The resulting ParserFailure should say this
        # looks like a request-level block, not suggest yet another URL.
        import requests

        from ir_monitor.monitors import puregym as module

        def _get_text(url, **kwargs):
            raise requests.exceptions.HTTPError(f"404 Client Error: Not Found for url: {url}")

        monkeypatch.setattr(module.http, "get_text", _get_text)
        monkeypatch.setattr(
            PureGymMonitor, "render_html", lambda self, url, **kw: "<html></html>"
        )
        monitor = PureGymMonitor(cfg("puregym"))
        with pytest.raises(module.ParserFailure, match="request") as excinfo:
            monitor.fetch_candidates()
        assert "404" in str(excinfo.value)


# ==========================================================================
class TestBluefitPage:
    def test_cert_failure_on_playwright_is_a_distinct_clear_error(self, monkeypatch):
        # Reproduces the production incident: the site's own TLS certificate
        # is expired, so both the plain-HTTP attempt AND Playwright fail on
        # it. This must surface as one specific, clearly-labelled failure
        # (an external site problem, not a parser bug) rather than the
        # generic "Central de Resultados returned no documents" message.
        import requests

        from ir_monitor.monitors import bluefit as module

        def _get_text(url, **kwargs):
            raise requests.exceptions.SSLError("certificate has expired")

        def _render_html(self, url, **kwargs):
            raise Exception(  # noqa: BLE001 - mirrors Playwright's own exception type
                "Page.goto: net::ERR_CERT_DATE_INVALID at " + url
            )

        monkeypatch.setattr(module.http, "get_text", _get_text)
        monkeypatch.setattr(BluefitMonitor, "render_html", _render_html)
        monitor = BluefitMonitor(cfg("bluefit"))
        with pytest.raises(module.ParserFailure, match="TLS certificate"):
            monitor.fetch_candidates()


# ==========================================================================
class TestBasicFitPage:
    def test_classify_reads_context_not_just_the_bare_link_text(self):
        # Reproduces the production incident: 10 candidates, 0 relevant.
        # parse_results_html()'s own fetch-time filter already requires the
        # qualifying phrase in title+block (see TRADING_UPDATE_RE etc. down
        # there), and normalize()/basic_fit_period() already read `context`
        # for the period - classify() was the one place still checking only
        # title+link_text, so a link whose own visible text is a generic
        # "Download" while the heading ("Q1 2026 Trading Update") sits in
        # the surrounding block was extracted as a candidate but always
        # classified as irrelevant.
        html = """
        <html><body>
        <div class="row">
          <h3>Q1 2026 Trading Update - Basic-Fit reports first quarter trading
          update for the period ended 31 March 2026, Hoofddorp</h3>
          <a href="/docs/basic-fit-q1-2026-trading-update.pdf">Download</a>
        </div>
        <div class="row">
          <h3>Full Year Results 2025 - Basic-Fit reports full year results
          for the twelve months ended 31 December 2025, Hoofddorp</h3>
          <a href="/docs/basic-fit-fy-2025-results.pdf">Download</a>
        </div>
        </body></html>
        """
        monitor = BasicFitMonitor(cfg("basic_fit"))
        candidates = monitor.parse_results_html(
            html, "https://corporate.basic-fit.com/", "basic_fit_results_rendered"
        )
        assert len(candidates) == 2
        # "Download" is only 8 chars, under parse_results_html()'s own
        # len(text) > 8 threshold for trusting the bare link text as the
        # title, so it falls back to the block - link_text (used by
        # classify()) is still the raw anchor text "Download", though.
        assert all(c.raw.get("link_text") == "Download" for c in candidates)

        events = []
        for cand in candidates:
            event_type = monitor.classify(cand)
            assert event_type is not None, cand.raw.get("context")
            event = monitor.normalize(cand, event_type)
            assert event is not None
            events.append(event)

        periods = {e.reporting_period for e in events}
        types = {e.event_type for e in events}
        assert periods == {"Q1-2026", "FY-2025"}
        assert types == {EventType.TRADING_UPDATE, EventType.FULL_YEAR_RESULTS}

    def test_table_row_context_is_not_contaminated_by_other_rows(self):
        # Reproduces the actual real-site structure (pulled from the live
        # inspect-validate DEBUG dump): a table where every row's link says
        # the same generic "View report (pdf)" and rows sit close enough
        # together that the old _block_text (climb until > 80 chars) merged
        # several rows into one block - including a "Capital Markets Day"
        # row, and the literal words "Presentation"/"Webcast" from other
        # rows' columns - which made IGNORE_RE reject every link in the
        # table, real event type included.
        html = """
        <html><body>
        <table>
          <tr><th>Date</th><th>Description</th><th>Report</th>
              <th>Presentation</th><th>Webcast</th></tr>
          <tr>
            <td>28 Jul 2026</td><td>Half Year 2026</td>
            <td><a href="/docs/h1-2026-report.pdf">View report (pdf)</a></td>
            <td><a href="/docs/h1-2026-presentation.pdf">View report (pdf)</a></td>
            <td><a href="/webcast/h1-2026">Listen</a></td>
          </tr>
          <tr>
            <td>21 Apr 2026</td><td>Capital Markets day 2026</td>
            <td><a href="/docs/cmd-2026.pdf">View report (pdf)</a></td>
            <td><a href="/docs/cmd-2026-presentation.pdf">View report (pdf)</a></td>
            <td><a href="/webcast/cmd-2026">Listen</a></td>
          </tr>
          <tr>
            <td>16 Apr 2026</td><td>Q1 2026 Trading Update</td>
            <td><a href="/docs/q1-2026-tu.pdf">View report (pdf)</a></td>
            <td><a href="/docs/q1-2026-tu-presentation.pdf">View report (pdf)</a></td>
            <td><a href="/webcast/q1-2026-tu">Listen</a></td>
          </tr>
        </table>
        </body></html>
        """
        monitor = BasicFitMonitor(cfg("basic_fit"))
        candidates = monitor.parse_results_html(
            html, "https://corporate.basic-fit.com/", "basic_fit_results_rendered"
        )
        events = []
        for cand in candidates:
            event_type = monitor.classify(cand)
            if event_type:
                event = monitor.normalize(cand, event_type)
                if event:
                    events.append(event)

        periods = {e.reporting_period for e in events}
        # The real bug: this used to be an empty set (everything excluded).
        assert "H1-2026" in periods
        assert "Q1-2026" in periods
        # Capital Markets Day stays excluded, same as before.
        assert not any("cmd" in (e.primary_url or "") for e in events)


# ==========================================================================
class TestBodytechPage:
    def test_non_pdf_document_link_in_financial_section_is_recovered(self):
        # Reproduces the production incident: the static fetch found the
        # page (its text is even search-engine indexed, so it is not purely
        # client-rendered) but produced 0 items because the real document
        # link is a "/download/..." viewer URL, not a bare ".pdf" href, so
        # the old strict `.endswith(".pdf")` filter dropped it - and the
        # Playwright fallback then also failed, waiting 30s for a
        # `a[href*='.pdf']` selector that the same real markup never
        # satisfies either.
        html = """
        <html><body>
        <section>
          <h2>Demonstrações Financeiras</h2>
          <ul>
            <li>Demonstrações Financeiras - Exercício de 2025
                <a href="/download/documento?id=4821">Baixar</a></li>
          </ul>
        </section>
        <section>
          <h2>Outras publicações legais</h2>
          <ul>
            <li>Ata de Assembleia <a href="/download/documento?id=9911">Baixar</a></li>
          </ul>
        </section>
        </body></html>
        """
        monitor = BodytechMonitor(cfg("bodytech"))
        candidates = monitor.parse_site_html(
            html, "https://www.bodytech.com.br/", "bodytech_politicas_html"
        )
        assert len(candidates) == 1
        assert candidates[0].document_url.endswith("id=4821")

        event_type = monitor.classify(candidates[0])
        assert event_type == EventType.ANNUAL_FINANCIAL_STATEMENTS
        event = monitor.normalize(candidates[0], event_type)
        assert event.reporting_period == "FY-2025"


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
