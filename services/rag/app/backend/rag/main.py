
"""Scoped hybrid retrieval with versioned E5 embeddings."""

import asyncio
import os

import httpx
from fastapi import Depends, FastAPI
from pgvector.sqlalchemy import Vector
from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, Boolean, Column, Integer, String, select

from backend.common.config import configuration
from backend.common.errors import install_validation_handler
from backend.common.runtime import Base, Session
from backend.common.security import internal

app = FastAPI()
install_validation_handler(app)
_embedder = None
_embed_lock = asyncio.Lock()


class Record(Base):
    """Read-only view of core's records; core owns writes and indexing."""
    __tablename__ = "records"
    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    data = Column(JSON, nullable=False)
    deleted = Column(Boolean, nullable=False)


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


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    timezone: str
    query: dict


@app.post("/query", dependencies=[Depends(internal)])
async def query(body: QueryRequest):
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            os.getenv("CORE_URL", "http://chat-calendar-task-notification:8000") + "/internal/query",
            headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
            json=body.model_dump(),
        )
        if response.status_code == 422:
            # Missing periods and domain query validation are information for the
            # next model turn, so it can ask the user rather than fail the job.
            return {"items": [], "error": response.json().get("detail", "Invalid query parameters")}
        response.raise_for_status()
        result = response.json()
    if body.query.get("operation") != "search":
        return result
    # Core expands and filters the complete pool before retrieval; no first-page truncation.
    async with httpx.AsyncClient(timeout=60) as client:
        candidate_response = await client.post(
            os.getenv("CORE_URL", "http://chat-calendar-task-notification:8000")
            + "/internal/search-candidates",
            headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
            json=body.model_dump(),
        )
        candidate_response.raise_for_status()
        candidate_items = candidate_response.json().get("items", [])
    if not candidate_items:
        return {**result, "items": []}
    if not configuration()["search"].get("enabled", True):
        result["items"] = fuse(
            candidate_items, [], body.query.get("text") or "", min(body.query.get("limit", 50), 100)
        )
        return result
    try:
        vector, model = await embed(body.query.get("text") or "", "query: ")
        with Session() as session:
            statement = (
                select(Record.id)
                .join(Embedding, Embedding.record_id == Record.id)
                .where(
                    Record.user_id == body.user_id,
                    Embedding.user_id == body.user_id,
                    Record.deleted.is_(False),
                    Embedding.version == Record.version,
                    Embedding.model == model,
                )
            )
            candidates = list({item["id"] for item in candidate_items})
            ranked = list(
                session.scalars(
                    statement.where(Record.id.in_(candidates)).order_by(
                        Embedding.vector.cosine_distance(vector)
                    )
                ).all()
            )
    except Exception:
        ranked = []
        result.setdefault("warnings", []).append("Semantic search is unavailable; results use text matching.")
    result["items"] = fuse(
        candidate_items, ranked, body.query.get("text") or "", min(body.query.get("limit", 50), 100)
    )
    return result


def fuse(items, semantic_ids, query_text, limit):
    """Fuse lexical and cosine record ranks while preserving expanded occurrences."""
    terms = query_text.casefold().split()
    lexical_scores = {}
    for item in items:
        haystack = (item.get("title", "") + " " + (item.get("notes") or "")).casefold()
        score = sum(haystack.count(term) for term in terms)
        lexical_scores[item["id"]] = max(score, lexical_scores.get(item["id"], 0))
    lexical_ids = sorted(
        (key for key, value in lexical_scores.items() if value), key=lexical_scores.get, reverse=True
    )
    scores = {}
    for ranking in (lexical_ids, semantic_ids):
        for index, key in enumerate(ranking, 1):
            scores[key] = scores.get(key, 0) + 1 / (60 + index)
    return sorted(
        (item for item in items if item["id"] in scores), key=lambda item: scores[item["id"]], reverse=True
    )[:limit]
