"""Email alerting.

The application talks to an ``EmailSender`` interface, never to a vendor.
A generic SMTP implementation ships in the MVP; Microsoft Graph, Resend or
anything else can be added later by implementing the same three methods.

One email per logical event. Never per document.
"""

from __future__ import annotations

import logging
import smtplib
from abc import ABC, abstractmethod
from datetime import datetime
from email.message import EmailMessage
from html import escape

from .config import EmailSettings, get_settings
from .models import HUMAN_EVENT_TYPES, NormalizedEvent
from .util import format_local, now_local

logger = logging.getLogger(__name__)


class EmailSendError(RuntimeError):
    pass


class EmailSender(ABC):
    @abstractmethod
    def send_event_alert(self, event: NormalizedEvent, detected_at: datetime) -> None:
        ...

    @abstractmethod
    def send_test_email(self) -> None:
        ...

    # -- avisos de funcionamento (não são alertas de documento) -----------
    def open_notice(self, title: str, body: str) -> str | None:
        """Abre um aviso. Devolve um identificador (ex.: número da issue)."""
        raise EmailSendError(f"{type(self).__name__} não suporta avisos")

    def close_notice(self, notice_id: str, body: str) -> None:
        """Comenta no aviso e o encerra (ex.: a empresa voltou a funcionar)."""
        raise EmailSendError(f"{type(self).__name__} não suporta avisos")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def build_subject(event: NormalizedEvent, prefix: str = "[IR Watch]") -> str:
    company = _company_label(event.company)
    event_label = HUMAN_EVENT_TYPES.get(event.event_type, event.event_type)
    return f"{prefix} {company} — {event_label} — {event.reporting_period}"


def _company_label(key: str) -> str:
    return " ".join(part.capitalize() for part in key.replace("-", "_").split("_"))


def render_plain_text(event: NormalizedEvent, detected_at: datetime) -> str:
    lines = [
        f"Company:          {_company_label(event.company)}",
        f"Event type:       {HUMAN_EVENT_TYPES.get(event.event_type, event.event_type)}",
        f"Reporting period: {event.reporting_period}",
        f"Publication date: {event.publication_date or '-'}",
        f"Detected at:      {format_local(detected_at)}",
        f"Title:            {event.title}",
        f"Source:           {event.source}",
        f"Primary link:     {event.primary_url or '-'}",
    ]
    if event.document_url and event.document_url != event.primary_url:
        lines.append(f"Document/PDF:     {event.document_url}")
    extras = event.extra_links()
    if extras:
        lines.append("")
        lines.append("Additional material:")
        lines.extend(f"  {label}: {url}" for label, url in extras)
    lines.append("")
    lines.append(f"Event key: {event.event_key}")
    return "\n".join(lines)


def render_html(event: NormalizedEvent, detected_at: datetime) -> str:
    def row(label: str, value: str | None, is_link: bool = False) -> str:
        if not value:
            value = "-"
        cell = (
            f'<a href="{escape(value)}">{escape(value)}</a>'
            if is_link and value != "-"
            else escape(str(value))
        )
        return (
            "<tr>"
            f'<td style="padding:4px 12px 4px 0;color:#555;white-space:nowrap;">{escape(label)}</td>'
            f'<td style="padding:4px 0;">{cell}</td>'
            "</tr>"
        )

    rows = [
        row("Company", _company_label(event.company)),
        row("Event type", HUMAN_EVENT_TYPES.get(event.event_type, event.event_type)),
        row("Reporting period", event.reporting_period),
        row("Publication date", str(event.publication_date) if event.publication_date else None),
        row("Detected at", format_local(detected_at)),
        row("Title", event.title),
        row("Source", event.source),
        row("Primary link", event.primary_url, is_link=True),
    ]
    if event.document_url and event.document_url != event.primary_url:
        rows.append(row("Document / PDF", event.document_url, is_link=True))

    extras = event.extra_links()
    extras_html = ""
    if extras:
        items = "".join(
            f'<li><a href="{escape(url)}">{escape(label)}</a></li>' for label, url in extras
        )
        extras_html = (
            '<p style="margin:16px 0 4px;color:#555;">Additional material</p>'
            f'<ul style="margin:0;padding-left:18px;">{items}</ul>'
        )

    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px;color:#111;">
