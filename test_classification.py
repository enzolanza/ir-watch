"""Title classification, with explicit coverage of the known false positives."""

from __future__ import annotations

from datetime import date

import pytest

from ir_monitor.models import EventType
from ir_monitor.monitors.basic_fit import basic_fit_period, classify_basic_fit_title
from ir_monitor.monitors.benefit_systems import (
    active_cards_period,
    classify_benefit_title,
    financial_report_period,
)
from ir_monitor.monitors.bluefit import classify_bluefit_document
from ir_monitor.monitors.bodytech import bodytech_fiscal_year, classify_bodytech_document
from ir_monitor.monitors.leejam import leejam_period
from ir_monitor.monitors.planet_fitness import (
    classify_planet_fitness_title,
    planet_fitness_period,
)
from ir_monitor.monitors.puregym import puregym_period
from ir_monitor.monitors.sats import classify_sats_title
from ir_monitor.monitors.selfit import classify_selfit_document
from ir_monitor.monitors.sports_world import sports_world_period_tuple
from ir_monitor.monitors.the_gym_group import classify_tgg_title, tgg_period
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


# ==========================================================================
# The Gym Group - "Notice of Pre-Close Trading Update" must never be an alert
# ==========================================================================
class TestTheGymGroup:
    def test_notice_of_pre_close_is_not_relevant(self):
        assert classify_tgg_title("Notice of Pre-Close Trading Update") is None

    def test_pre_close_trading_update_is_relevant(self):
        assert (
            classify_tgg_title("Pre-close trading update")
            == EventType.PRE_CLOSE_TRADING_UPDATE
        )

    def test_case_and_hyphen_variants(self):
        for title in ("Pre-Close Trading Update", "Pre close trading update"):
            assert (
                classify_tgg_title(title) == EventType.PRE_CLOSE_TRADING_UPDATE
            ), title

    def test_full_year_and_interim(self):
        assert classify_tgg_title("Full Year Results 2025") == EventType.FULL_YEAR_RESULTS
        assert classify_tgg_title("Interim Results 2026") == EventType.INTERIM_RESULTS

    def test_annual_report_ignored(self):
        assert classify_tgg_title("Annual Report and Accounts 2025") is None

    def test_january_pre_close_maps_to_previous_year(self):
        period = tgg_period(
            EventType.PRE_CLOSE_TRADING_UPDATE, "Pre-close trading update", date(2026, 1, 15)
        )
        assert period == "FY_PRE_CLOSE-2025"

    def test_july_pre_close_maps_to_current_h1(self):
        period = tgg_period(
            EventType.PRE_CLOSE_TRADING_UPDATE, "Pre-close trading update", date(2026, 7, 10)
        )
        assert period == "H1_PRE_CLOSE-2026"

    def test_march_full_year_without_year_in_title(self):
        period = tgg_period(
            EventType.FULL_YEAR_RESULTS, "Full Year Results", date(2026, 3, 12)
        )
        assert period == "FY-2025"

    def test_september_interim(self):
        period = tgg_period(
            EventType.INTERIM_RESULTS, "Interim Results", date(2026, 9, 11)
        )
        assert period == "H1-2026"


