"""Small shared utilities: timezone-aware clocks and PDF validation."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .config import LOCAL_TZ

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF"


def now_local() -> datetime:
    """Timezone-aware 'now' in America/Sao_Paulo (configurable)."""
    return datetime.now(tz=LOCAL_TZ)


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TZ)


def format_local(value: datetime | None) -> str:
    local = to_local(value)
    return local.strftime("%d/%m/%Y %H:%M %Z") if local else "-"


# --------------------------------------------------------------------------
# PDF validation
# --------------------------------------------------------------------------

def looks_like_pdf(content: bytes, content_type: str | None = None) -> bool:
    """A 200 response is not proof of a PDF. Check the magic bytes."""
    if content[:4] == PDF_MAGIC:
        return True
    # Some servers prepend a BOM/whitespace.
    if PDF_MAGIC in content[:1024]:
        return True
    if content_type and "application/pdf" in content_type.lower():
        # Content-Type alone is weak evidence; only trusted when body is empty
        # (e.g. HEAD request with no body returned).
        return not content
    return False


def extract_pdf_text(data: bytes, max_pages: int = 2) -> str:
    """Extract text from the first pages. Returns '' when no backend exists."""
    try:  # pragma: no cover - depends on optional dependency
        import fitz  # PyMuPDF

        with fitz.open(stream=data, filetype="pdf") as doc:
            pages = [doc[i].get_text() for i in range(min(max_pages, doc.page_count))]
        return "\n".join(pages)
    except Exception:  # noqa: BLE001 - fall through to pypdf
        pass
    try:  # pragma: no cover - depends on optional dependency
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [
            (reader.pages[i].extract_text() or "")
            for i in range(min(max_pages, len(reader.pages)))
        ]
        return "\n".join(pages)
    except Exception:  # noqa: BLE001
        logger.debug("action=pdf_text_extraction_unavailable")
        return ""


def text_contains_all(text: str, patterns: list[str]) -> bool:
    """Deterministic content validation. No LLM."""
    low = text.lower()
    return all(re.search(pattern, low, flags=re.IGNORECASE) for pattern in patterns)
