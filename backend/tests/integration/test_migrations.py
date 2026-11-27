"""Migration tests. These are synchronous on purpose: Alembic's env.py runs its own event
loop, which must not be nested inside pytest-asyncio's loop."""

from __future__ import annotations

import asyncio

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from tests.conftest import alembic_config

EXPECTED_TABLES = {"users", "transcriptions", "transcription_segments"}


async def _table_names(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
    finally:
        await engine.dispose()
    return set(names) - {"alembic_version"}


def test_models_match_migrations(migrated_database: str) -> None:
    """`alembic check` fails if the models and the migrations have drifted apart."""
    command.check(alembic_config(migrated_database))


def test_downgrade_and_upgrade_round_trip(migrated_database: str) -> None:
    config = alembic_config(migrated_database)

    command.downgrade(config, "base")
    assert asyncio.run(_table_names(migrated_database)) == set()

    command.upgrade(config, "head")
    assert asyncio.run(_table_names(migrated_database)) == EXPECTED_TABLES
