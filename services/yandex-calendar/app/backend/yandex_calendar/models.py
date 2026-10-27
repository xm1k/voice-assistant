"""Private Yandex service persistence; auth's schema migration keeps a copy."""

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.common.runtime import Base, uid


class YandexConnection(Base):
    __tablename__ = "yandex_connections"
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    generation: Mapped[str] = mapped_column(String(36), default=uid)
    status: Mapped[str] = mapped_column(String(30), default="disconnected")
    credentials: Mapped[str | None] = mapped_column(Text, nullable=True)
    calendars: Mapped[list] = mapped_column(JSON, default=list)
    calendar_id: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    calendar_name: Mapped[str | None] = mapped_column(String(500), nullable=True)


class YandexMapping(Base):
    __tablename__ = "yandex_mappings"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    generation: Mapped[str] = mapped_column(String(36))
    href: Mapped[str] = mapped_column(String(2000))
    uid: Mapped[str] = mapped_column(String(200))
    epoch: Mapped[str] = mapped_column(String(100))
    etag: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="synced")
    version: Mapped[int] = mapped_column(Integer, default=0)


class YandexOperation(Base):
    __tablename__ = "yandex_operation_journal"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    intent: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
