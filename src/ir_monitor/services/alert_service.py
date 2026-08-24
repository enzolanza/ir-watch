"""Alert dispatch.

One email per logical event. The alert timestamp is written *after* a
successful send, so a crash mid-run can retry rather than silently swallow an
alert.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import Event
from ..emailer import EmailSender, EmailSendError
from ..models import NormalizedEvent
from ..util import now_local
from .event_resolver import mark_alert_sent

logger = logging.getLogger(__name__)


class AlertService:
    def __init__(self, sender: EmailSender, dry_run: bool = False):
        self.sender = sender
        self.dry_run = dry_run

    def dispatch(
        self,
        session: Session,
        event_row: Event,
        normalized: NormalizedEvent,
        *,
        allow_resend: bool = False,
    ) -> bool:
        """Send one alert. Returns True when an email actually went out.

        ``allow_resend`` is only set by the resolver for a deliberate revision
        alert (ALERT_ON_REVISION=true). Ordinary re-detection of an already
        alerted event never gets past the guard below.
        """
        if event_row.alert_sent_at is not None and not allow_resend and not self.dry_run:
            logger.info(
                "company=%s action=alert_skipped reason=already_sent event_key=%s",
                normalized.company,
                normalized.event_key,
            )
            return False

        detected_at = event_row.first_seen or now_local()
        try:
            self.sender.send_event_alert(normalized, detected_at)
        except EmailSendError as exc:
            logger.error(
                "company=%s action=alert_failed event_key=%s error=%s",
                normalized.company,
                normalized.event_key,
                exc,
            )
            return False

        if not self.dry_run:
            mark_alert_sent(session, event_row)
        logger.info(
            "company=%s action=alert_sent event_key=%s dry_run=%s",
            normalized.company,
            normalized.event_key,
            self.dry_run,
        )
        return True
