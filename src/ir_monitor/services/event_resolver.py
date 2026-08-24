"""Event resolution and deduplication.

Every source observation converges on a single ``Event`` row keyed by
``event_key``. The resolver answers one question:

    Is this a genuinely new logical disclosure, or another sighting of one we
    already know about?

Republication, a changed URL, a second language version, or the same result
appearing on a second source all resolve to the existing event and only add a
``SourceObservation``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import Event, SourceObservation
from ..models import NormalizedEvent
from ..util import now_local

logger = logging.getLogger(__name__)


@dataclass
class ResolutionResult:
    event: Event
    is_new: bool
    is_revision: bool = False
    should_alert: bool = False


def resolve(
    session: Session,
    normalized: NormalizedEvent,
    *,
    baseline: bool = False,
) -> ResolutionResult:
    """Insert or enrich, then decide whether an alert is warranted."""
    settings = get_settings()
    now = now_local()

    existing = session.execute(
        select(Event).where(Event.event_key == normalized.event_key)
    ).scalar_one_or_none()

    if existing is None:
        event = Event(
            company=normalized.company,
            event_type=normalized.event_type,
            reporting_period=normalized.reporting_period,
            event_key=normalized.event_key,
            title=normalized.title,
            publication_date=normalized.publication_date,
            primary_url=normalized.primary_url,
            document_url=normalized.document_url,
            is_baseline=baseline,
            first_seen=now,
            created_at=now,
            updated_at=now,
            metadata_json=json.dumps(normalized.as_metadata(), ensure_ascii=False),
        )
        session.add(event)
        session.flush()
        _record_observation(session, event, normalized, now)
        logger.info(
            "company=%s action=new_event event_key=%s baseline=%s",
            normalized.company,
            normalized.event_key,
            baseline,
        )
        return ResolutionResult(event=event, is_new=True, should_alert=not baseline)

    # Known event: enrich only.
    is_revision = _enrich(session, existing, normalized, now)
    _record_observation(session, existing, normalized, now)
    should_alert = bool(
        is_revision and settings.alert_on_revision and existing.alert_sent_at is not None
    )
    if is_revision:
        logger.info(
            "company=%s action=revision event_key=%s alert=%s",
            normalized.company,
            normalized.event_key,
            should_alert,
        )
    return ResolutionResult(
        event=existing, is_new=False, is_revision=is_revision, should_alert=should_alert
    )


def _enrich(
    session: Session, event: Event, normalized: NormalizedEvent, now
) -> bool:
    """Fill in missing fields. Returns True when the primary document changed."""
    changed_document = False

    if not event.publication_date and normalized.publication_date:
        event.publication_date = normalized.publication_date
    if not event.primary_url and normalized.primary_url:
        event.primary_url = normalized.primary_url
    if normalized.document_url:
        if not event.document_url:
            event.document_url = normalized.document_url
        elif event.document_url != normalized.document_url:
            changed_document = True

    metadata = event.metadata_dict
    incoming = normalized.as_metadata()
    for key, value in incoming.items():
        metadata.setdefault(key, value)
    alternates = metadata.setdefault("alternate_documents", [])
    if normalized.document_url and normalized.document_url != event.document_url:
        if normalized.document_url not in alternates:
            alternates.append(normalized.document_url)
    event.metadata_json = json.dumps(metadata, ensure_ascii=False)
    event.updated_at = now
    return changed_document


def _record_observation(
    session: Session, event: Event, normalized: NormalizedEvent, now
) -> None:
    identifier = normalized.technical_id
    observation = session.execute(
        select(SourceObservation).where(
            SourceObservation.event_id == event.id,
            SourceObservation.source_name == normalized.source,
            SourceObservation.document_identifier == identifier,
        )
    ).scalar_one_or_none()

    if observation is None:
        session.add(
            SourceObservation(
                event_id=event.id,
                source_name=normalized.source,
                source_url=normalized.primary_url or normalized.document_url,
                document_identifier=identifier,
                first_seen=now,
                last_seen=now,
                metadata_json=json.dumps(
                    normalized.as_metadata(), ensure_ascii=False
                ),
            )
        )
    else:
        observation.last_seen = now


def mark_alert_sent(session: Session, event: Event) -> None:
    event.alert_sent_at = now_local()
    event.updated_at = event.alert_sent_at
