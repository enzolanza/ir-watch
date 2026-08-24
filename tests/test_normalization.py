"""Period normalization, URL canonicalization and event key construction."""

from __future__ import annotations

import pytest

from ir_monitor.models import EventType, NormalizedEvent
from ir_monitor.normalization import (
    canonical_url,
    extract_quarter_and_year,
    fy_period,
    half_year_period,
    normalize_year,
    parse_date,
    quarter_period,
    slug_title,
    squash,
)


class TestPeriods:
    def test_quarters(self):
        assert quarter_period(1, 2026) == "Q1-2026"
        assert quarter_period(2, 2026) == "Q2-2026"
        assert quarter_period(3, 2026) == "Q3-2026"

    def test_fourth_quarter_is_combined_with_full_year(self):
        assert quarter_period(4, 2025) == "Q4/FY-2025"

    def test_invalid_quarter(self):
        with pytest.raises(ValueError):
            quarter_period(5, 2026)

    def test_other_periods(self):
        assert fy_period(2025) == "FY-2025"
        assert half_year_period(2026) == "H1-2026"

    def test_two_digit_years(self):
        assert normalize_year("26") == 2026
        assert normalize_year(2026) == 2026

    def test_extract_quarter_and_year(self):
        assert extract_quarter_and_year("Release 1T26") == (1, 2026)
        assert extract_quarter_and_year("Q3 Report 2026") == (3, 2026)
        assert extract_quarter_and_year("Resultados 4T2025") == (4, 2025)
        assert extract_quarter_and_year("Annual Report") is None


class TestURLs:
    def test_tracking_params_removed(self):
        url = "https://example.com/report.pdf?utm_source=news&utm_medium=email&id=42"
        assert canonical_url(url) == "https://example.com/report.pdf?id=42"

    def test_meaningful_params_preserved(self):
        url = "https://api.mziq.com/mzfilemanager/v2/d/abc/def?origin=1"
        assert canonical_url(url) == url

    def test_fragment_dropped_by_default(self):
        assert (
            canonical_url("https://example.com/results#q1")
            == "https://example.com/results"
        )

    def test_trailing_slash_and_case(self):
        assert (
            canonical_url("https://Example.COM/investors/")
            == "https://example.com/investors"
        )

    def test_none_passthrough(self):
        assert canonical_url(None) is None


class TestText:
    def test_squash_normalizes_quotes_and_spaces(self):
        assert squash("active  sport\u2019s   number") == "active sport's number"

    def test_slug_removes_accents(self):
        assert slug_title("Comentário de Desempenho") == "comentario de desempenho"


class TestDates:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2026-03-31", (2026, 3, 31)),
            ("31/03/2026", (2026, 3, 31)),
            ("28 April 2026", (2026, 4, 28)),
            ("April 28, 2026", (2026, 4, 28)),
            ("28 de abril de 2026", (2026, 4, 28)),
        ],
    )
    def test_parse(self, value, expected):
        parsed = parse_date(value)
        assert (parsed.year, parsed.month, parsed.day) == expected

    def test_unparseable_returns_none(self):
        assert parse_date("sometime next quarter") is None


class TestEventKeys:
    def _event(self, **kwargs) -> NormalizedEvent:
        base = dict(
            company="basic_fit",
            event_type=EventType.TRADING_UPDATE,
            reporting_period="Q1-2026",
            title="Q1 Trading Update",
            source="test",
        )
        base.update(kwargs)
        return NormalizedEvent(**base)

    def test_key_with_event_type(self):
        assert self._event().event_key == "basic_fit|trading_update|Q1-2026"

    def test_key_without_event_type(self):
        event = self._event(key_includes_event_type=False)
        assert event.event_key == "basic_fit|Q1-2026"

    def test_same_period_different_type_are_distinct_events(self):
        a = self._event(event_type=EventType.TRADING_UPDATE)
        b = self._event(event_type=EventType.HALF_YEAR_RESULTS)
        assert a.event_key != b.event_key

    def test_technical_id_falls_back_to_url_hash(self):
        event = self._event(document_url="https://example.com/a.pdf")
        assert len(event.technical_id) == 32

    def test_technical_id_prefers_explicit_identifier(self):
        event = self._event(document_identifier="uuid-1234")
        assert event.technical_id == "uuid-1234"

    def test_extra_links_deduplicated(self):
        event = self._event(
            presentation_url="https://example.com/p.pdf",
            webcast_url="https://example.com/p.pdf",
        )
        assert len(event.extra_links()) == 1
