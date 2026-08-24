"""Run orchestration.

One company failing must never stop the other eleven. Every adapter runs inside
its own try/except and its own database transaction, and the run summary
reports successes, errors, events found, new events and alerts sent.
"""

from __future__ import annotations

import logging
from typing import Iterable

from ..config import CompanyConfig, load_companies
from ..database import (
    MonitorRun,
    company_has_baseline,
    is_bootstrapped,
    mark_bootstrapped,
    session_scope,
)
from ..emailer import EmailSender, build_sender
from ..models import CompanyRunResult, RunSummary
from ..monitors import ParserFailure, build_monitor
from ..util import now_local
from .alert_service import AlertService
from .event_resolver import resolve

logger = logging.getLogger(__name__)


class BootstrapRequired(RuntimeError):
    """Raised when `check` runs against an empty database."""


def select_companies(only: Iterable[str] | None = None) -> list[CompanyConfig]:
    companies = load_companies()
    if only:
        missing = [key for key in only if key not in companies]
        if missing:
            raise KeyError(f"unknown company key(s): {', '.join(missing)}")
        selected = [companies[key] for key in only]
    else:
        selected = [c for c in companies.values() if c.enabled]
    return selected


def run(
    *,
    mode: str = "check",
    only: Iterable[str] | None = None,
    dry_run: bool = False,
    sender: EmailSender | None = None,
    allow_missing_bootstrap: bool = False,
) -> RunSummary:
    """Execute a monitor run.

    ``mode='bootstrap'`` records everything currently published as baseline and
    sends nothing. ``mode='check'`` alerts on events that appear afterwards.
    """
    companies = select_companies(only)
    baseline = mode == "bootstrap"
    sender = sender or build_sender(dry_run=dry_run or baseline)
    alerts = AlertService(sender, dry_run=dry_run or baseline)

    summary = RunSummary(started_at=now_local())

    with session_scope() as session:
        if not baseline and not is_bootstrapped(session) and not allow_missing_bootstrap:
            raise BootstrapRequired(
                "database has no bootstrap marker. Run `python -m ir_monitor "
                "bootstrap` first, or pass --allow-missing-bootstrap to accept "
                "that every currently published disclosure may be alerted."
            )
        run_row = MonitorRun(started_at=summary.started_at, mode=mode, status="running")
        session.add(run_row)
        session.flush()
        run_id = run_row.id

    for config in companies:
        summary.results.append(
            _run_one(
                config,
                baseline=baseline,
                alerts=alerts,
                dry_run=dry_run,
                allow_missing_bootstrap=allow_missing_bootstrap,
            )
        )

    summary.finished_at = now_local()

    with session_scope() as session:
        if baseline:
            mark_bootstrapped(session)
        run_row = session.get(MonitorRun, run_id)
        if run_row is not None:
            run_row.finished_at = summary.finished_at
            run_row.status = "error" if summary.errors else "ok"
            run_row.companies_checked = summary.companies_checked
            run_row.events_found = summary.events_found
            run_row.new_events = summary.new_events
            run_row.alerts_sent = summary.alerts_sent
            run_row.errors = (
                "; ".join(f"{r.company}: {r.error}" for r in summary.errors) or None
            )

    logger.info(
        "action=run_finished mode=%s companies=%d ok=%d errors=%d relevant=%d "
        "new=%d alerts=%d",
        mode,
        summary.companies_checked,
        summary.companies_checked - len(summary.errors),
        len(summary.errors),
        summary.events_found,
        summary.new_events,
        summary.alerts_sent,
    )
    return summary


def _run_one(
    config: CompanyConfig,
    *,
    baseline: bool,
    alerts: AlertService,
    dry_run: bool,
    allow_missing_bootstrap: bool,
) -> CompanyRunResult:
    result = CompanyRunResult(company=config.key)
    try:
        monitor = build_monitor(config)
        candidates, events = monitor.run()
        result.candidates = len(candidates)
        result.relevant = len(events)
        result.events = events
        result.source_used = monitor.source_used
    except ParserFailure as exc:
        result.ok = False
        result.error = f"parser_failure: {exc}"
        logger.error("company=%s action=parser_failure error=%s", config.key, exc)
        return result
    except Exception as exc:  # noqa: BLE001 - isolate company failures
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        logger.exception("company=%s action=failed", config.key)
        return result

    try:
        with session_scope() as session:
            # A company added to the watch list after bootstrap has no rows
            # of its own; treat its first sighting as baseline so adding a
            # company never floods the inbox with its full history.
            treat_as_baseline = baseline
            if not baseline and not company_has_baseline(session, config.key):
                treat_as_baseline = True
                result.auto_baselined = True
                logger.warning(
                    "company=%s action=auto_baseline events=%d reason=no_history "
                    "detail=company has no rows yet (newly added, or its bootstrap "
                    "failed). Recording as baseline instead of emailing its full "
                    "history. Verify with `inspect` and re-run `check` afterwards.",
                    config.key,
                    len(events),
                )
            for event in events:
                resolution = resolve(session, event, baseline=treat_as_baseline)
                if resolution.is_new:
                    result.new_events += 1
                    result.new_event_keys.append(event.event_key)
                if resolution.should_alert:
                    if alerts.dispatch(
                        session,
                        resolution.event,
                        event,
                        allow_resend=resolution.is_revision,
                    ):
                        result.alerts_sent += 1
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"persistence_error: {type(exc).__name__}: {exc}"
        logger.exception("company=%s action=persistence_failed", config.key)
        return result

    logger.info(
        "company=%s candidates=%d relevant=%d new_events=%d alerts=%d",
        config.key,
        result.candidates,
        result.relevant,
        result.new_events,
        result.alerts_sent,
    )
    return result


def inspect(config_key: str) -> CompanyRunResult:
    """Fetch + classify only. Never touches the database."""
    config = select_companies([config_key])[0]
    result = CompanyRunResult(company=config_key)
    monitor = build_monitor(config)
    try:
        candidates, events = monitor.run()
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    result.candidates = len(candidates)
    result.relevant = len(events)
    result.events = events
    result.source_used = monitor.source_used
    return result
