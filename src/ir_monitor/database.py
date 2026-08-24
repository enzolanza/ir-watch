"""Persistence layer (SQLAlchemy 2.x).

SQLite for local development, PostgreSQL in production via DATABASE_URL.
The schema is intentionally small: the system only needs to answer
"have I already alerted on this logical event?".
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import get_settings
from .util import now_local


class Base(DeclarativeBase):
    pass


class Event(Base):
    """One logical disclosure. Exactly one alert per row, ever."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    reporting_period: Mapped[str] = mapped_column(String(32))
    event_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    title: Mapped[str | None] = mapped_column(Text)
    publication_date: Mapped[date | None] = mapped_column(Date)
    primary_url: Mapped[str | None] = mapped_column(Text)
    document_url: Mapped[str | None] = mapped_column(Text)

    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    alert_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[str | None] = mapped_column(Text)

    observations: Mapped[list["SourceObservation"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_events_company_period", "company", "reporting_period"),
    )

    @property
    def metadata_dict(self) -> dict[str, Any]:
        if not self.metadata_json:
            return {}
        try:
            return json.loads(self.metadata_json)
        except json.JSONDecodeError:  # pragma: no cover - defensive
            return {}


class SourceObservation(Base):
    """One (source, document) sighting that maps onto an Event."""

    __tablename__ = "source_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    source_name: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)
    document_identifier: Mapped[str] = mapped_column(String(255))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[str | None] = mapped_column(Text)

    event: Mapped[Event] = relationship(back_populates="observations")

    __table_args__ = (
        UniqueConstraint(
            "event_id", "source_name", "document_identifier", name="uq_observation"
        ),
    )


class MonitorRun(Base):
    __tablename__ = "monitor_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running")
    mode: Mapped[str] = mapped_column(String(32), default="check")
    companies_checked: Mapped[int] = mapped_column(Integer, default=0)
    events_found: Mapped[int] = mapped_column(Integer, default=0)
    new_events: Mapped[int] = mapped_column(Integer, default=0)
    alerts_sent: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[str | None] = mapped_column(Text)


class Meta(Base):
    """Tiny key/value table. Used to record that bootstrap has happened."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


BOOTSTRAP_KEY = "bootstrap_completed_at"


_engine = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs.pop("pool_pre_ping")
        _engine = create_engine(url, **kwargs)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory


def reset_engine() -> None:
    """Used by tests when DATABASE_URL changes."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


def init_db() -> None:
    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------
# Bootstrap bookkeeping
# --------------------------------------------------------------------------

def is_bootstrapped(session: Session) -> bool:
    row = session.get(Meta, BOOTSTRAP_KEY)
    return row is not None


def mark_bootstrapped(session: Session) -> None:
    row = session.get(Meta, BOOTSTRAP_KEY)
    stamp = now_local().isoformat()
    if row is None:
        session.add(Meta(key=BOOTSTRAP_KEY, value=stamp))
    else:
        row.value = stamp


def company_has_baseline(session: Session, company: str) -> bool:
    stmt = select(Event.id).where(Event.company == company).limit(1)
    return session.execute(stmt).first() is not None
