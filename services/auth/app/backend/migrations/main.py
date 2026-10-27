"""Versioned, additive migrations for the rebuilt domain database."""

from backend.common.runtime import Base, engine
from sqlalchemy import inspect, text


def migrate_domain(connection):
    from backend.migrations.schema import domain, yandex_calendar  # noqa: F401
    from backend.migrations.schema.rag import Embedding

    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT pg_advisory_xact_lock(2036102101)"))
    connection.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"))
    version = connection.scalar(text("SELECT max(version) FROM schema_version"))
    if version not in (None, 1, 2):
        raise RuntimeError("Unsupported database schema version")
    Base.metadata.create_all(
        connection,
        tables=[table for table in Base.metadata.sorted_tables if table is not Embedding.__table__],
    )
    # No backfill: the true creation time of existing records is unknown.
    columns = {c["name"] for c in inspect(connection).get_columns("records")}
    if "created_at" not in columns:
        connection.execute(text("ALTER TABLE records ADD COLUMN created_at TIMESTAMP WITH TIME ZONE"))
    columns = {c["name"] for c in inspect(connection).get_columns("messages")}
    if "cards" not in columns:
        connection.execute(text("ALTER TABLE messages ADD COLUMN cards JSON"))
    # Integration tables are additive; older releases can still read schema v2.
    if version != 2:
        connection.execute(text("INSERT INTO schema_version(version) VALUES (2)"))


def migrate():
    from backend.auth.main import migrate as auth_migrate
    from backend.migrations.schema.rag import migrate as rag_migrate

    with engine.begin() as connection:
        migrate_domain(connection)
    rag_migrate()
    auth_migrate()


if __name__ == "__main__":
    migrate()
