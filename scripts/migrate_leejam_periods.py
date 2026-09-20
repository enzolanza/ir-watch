"""One-off correction for Leejam events recorded with a wrong period key.

Context: the original leejam.py never parsed the IR Result Center archive's
filename conventions (see monitors/leejam.py's docstrings), so a handful of
events were bootstrapped under the wrong reporting_period/event_key - most
visibly "leejam|Q1-2018" for a document named "18Q3.pdf" (should be
"leejam|Q3/9M-2018"), and "leejam|Q4/FY-2026" for
"Earnings-Presentation-Q4-2025-Final.pdf" (should be "leejam|Q4/FY-2025").

Running `check` after the leejam.py fix ships, without correcting these rows
first, would make the corrected period look like a brand-new event and send
a real alert for a document that was already recorded (just under the wrong
key) - not a genuinely new disclosure. This script recomputes the correct
key for every existing Leejam event from its own stored document URL (the
same leejam_filename_period() the fixed adapter now uses) and either:

  - renames the row in place (event_key/reporting_period only - id,
    first_seen, created_at, is_baseline, alert_sent_at, metadata are
    untouched) when the corrected key does not already exist, or
  - merges it into the existing event at the corrected key when it does
    (e.g. Q4/FY-2026 merging into an already-present Q4/FY-2025): the older
    of the two events' identity (id, first_seen) is kept, every
    SourceObservation is moved onto it, and the other document's URL is
    recorded in its metadata's "alternate_documents" list, mirroring what
    event_resolver._enrich()/_record_observation() already do for a second
    sighting of the same event.

Nothing is ever deleted outright except a row that was fully merged into
another (its SourceObservations are moved first, never dropped). No other
company's rows are touched. Dry-run by default; pass --apply to write.

Usage:
    DATABASE_URL=sqlite:///state/ir_watch.db python scripts/migrate_leejam_periods.py
    DATABASE_URL=sqlite:///state/ir_watch.db python scripts/migrate_leejam_periods.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select  # noqa: E402

from ir_monitor.database import Event, SourceObservation, session_scope  # noqa: E402
from ir_monitor.monitors.leejam import leejam_filename_period  # noqa: E402


def plan(session) -> list[dict]:
    events = session.execute(
        select(Event).where(Event.company == "leejam")
    ).scalars().all()
    by_key = {e.event_key: e for e in events}

    actions = []
    for event in events:
        url = event.document_url or event.primary_url
        new_period = leejam_filename_period(url)
        if not new_period or new_period == event.reporting_period:
            continue
        new_key = f"leejam|{new_period}"
        target = by_key.get(new_key)
        if target is None or target.id == event.id:
            actions.append(
                {"kind": "rename", "event": event, "old_period": event.reporting_period,
                 "new_period": new_period, "new_key": new_key}
            )
        else:
            # Keep whichever of the two was recorded first; merge the other
            # into it, same principle as event_resolver._enrich().
            older, newer = sorted([event, target], key=lambda e: e.first_seen)
            actions.append(
                {"kind": "merge", "keep": older, "drop": newer,
                 "old_period": event.reporting_period, "new_period": new_period}
            )
    return actions


def apply_rename(action: dict) -> None:
    event = action["event"]
    event.event_key = action["new_key"]
    event.reporting_period = action["new_period"]


def apply_merge(session, action: dict) -> None:
    keep, drop = action["keep"], action["drop"]

    metadata = keep.metadata_dict
    alternates = metadata.setdefault("alternate_documents", [])
    for url in (drop.document_url, drop.primary_url):
        if url and url != keep.document_url and url not in alternates:
            alternates.append(url)
    keep.metadata_json = json.dumps(metadata, ensure_ascii=False)
    if not keep.document_url and drop.document_url:
        keep.document_url = drop.document_url
    if not keep.primary_url and drop.primary_url:
        keep.primary_url = drop.primary_url

    # Order matters for SQLite's UNIQUE(event_key): move drop's
    # observations off it and delete it *before* renaming keep to the
    # (currently still taken) target key, with a flush in between so each
    # step actually lands before the next one runs.
    for obs in session.execute(
        select(SourceObservation).where(SourceObservation.event_id == drop.id)
    ).scalars():
        obs.event_id = keep.id
    session.flush()

    session.delete(drop)
    session.flush()

    keep.event_key = f"leejam|{action['new_period']}"
    keep.reporting_period = action["new_period"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    args = parser.parse_args()

    with session_scope() as session:
        actions = plan(session)
        if not actions:
            print("No Leejam events need a period/key correction.")
            return 0

        for action in actions:
            if action["kind"] == "rename":
                event = action["event"]
                print(
                    f"RENAME  {event.event_key}  ->  leejam|{action['new_period']}"
                    f"  ({event.document_url or event.primary_url})"
                )
            else:
                keep, drop = action["keep"], action["drop"]
                print(
                    f"MERGE   {drop.event_key}  into  {keep.event_key}"
                    f"  ({drop.document_url or drop.primary_url})"
                )

        if not args.apply:
            print(f"\n{len(actions)} change(s) planned. Re-run with --apply to write them.")
            return 0

        for action in actions:
            if action["kind"] == "rename":
                apply_rename(action)
            else:
                apply_merge(session, action)
            session.flush()

        print(f"\nApplied {len(actions)} change(s).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
