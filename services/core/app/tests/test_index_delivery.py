"""Index persistence/version guards without a model download or pgvector server."""

import asyncio
from unittest.mock import AsyncMock, patch

from pgvector.sqlalchemy import Vector
from sqlalchemy import create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend.chat.models import Record
from backend.workers import index as rag


@compiles(Vector, "sqlite")
def sqlite_vector_type(element, compiler, **kw):
    # Vector bind/result processors still serialize vectors; similarity SQL is not under test.
    return "TEXT"


def test_index_repeated_and_stale_versions():
    engine = create_engine("sqlite://")
    Record.__table__.create(engine)
    rag.Embedding.__table__.create(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        db.add(Record(id="record", user_id="user", version=2, data={"title": "new"}))

    async def run():
        with (
            patch.object(rag, "configuration", return_value={"search": {"enabled": True}}),
            patch.object(rag, "Session", sessions),
            patch.object(rag, "embed", AsyncMock(return_value=([1.0, 0.0], "test"))) as embed,
        ):
            await rag.handle_index({"record_id": "record", "version": 2})
            await rag.handle_index({"record_id": "record", "version": 2})
            await rag.handle_index({"record_id": "record", "version": 1})
            assert embed.await_count == 2

            async def stale_embedding(*_):
                with sessions.begin() as db:
                    db.get(Record, "record").version = 3
                    db.get(rag.Embedding, "record").version = 3
                return [0.0, 1.0], "stale"

            embed.side_effect = stale_embedding
            await rag.handle_index({"record_id": "record", "version": 2})

    asyncio.run(run())
    with sessions() as db:
        rows = db.scalars(select(rag.Embedding)).all()
        assert len(rows) == 1 and rows[0].version == 3 and rows[0].model == "test"
    engine.dispose()
