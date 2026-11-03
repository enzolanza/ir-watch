"""Run orchestration.

One company failing must never stop the other eleven. Every adapter runs inside
its own try/except and its own database transaction, and the run summary
reports successes, errors, events found, new events and alerts sent.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from sqlalchemy import select

from ..config import CompanyConfig, load_companies
from ..database import (
    Event,
    Meta,
    MonitorRun,
    company_has_baseline,
    is_bootstrapped,
    mark_bootstrapped,
    session_scope,
)
from ..emailer import EmailSender, EmailSendError, build_sender, company_name
from ..models import CompanyRunResult, RunSummary
from ..monitors import ParserFailure, build_monitor
from ..util import format_local, now_local
from .alert_service import AlertService
from .event_resolver import resolve

logger = logging.getLogger(__name__)

#: v2: empresas cujo histórico antigo é apagado UMA vez, para que a linha de
#: base seja refeita com os novos leitores (evita alertas falsos de anos
#: antigos que o leitor antigo não tinha registrado).
V2_RESET_COMPANIES = ("bluefit", "selfit", "bodytech")
V2_RESET_FLAG = "v2_reset_done"


def _blind_after() -> int:
    """Quantas execuções seguidas com falha até abrir o aviso 'sem monitoramento'."""
    try:
        return max(1, int(os.getenv("BLIND_AFTER_FAILURES", "3")))
    except ValueError:
        return 3


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

    run_id = None
    with session_scope() as session:
        if not baseline and not is_bootstrapped(session) and not allow_missing_bootstrap:
            raise BootstrapRequired(
                "database has no bootstrap marker. Run `python -m ir_monitor "
                "bootstrap` first, or pass --allow-missing-bootstrap to accept "
                "that every currently published disclosure may be alerted."
            )
        if not dry_run:
            _v2_reset_once(session)
            run_row = MonitorRun(started_at=summary.started_at, mode=mode, status="running")
            session.add(run_row)
            session.flush()
            run_id = run_row.id

    for config in companies:
        result = _run_one(
            config,
            baseline=baseline,
            alerts=alerts,
            dry_run=dry_run,
            allow_missing_bootstrap=allow_missing_bootstrap,
        )
        summary.results.append(result)
        if not dry_run and not baseline:
            _track_health(config.key, result, sender)
            if result.auto_baselined:
                _notify_baseline(config, result, sender)

    summary.finished_at = now_local()

    if not dry_run:
        with session_scope() as session:
            if baseline:
                mark_bootstrapped(session)
            run_row = session.get(MonitorRun, run_id) if run_id is not None else None
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
                # Reenvio: um evento novo (não-baseline) cujo alerta falhou
                # numa execução anterior continua pendente até sair.
                pending = (
                    not resolution.is_new
                    and not resolution.event.is_baseline
                    and resolution.event.alert_sent_at is None
                )
                if resolution.should_alert or pending:
                    if pending:
                        logger.warning(
                            "company=%s action=alert_retry event_key=%s",
                            config.key,
                            event.event_key,
                        )
                    if alerts.dispatch(
                        session,
                        resolution.event,
                        event,
                        allow_resend=resolution.is_revision,
                    ):
                        result.alerts_sent += 1
            if dry_run:
                # Dry-run mostra o que aconteceria, mas não grava nada.
                session.rollback()
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



# --------------------------------------------------------------------------
# v2: limpeza única, saúde de cada empresa e aviso de linha de base
# --------------------------------------------------------------------------

def _v2_reset_once(session) -> None:
    if session.get(Meta, V2_RESET_FLAG) is not None:
        return
    rows = session.execute(
        select(Event).where(Event.company.in_(V2_RESET_COMPANIES))
    ).scalars().all()
    for row in rows:
        session.delete(row)
    session.add(Meta(key=V2_RESET_FLAG, value=now_local().isoformat()))
    logger.warning("action=v2_reset deleted_events=%d companies=%s", len(rows), V2_RESET_COMPANIES)


def _get_meta(session, key: str) -> str | None:
    row = session.get(Meta, key)
    return row.value if row is not None else None


def _set_meta(session, key: str, value: str | None) -> None:
    row = session.get(Meta, key)
    if value is None:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        session.add(Meta(key=key, value=value))
    else:
        row.value = value


def _track_health(company: str, result: CompanyRunResult, sender: EmailSender) -> None:
    """Conta falhas seguidas e avisa quando o robô fica 'cego' para uma empresa."""
    streak_key = f"fail_streak:{company}"
    notice_key = f"blind_notice:{company}"
    name = company_name(company)
    try:
        with session_scope() as session:
            streak = int(_get_meta(session, streak_key) or 0)
            notice = _get_meta(session, notice_key)
            if result.ok:
                _set_meta(session, streak_key, None)
                if notice:
                    try:
                        sender.close_notice(
                            notice,
                            f"{name} voltou a ser monitorada normalmente em "
                            f"{format_local(now_local())}. Aviso encerrado automaticamente.",
                        )
                    except EmailSendError as exc:
                        logger.error("company=%s action=notice_close_failed error=%s", company, exc)
                    _set_meta(session, notice_key, None)
                return

            streak += 1
            _set_meta(session, streak_key, str(streak))
            if streak >= _blind_after() and not notice:
                body = (
                    f"**Atenção:** o robô não está conseguindo ler os documentos de "
                    f"**{name}** há {streak} execuções seguidas.\n\n"
                    f"Enquanto este aviso estiver aberto, uma publicação nova de {name} "
                    f"**pode não gerar alerta**. Confira o site manualmente: "
                    f"{_primary_url(company)}\n\n"
                    f"Último erro: `{(result.error or '-')[:500]}`\n\n"
                    "Este aviso é encerrado sozinho quando a empresa voltar a funcionar."
                )
                try:
                    notice_id = sender.open_notice(f"⚠️ {name} sem monitoramento", body)
                    _set_meta(session, notice_key, notice_id or "aberto")
                except EmailSendError as exc:
                    logger.error("company=%s action=notice_open_failed error=%s", company, exc)
    except Exception:  # noqa: BLE001 - o controle de saúde nunca derruba a execução
        logger.exception("company=%s action=health_tracking_failed", company)


def _primary_url(company: str) -> str:
    try:
        from ..config import load_companies

        return load_companies()[company].primary_url or "-"
    except Exception:  # noqa: BLE001
        return "-"


def _notify_baseline(config: CompanyConfig, result: CompanyRunResult, sender: EmailSender) -> None:
    """Primeira leitura de uma empresa: pede conferência manual."""
    events = sorted(
        result.events,
        key=lambda e: (e.publication_date is not None, e.publication_date, e.reporting_period),
        reverse=True,
    )
    lines = [
        f"O robô leu **{config.name}** pela primeira vez e registrou "
        f"{len(events)} documento(s) já publicados como histórico. "
        "Por isso, nenhum deles gerou alerta.",
        "",
        "**Confira:** abra o site da empresa e veja se o documento mais recente "
        "abaixo é mesmo o último publicado. Se houver algum mais novo no site, "
        "ele saiu antes desta leitura e não vai gerar alerta.",
        "",
        f"Site: {config.primary_url}",
        "",
        "Documentos mais recentes encontrados:",
    ]
    for event in events[:3]:
        date_txt = event.publication_date.strftime("%d/%m/%Y") if event.publication_date else "sem data"
        lines.append(f"- {event.reporting_period} ({date_txt}): {event.document_url or event.title}")
    try:
        sender.open_notice(f"Linha de base registrada — {config.name}", "\n".join(lines))
    except EmailSendError as exc:
        logger.error("company=%s action=baseline_notice_failed error=%s", config.key, exc)


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
