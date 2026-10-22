"""Public response contracts, shared by HTTP schemas and event payloads."""

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

from backend.common.schemas import RecordData

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None


class Conflict(BaseModel):
    id: str
    title: str
    occurrence_at: str | None = None


class RecordOutput(RecordData):
    id: str
    version: int
    occurrence_at: str | None = None
    conflicts: list[Conflict] = Field(default_factory=list)
    created_at: datetime | None = None


class RecordCard(BaseModel):
    action: Literal["created", "updated", "deleted", "found"]
    record: RecordOutput


class MessageOutput(BaseModel):
    id: str
    role: str
    text: str
    created_at: str
    cards: list[RecordCard] = Field(default_factory=list)


class JobOutput(BaseModel):
    id: str
    status: str
    result: dict | None
    error: str | None


class JobAccepted(BaseModel):
    job_id: str
    status: str


class ChatOutput(BaseModel):
    id: str
    pending: bool
    busy: bool


class NotificationOutput(BaseModel):
    id: str
    record_id: str
    occurrence_at: str
    title: str
    due_at: str
    read_at: str | None
    read: bool
