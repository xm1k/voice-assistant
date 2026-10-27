"""Embedding table read by rag and written by core's index worker."""

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Integer, String, text

from backend.common.runtime import Base, engine


class Embedding(Base):
    __tablename__ = "record_embeddings"
    record_id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    model = Column(String, nullable=False)
    vector = Column(Vector(), nullable=False)


def migrate():
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Embedding.__table__.create(connection, checkfirst=True)
