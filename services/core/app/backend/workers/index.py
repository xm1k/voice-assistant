"""Index worker: consumes RAG indexing jobs and stores versioned E5 embeddings."""

import asyncio
import logging

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Integer, String, delete, select

from backend.chat.models import Record
from backend.common.config import configuration
from backend.common.runtime import Base, Session

logger = logging.getLogger(__name__)
_embedder = None
_embed_lock = asyncio.Lock()


class Embedding(Base):
    __tablename__ = "record_embeddings"
    record_id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    model = Column(String, nullable=False)
    vector = Column(Vector(), nullable=False)


async def embed(value, prefix):
    global _embedder
    async with _embed_lock:
        model = configuration()["search"]["model"]
        if _embedder is None or _embedder[0] != model:
            from sentence_transformers import SentenceTransformer

            _embedder = (model, await asyncio.to_thread(SentenceTransformer, model))
        vector = await asyncio.to_thread(_embedder[1].encode, prefix + value, normalize_embeddings=True)
        return vector.tolist(), model


async def handle_index(payload):
    if not configuration()["search"].get("enabled", True):
        return
    record_id = payload["record_id"]
    with Session() as session:
        record = session.get(Record, record_id)
        if record is None or record.deleted:
            session.execute(delete(Embedding).where(Embedding.record_id == record_id))
            session.commit()
            return
        version, user_id, data = record.version, record.user_id, record.data
        if version != payload["version"]:
            return
    import json

    vector, model = await embed(json.dumps(data, ensure_ascii=False), "passage: ")
    with Session() as session:
        # Lock the record and recheck after slow embedding work: stale workers cannot overwrite new vectors.
        record = session.scalar(select(Record).where(Record.id == record_id).with_for_update())
        if record is None or record.deleted or record.version != version:
            return
        previous = session.get(Embedding, record_id)
        if previous is None:
            previous = Embedding(record_id=record_id)
            session.add(previous)
        previous.user_id, previous.version, previous.model, previous.vector = user_id, version, model, vector
        session.commit()


async def main():
    logger.info("Index worker starting queue=index")
    from backend.chat.broker import consume

    await consume("index", handle_index)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
