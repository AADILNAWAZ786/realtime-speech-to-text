"""Unit tests must not need PostgreSQL.

Collection fails if a test under tests/unit/ requests any fixture that (directly or
indirectly) connects to the database. Database-backed tests belong in tests/integration/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

UNIT_DIR = Path(__file__).resolve().parent

DATABASE_FIXTURES = frozenset(
    {
        "test_database_url",
        "migrated_database",
        "make_settings",
        "settings",
        "app",
        "client",
        "session_factory",
        "register_user",
        "login",
        "auth_headers_for",
        "auth_headers",
    }
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    offenders = []
    for item in items:
        if UNIT_DIR not in Path(item.path).resolve().parents:
            continue  # this hook sees the whole session; only police tests/unit/
        used = DATABASE_FIXTURES.intersection(getattr(item, "fixturenames", ()))
        if used:
            offenders.append(f"{item.nodeid} uses {sorted(used)}")
    if offenders:
        raise pytest.UsageError(
            "Unit tests must not use database fixtures (move them to tests/integration/):\n  "
            + "\n  ".join(offenders)
        )
