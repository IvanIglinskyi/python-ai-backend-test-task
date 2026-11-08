from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(128), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Ticket(Base):
    __tablename__ = "tickets"

    ticket_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    customer_email: Mapped[str] = mapped_column(String(320))
    message: Mapped[str] = mapped_column(Text)
    reply_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Job(Base):
    """One durable unit of work per webhook event. Duplicate event_id does not enqueue twice."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "available_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(64))
    ticket_id: Mapped[str] = mapped_column(String(128), index=True)
    # queued -> running -> done | failed. A running job whose lease
    # (available_at) expired is claimed again, so a crashed worker loses nothing.
    status: Mapped[str] = mapped_column(String(32), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.ticket_id"), index=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True)
    body: Mapped[str] = mapped_column(Text)
    article_ids: Mapped[list] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="pending_review")
    source: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Delivery(Base):
    """Exactly one row per approved draft. Retries reuse the row and its idempotency key."""

    __tablename__ = "deliveries"
    __table_args__ = (Index("ix_deliveries_claim", "state", "available_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"), unique=True)
    ticket_id: Mapped[str] = mapped_column(String(128), index=True)
    reply_url: Mapped[str] = mapped_column(Text)
    # Frozen text that goes to the customer; retries never re-read the draft.
    message: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    # Next attempt time for `pending`, lease expiry for `in_flight`.
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerKick(Base):
    """Single row: when a serverless tick was last triggered (debounces kicks)."""

    __tablename__ = "worker_kicks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kicked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LlmPause(Base):
    """Single row: no provider calls until `until` (set when the provider answers 429)."""

    __tablename__ = "llm_pause"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LlmCall(Base):
    """One row per provider call; the sliding window behind the 60/min limit."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class MockReply(Base):
    __tablename__ = "mock_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(128), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
