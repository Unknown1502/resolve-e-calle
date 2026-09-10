"""Test fixtures.

Integration tests run against the real PostgreSQL from
``docker compose up postgres``, not against SQLite. The schema uses
JSONB and ``FOR UPDATE SKIP LOCKED``; testing those on a different
engine would prove nothing about production behaviour.

If Postgres is unreachable the integration tests skip rather than fail,
so ``pytest`` still runs the (much larger) pure policy and adapter
suites on a machine with no Docker.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

#: A SEPARATE database from the one the demo runs on.
#:
#: The session fixture drops every table when it finishes. Pointing that
#: at the application database means running the test suite silently
#: destroys a running demo -- which is exactly what a judge would do
#: after `docker compose up`. So tests get their own database, created
#: on demand.
ADMIN_DATABASE_URL = os.environ.get(
    "ADMIN_DATABASE_URL",
    "postgresql+psycopg://resolvee:resolvee@localhost:55432/resolvee",
)
TEST_DATABASE_NAME = os.environ.get("TEST_DATABASE_NAME", "resolvee_test")
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    ADMIN_DATABASE_URL.rsplit("/", 1)[0] + "/" + TEST_DATABASE_NAME,
)


def _ensure_test_database() -> bool:
    """Create the test database if it does not exist yet."""
    try:
        admin = create_engine(
            ADMIN_DATABASE_URL,
            connect_args={"connect_timeout": 3},
            isolation_level="AUTOCOMMIT",
        )
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DATABASE_NAME},
            ).scalar()
            if not exists:
                # Identifier cannot be parameterised; the name comes from
                # our own configuration, not from user input.
                conn.execute(text(f'CREATE DATABASE "{TEST_DATABASE_NAME}"'))
        admin.dispose()
    except Exception:
        return False
    return True


def _postgres_available() -> bool:
    if not _ensure_test_database():
        return False
    try:
        engine = create_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


POSTGRES_UP = _postgres_available()

requires_postgres = pytest.mark.skipif(
    not POSTGRES_UP,
    reason=(
        "PostgreSQL is not reachable. Start it with: "
        "docker compose up -d postgres redis"
    ),
)


@pytest.fixture(scope="session")
def engine():
    if not POSTGRES_UP:
        pytest.skip("PostgreSQL unavailable")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

    from app.config import get_settings

    get_settings.cache_clear()

    from app.db.models import Base
    from app.db.session import get_engine

    eng = get_engine()
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def db(engine) -> Iterator[Session]:
    """A clean database per test.

    Truncating rather than rolling back a transaction, because the code
    under test commits deliberately -- the outbox pattern and the
    dispatch path both depend on committing before a side effect.
    """
    from app.db.models import Base

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def fresh_mock_provider():
    """Reset the process-wide mock between tests."""
    from app.services.provider import reset_mock_provider

    reset_mock_provider()
    yield
    reset_mock_provider()

@pytest.fixture(autouse=True, scope="session")
def _never_place_a_real_call() -> Iterator[None]:
    """Neutralise live calling for the whole test session.

    Two reasons, and the second is the important one.

    1. **Determinism.** ``get_settings()`` reads ``.env``, so without
       this a developer's local configuration leaks into assertions and
       tests pass or fail depending on whose machine they run on.

    2. **Safety.** A developer with ``CALLE_LIVE_CALLS=true`` in ``.env``
       -- which is exactly the state you are in right after a smoke test
       -- would otherwise have a test suite capable of dialling real
       phone numbers. Running the tests must never be able to ring
       anybody.
    """
    import os

    from app.config import get_settings

    previous = {
        key: os.environ.get(key)
        for key in ("CALLE_LIVE_CALLS", "CALLE_API_KEY", "CALLE_ALLOWED_NUMBERS")
    }
    os.environ["CALLE_LIVE_CALLS"] = "false"
    os.environ["CALLE_API_KEY"] = ""
    os.environ["CALLE_ALLOWED_NUMBERS"] = ""
    get_settings.cache_clear()

    assert not get_settings().live_calls_enabled, "tests must never be able to call"

    yield

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()
