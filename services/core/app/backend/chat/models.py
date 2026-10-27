from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.common.runtime import Base, now, uid


class Record(Base):
    __tablename__ = "records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=now)


class ExceptionRecord(Base):
    __tablename__ = "record_exceptions"
    __table_args__ = (UniqueConstraint("record_id", "occurrence_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    record_id: Mapped[str] = mapped_column(String(36), index=True)
    occurrence_at: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON)


class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    pending: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(String(8000))
    cards: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("user_id", "kind", "request_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    generation: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    queue: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (UniqueConstraint("user_id", "key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36))
    key: Mapped[str] = mapped_column(String(100))
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("user_id", "record_id", "occurrence_at", "due_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    record_id: Mapped[str] = mapped_column(String(36))
    occurrence_at: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(500))
    due_at: Mapped[str] = mapped_column(String(64))
    read_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Setting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(2000))


class YandexAccount(Base):
    """Core's connection fence; credentials belong to the Yandex service."""
    __tablename__ = "yandex_accounts"
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    generation: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30))


class YandexSync(Base):
    __tablename__ = "yandex_sync"
    record_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    generation: Mapped[str] = mapped_column(String(36))
    title: Mapped[str] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer)
    synced_version: Mapped[int] = mapped_column(Integer, default=0)
    operation_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    code: Mapped[str | None] = mapped_column(String(50), nullable=True)


class YandexSyncOperation(Base):
    __tablename__ = "yandex_sync_operations"
    __table_args__ = (UniqueConstraint("record_id", "generation", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    record_id: Mapped[str] = mapped_column(String(36), index=True)
    generation: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    predecessor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    code: Mapped[str | None] = mapped_column(String(50), nullable=True)
