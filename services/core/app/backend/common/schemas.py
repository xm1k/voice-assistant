from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Recurrence(Strict):
    frequency: Literal["daily", "weekly", "monthly"]
    interval: int = Field(default=1, ge=1, le=365)
    weekdays: list[int] = Field(
        default_factory=list, max_length=7,
        description="Дни недели: 0=понедельник, 1=вторник, 2=среда, 3=четверг, "
                    "4=пятница, 5=суббота, 6=воскресенье. По будням: [0,1,2,3,4]; по выходным: [5,6].",
    )
    until: date | None = None
    count: int | None = Field(default=None, ge=1, le=10000)

    @model_validator(mode="after")
    def weekdays_valid(self):
        if any(d < 0 or d > 6 for d in self.weekdays):
            raise ValueError("weekdays must be 0..6 (Monday..Sunday)")
        return self


class RecordData(Strict):
    kind: Literal["task", "meeting", "reminder"]
    title: str = Field(min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=5000)
    status: Literal["active", "completed", "cancelled"] = "active"
    due_date: date | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    remind_at: datetime | None = None
    timezone: str = "Europe/Moscow"
    recurrence: Recurrence | None = None
    reminder_minutes: int | None = Field(default=None, ge=0, le=525600)

    @model_validator(mode="after")
    def valid(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Invalid timezone") from exc
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("Empty title")
        for value in (self.start_at, self.end_at, self.remind_at):
            if value and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("Datetime requires timezone")
        if self.kind == "meeting" and (not self.start_at or not self.end_at or self.end_at <= self.start_at):
            raise ValueError("Meeting requires start_at < end_at")
        if self.kind == "reminder" and not self.remind_at:
            raise ValueError("Reminder requires remind_at")
        if self.kind == "task" and (self.start_at or self.end_at):
            raise ValueError("Task uses due_date")
        if self.recurrence and not (self.start_at or self.remind_at or self.due_date):
            raise ValueError("Repeating record requires a date")
        return self


class Fields(Strict):
    kind: Literal["task", "meeting", "reminder"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=5000)
    status: Literal["active", "completed", "cancelled"] | None = None
    due_date: date | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    remind_at: datetime | None = None
    timezone: str | None = None
    recurrence: Recurrence | None = None
    reminder_minutes: int | None = Field(default=None, ge=0, le=525600)


class Command(Strict):
    operation: Literal["create", "update", "delete"]
    id: str | None = None
    version: int | None = Field(default=None, ge=1)
    fields: Fields = Field(default_factory=Fields)
    scope: Literal["series", "occurrence"] = "series"
    occurrence_at: str | None = None
    clear_fields: list[
        Literal["notes", "due_date", "remind_at", "recurrence", "reminder_minutes", "start_at", "end_at"]
    ] = Field(default_factory=list, max_length=7)

    @model_validator(mode="after")
    def target(self):
        if self.operation != "create" and (not self.id or self.version is None):
            raise ValueError("Mutation requires exact id and version from query")
        if self.operation == "create" and self.scope == "occurrence":
            raise ValueError("scope=occurrence is not valid for create; omit scope or use series")
        if self.scope == "occurrence" and not self.occurrence_at:
            raise ValueError("Occurrence requires occurrence_at")
        return self


class Query(Strict):
    operation: Literal["list", "search", "count", "free_slots", "nearest", "latest"] = "list"
    kind: Literal["task", "meeting", "reminder"] | None = None
    status: Literal["active", "completed", "cancelled"] | None = None
    period_start: date | None = None
    period_end: date | None = None
    text: str | None = Field(default=None, max_length=1000)
    limit: int = Field(default=20, ge=1, le=100)
    duration_minutes: int = Field(default=60, ge=1, le=1440)
    work_start: int = Field(default=9, ge=0, le=23)
    work_end: int = Field(default=18, ge=1, le=24)

    @model_validator(mode="after")
    def period(self):
        if bool(self.period_start) != bool(self.period_end):
            raise ValueError("Both period boundaries required")
        if self.period_start and not 0 <= (self.period_end - self.period_start).days <= 366:
            raise ValueError("Period must be 0..366 days")
        if self.work_end <= self.work_start:
            raise ValueError("work_end must exceed work_start")
        return self


class RecordReference(Strict):
    id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    occurrence_at: str | None = None


class Decision(Strict):
    type: Literal["ready", "clarify", "query"]
    answer: str | None = Field(default=None, max_length=8000)
    commands: list[Command] = Field(default_factory=list, max_length=5)
    question: str | None = Field(default=None, max_length=2000)
    context: str | None = Field(default=None, max_length=8000)
    query: Query | None = None
    record_refs: list[RecordReference] = Field(default_factory=list, max_length=100)
    conflict_confirmation: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def branch(self):
        if self.type == "query" and (not self.query or self.commands or self.record_refs):
            raise ValueError("query requires query and no commands")
        if self.type == "clarify" and (not self.question or self.commands or self.query or self.record_refs):
            raise ValueError("clarify requires question and no side effects")
        if self.type == "ready" and (self.query or self.question or not (self.answer or self.commands)):
            raise ValueError("ready requires answer or commands")
        return self
