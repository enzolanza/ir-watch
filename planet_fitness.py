"""Planet Fitness - official press release RSS feed.

Source of truth: investor.planetfitness.com RSS. No HTML scraping, no
Playwright. SEC filings, presentations and events are explicitly out of scope.

The only trap here is that the company pre-announces its reporting date with a
"To Report ... Results" release. That title contains the word "Results" and the
quarter, so substring matching on "Results" is not acceptable.
"""

from __future__ import annotations

import re

from ..models import CandidateEvent, EventType, NormalizedEvent
from ..normalization import (
    canonical_url,
    normalize_year,
    quarter_period,
    slug_title,
)
from .base import CompanyMonitor, RSSSourceMixin, candidate

SOURCE_RSS = "planet_fitness_press_release_rss"

DEFAULT_FEED = "https://investor.planetfitness.com/rss/pressrelease.aspx"

# Rejections are evaluated first and win over any acceptance rule.
REJECT_PATTERNS = [
    re.compile(r"\bto\s+report\b"),                 # date announcement
    re.compile(r"\bto\s+announce\b"),
    re.compile(r"\bwill\s+report\b"),
    re.compile(r"\bkey\s+year-?end\s+metrics\b"),   # preliminary metrics
    re.compile(r"\bpreliminary\b"),
    re.compile(r"\bconference\s+call\b"),
    re.compile(r"\bwebcast\b"),
]

QUARTER_WORD_TO_NUMBER = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
}

# "Announces Second Quarter 2026 Results"
# "Announces Fourth Quarter and Full Year 2025 Results"
ANNOUNCE_RE = re.compile(
    r"\bannounce[sd]?\b.*?\b(first|second|third|fourth)\s+quarter\b"
    r"(?:\s+and\s+full\s+year)?\s*(?:20\d{2})?",
)
PERIOD_RE = re.compile(
    r"\b(first|second|third|fourth)\s+quarter\b"
    r"(?:\s+and\s+full\s+year)?\s+(20\d{2})\b"
)
FULL_YEAR_ONLY_RE = re.compile(r"\bannounce[sd]?\b.*?\bfull\s+year\s+(20\d{2})\s+results\b")
RESULTS_RE = re.compile(r"\bresults\b")


class PlanetFitnessMonitor(RSSSourceMixin, CompanyMonitor):
    key = "planet_fitness"
    min_expected_candidates = 1

    def fetch_candidates(self) -> list[CandidateEvent]:
        feed_url = self.config.option("rss_url", DEFAULT_FEED)
        entries = self.fetch_feed_entries(feed_url)
        self.source_used = SOURCE_RSS
        return [
            candidate(
                self.key,
                SOURCE_RSS,
                entry["title"],
                url=entry.get("link"),
                publication_date=entry.get("published"),
                guid=entry.get("guid"),
                summary=entry.get("summary"),
            )
            for entry in entries
        ]

    # ------------------------------------------------------------------
    def classify(self, cand: CandidateEvent) -> str | None:
        return classify_planet_fitness_title(cand.title)

    def normalize(self, cand: CandidateEvent, event_type: str) -> NormalizedEvent | None:
        period = planet_fitness_period(cand.title)
        if not period:
            return None
        guid = cand.raw.get("guid")
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=period,
            title=cand.title,
            source=cand.source,
            publication_date=cand.publication_date,
            primary_url=cand.url,
            document_url=cand.url,
            document_identifier=canonical_url(guid) or canonical_url(cand.url),
            guid=guid,
            ticker="PLNT",
            issuer="Planet Fitness, Inc.",
        )


def classify_planet_fitness_title(title: str) -> str | None:
    """Deterministic title rule. Rejections evaluated before acceptances."""
    low = slug_title(title)
    if any(pattern.search(low) for pattern in REJECT_PATTERNS):
        return None
    if not RESULTS_RE.search(low):
        return None
    if ANNOUNCE_RE.search(low) or FULL_YEAR_ONLY_RE.search(low):
        return EventType.EARNINGS_RELEASE
    return None


def planet_fitness_period(title: str) -> str | None:
    low = slug_title(title)
    match = PERIOD_RE.search(low)
    if match:
        quarter = QUARTER_WORD_TO_NUMBER[match.group(1)]
        return quarter_period(quarter, normalize_year(match.group(2)))
    full_year = FULL_YEAR_ONLY_RE.search(low)
    if full_year:
        return quarter_period(4, normalize_year(full_year.group(1)))
    return None
