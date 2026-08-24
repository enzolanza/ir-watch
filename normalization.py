"""Deterministic normalization helpers.

Everything here is pure and side-effect free so it can be unit tested without
network access. No LLM is involved in any classification decision.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def squash(text: str) -> str:
    """Collapse whitespace and normalize quotes/dashes for stable matching."""
    if not text:
        return ""
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2011", "-")
    text = text.replace("\xa0", " ")
    return _WS_RE.sub(" ", text).strip()


def slug_title(text: str) -> str:
    """Lowercased, accent-free, whitespace-collapsed title for regex matching."""
    return strip_accents(squash(text)).lower()


# --------------------------------------------------------------------------
# Reporting period normalization
# --------------------------------------------------------------------------

FY_LABEL = "Q4/FY"


def quarter_period(quarter: int, year: int) -> str:
    """Q1-2026 ... Q3-2026, and Q4/FY-2026 for the fourth quarter.

    Companies whose Q4 disclosure *is* the full-year disclosure use the
    combined ``Q4/FY`` label so a later FY document cannot create a second
    logical event for the same economic disclosure.
    """
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"invalid quarter: {quarter}")
    if quarter == 4:
        return f"{FY_LABEL}-{year}"
    return f"Q{quarter}-{year}"


def fy_period(year: int) -> str:
    return f"FY-{year}"


def half_year_period(year: int) -> str:
    return f"H1-{year}"


def normalize_year(value: int | str) -> int:
    """Accept 26, '26', 2026, '2026' and return a four digit year."""
    year = int(str(value).strip())
    if year < 100:
        year += 2000
    if not (1990 <= year <= 2100):
        raise ValueError(f"implausible year: {value}")
    return year


_QUARTER_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "primer": 1,
    "primero": 1,
    "segundo": 2,
    "tercer": 3,
    "tercero": 3,
    "cuarto": 4,
    "1st": 1,
    "2nd": 2,
    "3rd": 3,
    "4th": 4,
    "1o": 1,
    "2o": 2,
    "3o": 3,
    "4o": 4,
}

_QUARTER_PATTERNS = [
    re.compile(r"\bq([1-4])\b"),
    re.compile(r"\b([1-4])\s*t(?:rim|rimestre)?\b"),  # 1T26 / 1 trimestre
    re.compile(r"\b([1-4])\s*[oº]?\s*quarter\b"),
    re.compile(r"\b([1-4])\s*[oº]?\s*trimestre\b"),
]

_YEAR_RE = re.compile(r"\b(20\d{2})\b")
# 1T26 / 4T2025 / "1 T 26" - the digit must not be part of a longer number.
_SHORT_PERIOD_RE = re.compile(r"(?<!\d)([1-4])\s*t\s*(\d{4}|\d{2})(?!\d)")


def extract_year(text: str) -> int | None:
    match = _YEAR_RE.search(squash(text))
    return int(match.group(1)) if match else None


def extract_quarter(text: str) -> int | None:
    """Find a quarter number in free text using deterministic patterns."""
    low = slug_title(text)
    short = _SHORT_PERIOD_RE.search(low)
    if short:
        return int(short.group(1))
    for pattern in _QUARTER_PATTERNS:
        match = pattern.search(low)
        if match:
            return int(match.group(1))
    for word, quarter in _QUARTER_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            return quarter
    return None


def extract_quarter_and_year(text: str) -> tuple[int, int] | None:
    """Return (quarter, year) from strings like '1T26' or 'Q3 Report 2026'."""
    low = slug_title(text)
    short = _SHORT_PERIOD_RE.search(low)
    if short:
        return int(short.group(1)), normalize_year(short.group(2))
    quarter = extract_quarter(low)
    year = extract_year(low)
    if quarter and year:
        return quarter, year
    return None


# --------------------------------------------------------------------------
# URL canonicalization
# --------------------------------------------------------------------------

_TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "_hs")
_TRACKING_PARAMS = {
    "gclid",
    "fbclid",
    "msclkid",
    "mkt_tok",
    "igshid",
    "ref",
    "ref_src",
    "spm",
    "s_cid",
    "cmpid",
    "campaignid",
    "trk",
}


def canonical_url(url: str | None, *, keep_fragment: bool = False) -> str | None:
    """Remove tracking noise while preserving parameters that identify the doc.

    Only parameters that are *clearly* analytics related are removed. Anything
    else (ids, tokens needed to access the file, language selectors) is kept.
    """
    if not url:
        return None
    url = squash(url)
    parts = urlsplit(url)
    if not parts.scheme and not parts.netloc:
        return url

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith(_TRACKING_PREFIXES) or k.lower() in _TRACKING_PARAMS)
    ]
    netloc = parts.netloc.lower()
    if netloc.endswith(":443") and parts.scheme == "https":
        netloc = netloc[: -len(":443")]
    if netloc.endswith(":80") and parts.scheme == "http":
        netloc = netloc[: -len(":80")]

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    fragment = parts.fragment if keep_fragment else ""
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), fragment))


# --------------------------------------------------------------------------
# Date parsing
# --------------------------------------------------------------------------

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%m/%d/%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
)

_PT_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
}
_ES_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}


def parse_date(value: str | None) -> date | None:
    """Best effort, deterministic date parsing. Returns None when unsure."""
    if not value:
        return None
    text = squash(str(value))
    if not text:
        return None

    iso_candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).date()
    except ValueError:
        pass

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    low = strip_accents(text.lower())
    match = re.search(r"(\d{1,2})\s*(?:de\s+)?([a-z]+)\s*(?:de\s+)?(\d{4})", low)
    if match:
        day, month_name, year = match.groups()
        month = _PT_MONTHS.get(month_name) or _ES_MONTHS.get(month_name)
        if month:
            try:
                return date(int(year), month, int(day))
            except ValueError:
                return None
    return None