# ==========================================================================
# Basic-Fit - all five disclosures are relevant
# ==========================================================================
class TestBasicFit:
    def test_q1_trading_update_is_relevant(self):
        assert classify_basic_fit_title("Q1 Trading Update") == EventType.TRADING_UPDATE
        assert (
            basic_fit_period(EventType.TRADING_UPDATE, "Q1 Trading Update 2026", None)
            == "Q1-2026"
        )

    def test_january_trading_update(self):
        assert (
            classify_basic_fit_title("January Trading Update")
            == EventType.TRADING_UPDATE
        )
        assert (
            basic_fit_period(
                EventType.TRADING_UPDATE, "January Trading Update", date(2026, 1, 14)
            )
            == "JAN-TU-2026"
        )

    def test_q3_trading_update(self):
        assert (
            basic_fit_period(EventType.TRADING_UPDATE, "Q3 Trading Update 2026", None)
            == "Q3-2026"
        )

    def test_half_year_results(self):
        assert (
            classify_basic_fit_title("Half Year Results 2026")
            == EventType.HALF_YEAR_RESULTS
        )
        assert (
            basic_fit_period(EventType.HALF_YEAR_RESULTS, "Half Year Results 2026", None)
            == "H1-2026"
        )

    def test_full_year_results(self):
        assert (
            classify_basic_fit_title("Full Year Results 2025")
            == EventType.FULL_YEAR_RESULTS
        )
        assert (
            basic_fit_period(
                EventType.FULL_YEAR_RESULTS, "Full Year Results 2025", date(2026, 3, 5)
            )
            == "FY-2025"
        )

    @pytest.mark.parametrize(
        "title",
        [
            "Capital Markets Day 2026",
            "Annual Report 2025",
            "Q1 Trading Update presentation",
            "Notice of Annual General Meeting",
        ],
    )
    def test_non_events_rejected(self, title):
        assert classify_basic_fit_title(title) is None


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
# Xponential / Sports World / PureGym / Leejam period normalization
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

    def test_puregym_periods(self):
        assert puregym_period("Q3 2026") == "Q3-2026"
        assert puregym_period("FY 2025") == "Q4/FY-2025"
        assert (
            puregym_period(
                "https://s28.q4cdn.com/583314398/files/doc_financials/2024/q3/PureGym-Q324-Report-Final.pdf"
            )
            == "Q3-2024"
        )

    def test_leejam_periods_from_period_end_date(self):
        assert (
            leejam_period(
                "Leejam Sports Company Announces the Interim Consolidated "
                "Financial Results for the Period Ending on 2026-03-31"
            )
            == "Q1-2026"
        )
        assert (
            leejam_period(
                "Interim Consolidated Financial Results for the Period Ending on 2026-06-30"
            )
            == "Q2/H1-2026"
        )
        assert (
            leejam_period(
                "Annual Consolidated Financial Results for the Period Ending on 2025-12-31"
            )
            == "Q4/FY-2025"
        )

    def test_leejam_result_center_labels(self):
        assert leejam_period("1st Quarter 2026") == "Q1-2026"
        assert leejam_period("3rd Quarter 2026") == "Q3/9M-2026"


# ==========================================================================
# Bluefit / Selfit / Bodytech
# ==========================================================================
class TestBrazilianAdapters:
    def test_bluefit_accepts_alternative_release_names(self):
        for title in (
            "Release de Resultados 1T26",
            "Comentário de Desempenho 2T26",
            "Relatório da Administração 4T25",
        ):
            assert classify_bluefit_document(title) == EventType.EARNINGS_RELEASE, title

    def test_bluefit_rejects_other_documents(self):
        for title in (
            "Apresentação de Resultados 1T26",
            "Demonstrações Financeiras 2025",
            "Formulário de Referência 2026",
            "Transcrição da Teleconferência 1T26",
            "Ata da Assembleia Geral Ordinária",
        ):
            assert classify_bluefit_document(title) is None, title

    def test_selfit_annual_statements_from_section(self):
        assert (
            classify_selfit_document(
                "DF 2025", "Demonstrações financeiras anuais", "/pdfs/df-2025.pdf"
            )
            == EventType.ANNUAL_FINANCIAL_STATEMENTS
        )

    def test_selfit_ignores_other_sections(self):
        assert (
            classify_selfit_document(
                "Ata da AGO 2026", "Assembleias e Atas", "/pdfs/ata-ago-2026.pdf"
            )
            is None
        )
        assert (
            classify_selfit_document(
                "Relatório do Agente Fiduciário", "Debêntures", "/pdfs/agente.pdf"
            )
            is None
        )

    def test_bodytech_financial_statements_only(self):
        assert (
            classify_bodytech_document(
                "Demonstrações Financeiras 2025", "Demonstrações financeiras", "/df2025.pdf"
            )
            == EventType.ANNUAL_FINANCIAL_STATEMENTS
        )
        assert (
            classify_bodytech_document(
                "Edital de Convocação", "Outras publicações legais", "/edital.pdf"
            )
            is None
        )

    def test_bodytech_fiscal_year_from_exercise_wording(self):
        text = (
            "Demonstrações financeiras referentes ao exercício social findo em "
            "31 de dezembro de 2025"
        )
        assert bodytech_fiscal_year(text) == 2025
