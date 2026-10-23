"""Private Calendar service persistence; credentials never enter core tables."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.common.runtime import Base, uid


class GoogleConnection(Base):
    __tablename__ = "google_connections"
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    generation: Mapped[str] = mapped_column(String(36), default=uid)
    status: Mapped[str] = mapped_column(String(30), default="disconnected")
    tokens: Mapped[str | None] = mapped_column(Text, nullable=True)
    calendar_id: Mapped[str | None] = mapped_column(String(500), nullable=True)


class GoogleOAuthState(Base):
    __tablename__ = "google_oauth_states"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36))
    session_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GoogleMapping(Base):
    __tablename__ = "google_event_mappings"
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    generation: Mapped[str] = mapped_column(String(36))
    event_id: Mapped[str] = mapped_column(String(200))
    etag: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="synced")
    detached: Mapped[bool] = mapped_column(Boolean, default=False)
    source_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)


class GoogleOperation(Base):
    __tablename__ = "google_operation_journal"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    intent: Mapped[dict] = mapped_column(JSON, default=dict)
