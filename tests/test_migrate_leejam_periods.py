"""scripts/migrate_leejam_periods.py against a throwaway database.

Built from the real production events dumped from the pre-fix bootstrap
run: a plain rename (Q1-2018 -> Q3/9M-2018, no collision) and a merge
(Q4/FY-2026 -> Q4/FY-2025, which already existed as a separate event).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from ir_monitor import config as config_module
from ir_monitor import database as db_module
from ir_monitor.database import Event, SourceObservation

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_leejam_periods.py"


@pytest.fixture()
def migrate_module(monkeypatch):
    spec = importlib.util.spec_from_file_location("migrate_leejam_periods", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_leejam_periods"] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules["migrate_leejam_periods"]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'test.db'}")
    config_module.reset_caches()
    db_module.reset_engine()
    db_module.init_db()
    yield
    db_module.reset_engine()
    config_module.reset_caches()


def _add_event(session, *, key, period, url, first_seen):
    event = Event(
        company="leejam",
        event_type="quarterly_results",
        reporting_period=period,
        event_key=key,
        title=f"Leejam {period} Results",
        primary_url=url,
        document_url=url,
        is_baseline=True,
        first_seen=first_seen,
        created_at=first_seen,
        updated_at=first_seen,
        metadata_json='{"issuer": "Leejam Sports Company", "ticker": "1830"}',
    )
    session.add(event)
    session.flush()
    session.add(
        SourceObservation(
            event_id=event.id,
            source_name="leejam_result_center",
            source_url=url,
            document_identifier=url,
            first_seen=first_seen,
            last_seen=first_seen,
        )
    )
    return event


class TestMigrateLeejamPeriods:
    def test_plain_rename_no_collision(self, db, migrate_module):
        with db_module.session_scope() as session:
            _add_event(
                session, key="leejam|Q1-2018", period="Q1-2018",
                url="https://leejam.com.sa/wp-content/uploads/2023/05/18Q3.pdf",
                first_seen=datetime(2026, 9, 20, tzinfo=timezone.utc),
            )

        with db_module.session_scope() as session:
            actions = migrate_module.plan(session)
            assert len(actions) == 1
            assert actions[0]["kind"] == "rename"
            for action in actions:
                migrate_module.apply_rename(action)

        with db_module.session_scope() as session:
            events = session.execute(
                select(Event).where(Event.company == "leejam")
            ).scalars().all()
            assert len(events) == 1
            assert events[0].event_key == "leejam|Q3/9M-2018"
            assert events[0].reporting_period == "Q3/9M-2018"
            assert events[0].alert_sent_at is None
            assert events[0].is_baseline is True

    def test_merge_into_existing_event_preserves_both_observations(self, db, migrate_module):
        with db_module.session_scope() as session:
            # The already-correct event, seen first.
            _add_event(
                session, key="leejam|Q4/FY-2025", period="Q4/FY-2025",
                url="https://leejam.com.sa/wp-content/uploads/2026/05/"
                    "LEEJAM-2025-English-156pp-27-March-compressed.pdf",
                first_seen=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
            )
            # The mis-keyed one, seen slightly later.
            _add_event(
                session, key="leejam|Q4/FY-2026", period="Q4/FY-2026",
                url="https://leejam.com.sa/wp-content/uploads/2026/02/"
                    "Earnings-Presentation-Q4-2025-Final.pdf",
                first_seen=datetime(2026, 9, 20, 10, 0, 1, tzinfo=timezone.utc),
            )

        with db_module.session_scope() as session:
            actions = migrate_module.plan(session)
            assert len(actions) == 1
            assert actions[0]["kind"] == "merge"
            for action in actions:
                migrate_module.apply_merge(session, action)
                session.flush()

        with db_module.session_scope() as session:
            events = session.execute(
                select(Event).where(Event.company == "leejam")
            ).scalars().all()
            # One event survives, under the corrected key.
            assert len(events) == 1
            assert events[0].event_key == "leejam|Q4/FY-2025"
            assert events[0].reporting_period == "Q4/FY-2025"
            assert events[0].alert_sent_at is None
            assert events[0].is_baseline is True

            # Both documents are still tracked - nothing was dropped.
            obs = session.execute(
                select(SourceObservation).where(SourceObservation.event_id == events[0].id)
            ).scalars().all()
            assert len(obs) == 2
            urls = {o.document_identifier for o in obs}
            assert "Earnings-Presentation-Q4-2025-Final.pdf" in "".join(urls)
            assert "LEEJAM-2025-English" in "".join(urls)

            alternates = events[0].metadata_dict.get("alternate_documents", [])
            assert any("Earnings-Presentation-Q4-2025-Final.pdf" in u for u in alternates)

    def test_idempotent_second_run_finds_nothing_to_do(self, db, migrate_module):
        with db_module.session_scope() as session:
            _add_event(
                session, key="leejam|Q1-2018", period="Q1-2018",
                url="https://leejam.com.sa/wp-content/uploads/2023/05/18Q3.pdf",
                first_seen=datetime(2026, 9, 20, tzinfo=timezone.utc),
            )
        with db_module.session_scope() as session:
            for action in migrate_module.plan(session):
                migrate_module.apply_rename(action)

        with db_module.session_scope() as session:
            assert migrate_module.plan(session) == []

    def test_other_companies_are_never_touched(self, db, migrate_module):
        with db_module.session_scope() as session:
            _add_event(
                session, key="leejam|Q1-2018", period="Q1-2018",
                url="https://leejam.com.sa/wp-content/uploads/2023/05/18Q3.pdf",
                first_seen=datetime(2026, 9, 20, tzinfo=timezone.utc),
            )
            other = Event(
                company="sats",
                event_type="quarterly_results",
                reporting_period="Q1-2018",
                event_key="sats|Q1-2018",
                title="SATS Q1-2018 Results",
                primary_url="https://satsgroup.com/q1-2018.pdf",
                is_baseline=True,
                first_seen=datetime(2026, 9, 20, tzinfo=timezone.utc),
                created_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
                updated_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
            )
            session.add(other)

        with db_module.session_scope() as session:
            for action in migrate_module.plan(session):
                migrate_module.apply_rename(action)

        with db_module.session_scope() as session:
            sats_event = session.execute(
                select(Event).where(Event.company == "sats")
            ).scalar_one()
            assert sats_event.event_key == "sats|Q1-2018"