<h2 style="margin:0 0 12px;font-size:16px;">{escape(build_subject(event, ''))}</h2>
<table cellpadding="0" cellspacing="0">{''.join(rows)}</table>
{extras_html}
<p style="margin-top:20px;color:#888;font-size:12px;">event_key: {escape(event.event_key)}</p>
</body></html>"""


# --------------------------------------------------------------------------
# Texto em português (usado nas issues do GitHub)
# --------------------------------------------------------------------------

PT_EVENT_TYPES = {
    "quarterly_results": "Demonstrações financeiras trimestrais (ITR)",
    "annual_financial_statements": "Demonstrações financeiras anuais",
}


def company_name(key: str) -> str:
    """Nome amigável da empresa, vindo do config/companies.yaml."""
    try:
        from .config import load_companies

        config = load_companies().get(key)
        if config and config.name:
            return config.name
    except Exception:  # noqa: BLE001 - nunca deixar o alerta falhar por isso
        pass
    return _company_label(key)


def build_issue_title(event: NormalizedEvent) -> str:
    label = PT_EVENT_TYPES.get(
        event.event_type, HUMAN_EVENT_TYPES.get(event.event_type, event.event_type)
    )
    return f"{company_name(event.company)} — {label} — {event.reporting_period}"


def render_issue_body(event: NormalizedEvent, detected_at: datetime) -> str:
    published = (
        event.publication_date.strftime("%d/%m/%Y") if event.publication_date else "não informada"
    )
    lines = [
        f"**Novo documento publicado por {company_name(event.company)}.**",
        "",
        f"- **Documento:** {event.title}",
        f"- **Período:** {event.reporting_period}",
        f"- **Data de publicação no site:** {published}",
        f"- **Detectado pelo robô em:** {format_local(detected_at)}",
        f"- **Link do documento:** {event.document_url or '-'}",
        f"- **Página da empresa:** {event.primary_url or '-'}",
        "",
        f"<sub>chave: `{event.event_key}`</sub>",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# SMTP implementation
# --------------------------------------------------------------------------

class SMTPEmailSender(EmailSender):
    def __init__(self, settings: EmailSettings | None = None):
        self.settings = settings or get_settings().email

    def _send(self, subject: str, text: str, html: str) -> None:
        cfg = self.settings
        if not cfg.configured:
            raise EmailSendError(
                "SMTP is not configured (need SMTP_HOST, EMAIL_FROM, EMAIL_TO)"
            )
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = cfg.email_from
        message["To"] = ", ".join(cfg.email_to)
        message.set_content(text)
        message.add_alternative(html, subtype="html")

        try:
            if cfg.smtp_port == 465:
                server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
            with server:
                server.ehlo()
                if cfg.use_tls and cfg.smtp_port != 465:
                    server.starttls()
                    server.ehlo()
                if cfg.smtp_user and cfg.smtp_password:
                    server.login(cfg.smtp_user, cfg.smtp_password)
                server.send_message(message)
        except smtplib.SMTPException as exc:
            raise EmailSendError(f"SMTP failure: {type(exc).__name__}") from exc

        logger.info(
            "action=email_sent recipients=%d subject=%r", len(cfg.email_to), subject
        )

    def send_event_alert(self, event: NormalizedEvent, detected_at: datetime) -> None:
        self._send(
            build_subject(event, self.settings.subject_prefix),
            render_plain_text(event, detected_at),
            render_html(event, detected_at),
        )

    def open_notice(self, title: str, body: str) -> str | None:
        self._send(f"{self.settings.subject_prefix} {title}", body, f"<pre>{escape(body)}</pre>")
        return title

    def close_notice(self, notice_id: str, body: str) -> None:
        self._send(
            f"{self.settings.subject_prefix} Resolvido: {notice_id}", body, f"<pre>{escape(body)}</pre>"
        )

    def send_test_email(self) -> None:
        stamp = format_local(now_local())
        self._send(
            f"{self.settings.subject_prefix} SMTP test",
            f"ir-watch SMTP test message.\nSent at {stamp}.",
            f"<p>ir-watch SMTP test message.</p><p>Sent at {escape(stamp)}.</p>",
        )


class GitHubIssueSender(EmailSender):
    """Opens a GitHub issue per event instead of sending SMTP mail.

    This exists so the project can run with ZERO external accounts: GitHub
    already emails you about issues in a repository you watch, so the alert
    still lands in your inbox without an SMTP provider, an API key, or a
    hosted database.

    Requires GITHUB_TOKEN (provided automatically by Actions) and a workflow
    with `permissions: issues: write`.
    """

    API = "https://api.github.com"

    def __init__(self, repo: str | None = None, token: str | None = None):
        import os

        self.repo = repo or os.getenv("GITHUB_REPOSITORY")
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.repo or not self.token:
            raise EmailSendError(
                "GitHubIssueSender needs GITHUB_REPOSITORY and GITHUB_TOKEN"
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _call(self, method: str, path: str, payload: dict):
        from . import http

        try:
            return http.request(
                method, f"{self.API}/repos/{self.repo}{path}", headers=self._headers(), json=payload
            )
        except Exception as exc:  # noqa: BLE001
            raise EmailSendError(f"GitHub API failure: {type(exc).__name__}") from exc

    def _post(self, title: str, body: str, labels: list[str]) -> int | None:
        response = self._call("POST", "/issues", {"title": title, "body": body, "labels": labels})
        if response.status_code in (403, 422) and labels:
            # Se o GitHub recusar as etiquetas, abre a issue sem elas:
            # o alerta é mais importante que a etiqueta.
            logger.warning(
                "action=issue_labels_rejected status=%s - retrying without labels",
                response.status_code,
            )
            response = self._call("POST", "/issues", {"title": title, "body": body})
        if response.status_code >= 300:
            raise EmailSendError(f"GitHub API returned HTTP {response.status_code}")
        logger.info("action=issue_created title=%r", title)
        try:
            return int(response.json().get("number"))
        except Exception:  # noqa: BLE001
            return None

    def send_event_alert(self, event: NormalizedEvent, detected_at: datetime) -> None:
        self._post(
            build_issue_title(event),
            render_issue_body(event, detected_at),
            ["ir-watch", event.company],
        )

    def open_notice(self, title: str, body: str) -> str | None:
        number = self._post(title, body, ["ir-watch"])
        return str(number) if number is not None else None

    def close_notice(self, notice_id: str, body: str) -> None:
        if not str(notice_id).isdigit():
            return
        response = self._call("POST", f"/issues/{notice_id}/comments", {"body": body})
        if response.status_code >= 300:
            raise EmailSendError(f"GitHub API returned HTTP {response.status_code}")
        response = self._call("PATCH", f"/issues/{notice_id}", {"state": "closed"})
        if response.status_code >= 300:
            raise EmailSendError(f"GitHub API returned HTTP {response.status_code}")

    def send_test_email(self) -> None:
        self._post(
            "IR Watch — teste do canal de alertas",
            f"Teste disparado em {format_local(now_local())}.",
            ["ir-watch"],
        )


class ConsoleEmailSender(EmailSender):
    """Used by --dry-run and by tests. Never touches the network."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.notices: list[tuple[str, str, str]] = []

    def send_event_alert(self, event: NormalizedEvent, detected_at: datetime) -> None:
        subject = build_subject(event)
        self.sent.append((subject, render_plain_text(event, detected_at)))
        logger.info("action=email_suppressed_dry_run subject=%r", subject)

    def send_test_email(self) -> None:
        self.sent.append(("[IR Watch] SMTP test", "dry-run"))
        logger.info("action=test_email_suppressed_dry_run")

    def open_notice(self, title: str, body: str) -> str | None:
        self.notices.append(("open", title, body))
        logger.info("action=notice_suppressed_dry_run title=%r", title)
        return f"console-{len(self.notices)}"

    def close_notice(self, notice_id: str, body: str) -> None:
        self.notices.append(("close", notice_id, body))
        logger.info("action=notice_close_suppressed_dry_run id=%s", notice_id)


def build_sender(dry_run: bool = False) -> EmailSender:
    """Pick the alert channel.

    ALERT_PROVIDER=smtp          -> SMTPEmailSender (default)
    ALERT_PROVIDER=github_issue  -> GitHubIssueSender (no external accounts)
    """
    import os

    if dry_run:
        return ConsoleEmailSender()
    provider = (os.getenv("ALERT_PROVIDER") or "smtp").strip().lower()
    if provider in {"github_issue", "github", "issue"}:
        return GitHubIssueSender()
    return SMTPEmailSender()
