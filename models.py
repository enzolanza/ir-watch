"""Domain models shared by every company monitor.

These are plain dataclasses (not ORM objects) so that adapters can be unit
tested without touching a database.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


# --------------------------------------------------------------------------
# Event types
# --------------------------------------------------------------------------
class EventType:
    """Canonical event types produced by the adapters.

    Kept as plain string constants (not an Enum) so that the values survive
    JSON round-trips and database storage without conversion helpers.
    """

    EARNINGS_RELEASE = "earnings_release"
    QUARTERLY_RESULTS = "quarterly_results"
    HALF_YEAR_RESULTS = "half_year_results"
    FULL_YEAR_RESULTS = "full_year_results"
    INTERIM_RESULTS = "interim_results"
    TRADING_UPDATE = "trading_update"
    PRE_CLOSE_TRADING_UPDATE = "pre_close_trading_update"
    ANNUAL_FINANCIAL_STATEMENTS = "annual_financial_statements"
    ACTIVE_SPORT_CARDS_UPDATE = "active_sport_cards_update"

    ALL = {
        EARNINGS_RELEASE,
        QUARTERLY_RESULTS,
        HALF_YEAR_RESULTS,
        FULL_YEAR_RESULTS,
        INTERIM_RESULTS,
        TRADING_UPDATE,
        PRE_CLOSE_TRADING_UPDATE,
        ANNUAL_FINANCIAL_STATEMENTS,
        ACTIVE_SPORT_CARDS_UPDATE,
    }


HUMAN_EVENT_TYPES = {
    EventType.EARNINGS_RELEASE: "Earnings Release",
    EventType.QUARTERLY_RESULTS: "Quarterly Results",
    EventType.HALF_YEAR_RESULTS: "Half Year Results",
    EventType.FULL_YEAR_RESULTS: "Full Year Results",
    EventType.INTERIM_RESULTS: "Interim Results",
    EventType.TRADING_UPDATE: "Trading Update",
    EventType.PRE_CLOSE_TRADING_UPDATE: "Pre-close Trading Update",
    EventType.ANNUAL_FINANCIAL_STATEMENTS: "Annual Financial Statements",
    EventType.ACTIVE_SPORT_CARDS_UPDATE: "Active Sport Cards Update",
}


@dataclass
class CandidateEvent:
    """Raw item extracted from a source, before classification/normalization.

    A candidate is *anything* the source listed. Whether it is a relevant
    periodic disclosure is decided later by the adapter's ``classify`` step.
    """

    company: str
    source: str
    title: str
    url: str | None = None
    document_url: str | None = None
    publication_date: date | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Candidate {self.company}/{self.source} {self.title!r}>"


@dataclass
class NormalizedEvent:
    """A relevant periodic disclosure, normalized to a common shape.

    ``event_key`` is the *logical* identity of the disclosure and is what
    deduplication and alerting are based on. ``document_identifier`` is the
    *technical* identity of one particular file/record and may differ between
    sources describing the same event.
    """

    company: str
    event_type: str
    reporting_period: str
    title: str
    source: str

    publication_date: date | None = None
    primary_url: str | None = None
    document_url: str | None = None
    document_identifier: str | None = None

    # Optional related material. Never a trigger on its own.
    presentation_url: str | None = None
    webcast_url: str | None = None
    transcript_url: str | None = None
    financial_statements_url: str | None = None
    pdf_url: str | None = None
    analyst_tool_url: str | None = None
    saudi_exchange_announcement_url: str | None = None
    results_release_url: str | None = None
    earnings_presentation_url: str | None = None

    guid: str | None = None
    release_id: str | None = None
    report_number: str | None = None
    issuer: str | None = None
    ticker: str | None = None

    metadata: dict[str, Any] = field(default_factory=dict)

    # Set by the adapter. When False the event resolver keys only on
    # company + reporting_period (Bodytech, Leejam, PureGym, SATS, ...).
    key_includes_event_type: bool = True

    @property
    def event_key(self) -> str:
        """Logical key. One alert per distinct event_key, ever."""
        if self.key_includes_event_type:
            return f"{self.company}|{self.event_type}|{self.reporting_period}"
        return f"{self.company}|{self.reporting_period}"

    @property
    def technical_id(self) -> str:
        """Stable technical identifier for the concrete document/record."""
        if self.document_identifier:
            return self.document_identifier
        basis = self.document_url or self.primary_url or self.title
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    def extra_links(self) -> list[tuple[str, str]]:
        """Named supplementary links, for the alert body."""
        pairs = [
            ("Presentation", self.presentation_url),
            ("Earnings Presentation", self.earnings_presentation_url),
            ("Webcast", self.webcast_url),
            ("Transcript", self.transcript_url),
            ("Financial Statements", self.financial_statements_url),
            ("Results Release", self.results_release_url),
            ("Analyst Tool", self.analyst_tool_url),
            ("Saudi Exchange Announcement", self.saudi_exchange_announcement_url),
        ]
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for label, url in pairs:
            if url and url not in seen:
                seen.add(url)
                out.append((label, url))
        return out

    def as_metadata(self) -> dict[str, Any]:
        payload = dict(self.metadata)
        for name in (
            "presentation_url",
            "webcast_url",
            "transcript_url",
            "financial_statements_url",
            "pdf_url",
            "analyst_tool_url",
            "saudi_exchange_announcement_url",
            "results_release_url",
            "earnings_presentation_url",
            "guid",
            "release_id",
            "report_number",
            "issuer",
            "ticker",
        ):
            value = getattr(self, name)
            if value:
                payload[name] = value
        return payload


@dataclass
class CompanyRunResult:
    """Per-company outcome of a monitor run."""

    company: str
    ok: bool = True
    candidates: int = 0
    relevant: int = 0
    new_events: int = 0
    alerts_sent: int = 0
    error: str | None = None
    auto_baselined: bool = False
    source_used: str | None = None
    events: list[NormalizedEvent] = field(default_factory=list)
    new_event_keys: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    started_at: datetime | None = None
    finished_at: datetime | None = None
    results: list[CompanyRunResult] = field(default_factory=list)

    @property
    def companies_checked(self) -> int:
        return len(self.results)

    @property
    def errors(self) -> list[CompanyRunResult]:
        return [r for r in self.results if not r.ok]

    @property
    def events_found(self) -> int:
        return sum(r.relevant for r in self.results)

    @property
    def new_events(self) -> int:
        return sum(r.new_events for r in self.results)

    @property
    def alerts_sent(self) -> int:
        return sum(r.alerts_sent for r in self.results)
