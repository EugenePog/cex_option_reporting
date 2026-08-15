"""SQLAlchemy engine, session factory, and declarative Base — shared by all processes."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.settings import get_settings


class Base(DeclarativeBase):
    """Base class for all ORM models."""


_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    future=True,
    pool_pre_ping=True,   # transparently recover dropped connections
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context: commit on success, rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    """Create every table registered on Base.metadata (dev convenience; prefer Alembic).

    Imports models so they register on the metadata before create_all runs.
    """
    import app.db.models  # noqa: F401  (registers tables)

    # Ensure the medallion schemas exist first.
    from sqlalchemy import text

    with engine.begin() as conn:
        for schema in ("bronze", "silver", "gold", "core"):
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    Base.metadata.create_all(engine)
