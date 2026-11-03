"""Title classification, with explicit coverage of the known false positives."""

from __future__ import annotations

from datetime import date

import pytest

from ir_monitor.models import EventType
from ir_monitor.monitors.benefit_systems import (
    active_cards_period,
    classify_benefit_title,
    financial_report_period,
)
from ir_monitor.monitors.planet_fitness import (
    classify_planet_fitness_title,
    planet_fitness_period,
)
from ir_monitor.monitors.sats import classify_sats_title
from ir_monitor.monitors.sports_world import sports_world_period_tuple
from ir_monitor.monitors.xponential import xponential_period


# ==========================================================================
# Planet Fitness - "To Report" must never be an alert
# ==========================================================================
class TestPlanetFitness:
    def test_date_announcement_is_not_relevant(self):
        title = (
            "Planet Fitness, Inc. To Report Second Quarter 2026 Results on "
            "August 6, 2026"
        )
        assert classify_planet_fitness_title(title) is None

    def test_actual_release_is_relevant(self):
        title = "Planet Fitness, Inc. Announces Second Quarter 2026 Results"
        assert classify_planet_fitness_title(title) == EventType.EARNINGS_RELEASE
        assert planet_fitness_period(title) == "Q2-2026"

    def test_key_year_end_metrics_is_not_relevant(self):
        title = "Planet Fitness Announces Key Year-End Metrics"
        assert classify_planet_fitness_title(title) is None

    def test_fourth_quarter_and_full_year_maps_to_combined_period(self):
        title = (
            "Planet Fitness, Inc. Announces Fourth Quarter and Full Year 2025 Results"
        )
        assert classify_planet_fitness_title(title) == EventType.EARNINGS_RELEASE
        assert planet_fitness_period(title) == "Q4/FY-2025"

    @pytest.mark.parametrize(
        "title",
        [
            "Planet Fitness, Inc. Announces Participation in Investor Conference",
            "Planet Fitness Announces Leadership Transition",
            "Planet Fitness, Inc. to Report First Quarter 2026 Results",
        ],
    )
    def test_non_results_titles_rejected(self, title):
        assert classify_planet_fitness_title(title) is None


# (Basic-Fit, The Gym Group e PureGym: ver tests/test_v4_rodada2.py)


# ==========================================================================
# Benefit Systems
# ==========================================================================
class TestBenefitSystems:
    def test_active_sport_cards_is_relevant(self):
        title = "Quarterly information on active sport cards' number"
        assert (
            classify_benefit_title(title) == EventType.ACTIVE_SPORT_CARDS_UPDATE
        )

    def test_active_sport_cards_typographic_apostrophe(self):
        title = "Quarterly information on active sport cards\u2019 number"
        assert (
            classify_benefit_title(title) == EventType.ACTIVE_SPORT_CARDS_UPDATE
        )

    def test_consolidated_annual_report_is_relevant(self):
        title = "Consolidated annual report of Benefit Systems Group for 2025"
        assert classify_benefit_title(title) == EventType.FULL_YEAR_RESULTS

    def test_standalone_does_not_produce_a_second_alert(self):
        title = "Standalone annual report of Benefit Systems S.A. for 2025"
        assert classify_benefit_title(title) is None

    def test_consolidated_half_year(self):
        title = "Consolidated interim report for the first half of 2026"
        assert classify_benefit_title(title) == EventType.HALF_YEAR_RESULTS
        assert financial_report_period(title, date(2026, 8, 28)) == "H1/Q2-2026"

    def test_consolidated_q1(self):
        title = "Consolidated quarterly report Q1 2026"
        assert classify_benefit_title(title) == EventType.QUARTERLY_RESULTS
        assert financial_report_period(title, date(2026, 5, 20)) == "Q1-2026"

    def test_unrelated_current_report_rejected(self):
        assert classify_benefit_title("Change in the composition of the Management Board") is None
        assert classify_benefit_title("Conclusion of a significant agreement") is None

    def test_active_cards_period_from_publication_month(self):
        assert (
            active_cards_period(
                "Quarterly information on active sport cards' number", "", date(2026, 1, 8)
            )
            == "CARDS-Q4-2025"
        )
        assert (
            active_cards_period(
                "Quarterly information on active sport cards' number", "", date(2026, 7, 6)
            )
            == "CARDS-Q2-2026"
        )


# ==========================================================================
# SATS - pre-close call scripts and annual reports are not events
# ==========================================================================
class TestSATS:
    def test_pre_close_call_script_is_not_relevant(self):
        assert classify_sats_title("Pre-Close Call Script Q3 2026") is None
        assert classify_sats_title("Pre-close call script") is None

    def test_quarter_report_is_relevant(self):
        assert classify_sats_title("Q3 Report 2026") == EventType.QUARTERLY_RESULTS

    def test_annual_report_is_ignored(self):
        assert classify_sats_title("Annual Report 2025") is None

    def test_q4_report_is_the_full_year_event(self):
        assert classify_sats_title("Q4 Report 2025") == EventType.QUARTERLY_RESULTS


# ==========================================================================
# Xponential / Sports World period normalization
# ==========================================================================
class TestPeriodNormalization:
    def test_xponential_quarters(self):
        assert xponential_period("Q1 2026") == "Q1-2026"
        assert xponential_period("Third Quarter 2026") == "Q3-2026"
        assert xponential_period("FY 2025") == "Q4/FY-2025"

    def test_sports_world_filename(self):
        assert sports_world_period_tuple(
            "https://www.sportsworld.com.mx/uploads/es/documents/reports_quarterly/gsw_reporte_1T26.pdf"
        ) == (2026, 1)
        assert sports_world_period_tuple("2T26") == (2026, 2)


# Leejam: ver tests/test_v3_rodada1.py
# Bluefit, Selfit e Bodytech: ver tests/test_v2_brasil.py
