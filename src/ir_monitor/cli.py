"""Command line interface."""

from __future__ import annotations

import argparse
import sys

from .config import load_companies
from .database import init_db
from .emailer import EmailSendError, build_sender
from .logging_config import configure_logging
from .models import HUMAN_EVENT_TYPES, RunSummary
from .services.runner import BootstrapRequired, inspect, run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ir_monitor",
        description="Monitor periodic financial/operational disclosures of a "
        "fitness peer watch list and email an alert as soon as one is published.",
    )
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--log-format", default=None, choices=["text", "json"])
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser(
        "bootstrap",
        help="Record every currently published disclosure as baseline. Sends no email.",
    )
    bootstrap.add_argument("--company", action="append", dest="companies")

    check = sub.add_parser("check", help="Detect new disclosures and send alerts.")
    check.add_argument("--company", action="append", dest="companies")
    check.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen. No emails, no database writes.",
    )
    check.add_argument(
        "--allow-missing-bootstrap",
        action="store_true",
        help="Run against an un-bootstrapped database (may alert the full history).",
    )

    inspect_cmd = sub.add_parser(
        "inspect", help="Show extracted candidates and their classification."
    )
    inspect_cmd.add_argument("--company", required=True)
    inspect_cmd.add_argument("--verbose", action="store_true")

    sub.add_parser("send-test-email", help="Validate the SMTP configuration.")
    sub.add_parser("list-companies", help="Show the configured watch list.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level, args.log_format)

    if args.command == "list-companies":
        return _cmd_list_companies()
    if args.command == "send-test-email":
        return _cmd_send_test_email()
    if args.command == "inspect":
        return _cmd_inspect(args.company, args.verbose)

    init_db()

    if args.command == "bootstrap":
        summary = run(mode="bootstrap", only=args.companies)
        _print_summary(summary, "bootstrap")
        print(
            "\nBaseline recorded. No emails were sent. "
            "Run `python -m ir_monitor check` from now on."
        )
        return 1 if summary.errors else 0

    if args.command == "check":
        try:
            summary = run(
                mode="check",
                only=args.companies,
                dry_run=args.dry_run,
                allow_missing_bootstrap=args.allow_missing_bootstrap,
            )
        except BootstrapRequired as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        _print_summary(summary, "check (dry-run)" if args.dry_run else "check")
        return 1 if summary.errors else 0

    return 0


# --------------------------------------------------------------------------
def _cmd_list_companies() -> int:
    companies = load_companies()
    width = max(len(k) for k in companies)
    for key, config in companies.items():
        status = "enabled " if config.enabled else "disabled"
        print(f"{key.ljust(width)}  {status}  {config.name}")
    return 0


def _cmd_send_test_email() -> int:
    try:
        build_sender().send_test_email()
    except EmailSendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("Test email sent.")
    return 0


def _cmd_inspect(company: str, verbose: bool) -> int:
    result = inspect(company)
    print(f"\n{company}")
    if not result.ok:
        print(f"  ERROR: {result.error}")
        return 1
    print(f"  Source used:     {result.source_used}")
    print(f"  Candidates found: {result.candidates}")
    print(f"  Relevant events:  {result.relevant}")
    for event in sorted(result.events, key=lambda e: e.reporting_period, reverse=True):
        label = HUMAN_EVENT_TYPES.get(event.event_type, event.event_type)
        print(f"    - {event.reporting_period:<14} {label:<28} {event.title[:70]}")
        if verbose:
            print(f"        key: {event.event_key}")
            print(f"        url: {event.primary_url}")
    return 0


def _print_summary(summary: RunSummary, mode: str) -> None:
    print(f"\n=== ir-watch {mode} ===")
    for result in summary.results:
        status = "ok   " if result.ok else "ERROR"
        print(f"\n{result.company}  [{status}]")
        if not result.ok:
            print(f"  {result.error}")
            continue
        print(f"  Source used:      {result.source_used}")
        print(f"  Candidates found: {result.candidates}")
        print(f"  Relevant events:  {result.relevant}")
        print(f"  New events:       {result.new_events}")
        print(f"  Alerts:           {result.alerts_sent}")
        if result.auto_baselined:
            print("  NOTE: no prior history for this company - events recorded "
                  "as baseline, no emails sent.")
        for key in result.new_event_keys:
            print(f"    + {key}")

    print("\n--- totals ---")
    print(f"Companies checked: {summary.companies_checked}")
    print(f"Successes:         {summary.companies_checked - len(summary.errors)}")
    print(f"Errors:            {len(summary.errors)}")
    print(f"Events found:      {summary.events_found}")
    print(f"New events:        {summary.new_events}")
    print(f"Alerts sent:       {summary.alerts_sent}")
    if summary.errors:
        print("\nFailed companies:")
        for result in summary.errors:
            print(f"  {result.company}: {result.error}")
