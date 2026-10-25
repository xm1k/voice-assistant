"""Add Yandex tables without losing v2 records or breaking repeat migration."""

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


def test_additive_migration_preserves_v2_data_and_is_repeatable():
    source = Path(__file__).resolve().parents[2] / "services/auth/app/backend/migrations/main.py"
    spec = importlib.util.spec_from_file_location("yandex_migration_test", source)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO schema_version VALUES (2)"))
            connection.execute(
                text(
                    "CREATE TABLE records (id TEXT PRIMARY KEY, user_id TEXT, version INTEGER, data JSON, deleted BOOLEAN)"
                )
            )
            connection.execute(text("INSERT INTO records VALUES ('existing', 'user', 1, '{}', 0)"))
            migration.migrate_domain(connection)
            migration.migrate_domain(connection)
            assert connection.scalar(text("SELECT id FROM records")) == "existing"
            tables = set(inspect(connection).get_table_names())
            assert {
                "yandex_accounts",
                "yandex_sync",
                "yandex_sync_operations",
                "yandex_connections",
                "yandex_mappings",
                "yandex_operation_journal",
            } <= tables
            assert connection.scalar(text("SELECT max(version) FROM schema_version")) == 2
    finally:
        engine.dispose()
