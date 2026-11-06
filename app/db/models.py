"""SQLAlchemy 2.0 models.

Only public clinic data is stored. There are no patient fields by design.
All datetimes are naive UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC 'now' (SQLite drops tzinfo, so we keep everything naive and consistent)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


CLINIC_STATUSES = (
    "new",
    "ready",
    "approved",
    "sent",
    "replied",
    "negotiating",
    "prepaid",
    "launched",
    "opt_out",
    "skipped",
)
MESSAGE_STATUSES = ("draft", "approved", "rejected", "sent")
MESSAGE_KINDS = ("outreach", "reply")
PAYMENT_KINDS = ("prepayment", "launch", "subscription")


class Base(DeclarativeBase):
    pass


class Clinic(Base):
    __tablename__ = "clinics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    direction: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Public contact only: phone number, @username, t.me or Instagram URL.
    public_contact: Mapped[str | None] = mapped_column(String(300), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    messages: Mapped[list[Message]] = relationship(back_populates="clinic", cascade="all, delete-orphan")
    events: Mapped[list[Event]] = relationship(back_populates="clinic")
    payments: Mapped[list[Payment]] = relationship(back_populates="clinic")

    __table_args__ = (Index("ix_clinics_name_lower", "name"), Index("ix_clinics_status", "status"))


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False)
    # outreach = first message; reply = answer to a clinic's reply
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="outreach")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    lang: Mapped[str] = mapped_column(String(4), nullable=False, default="ru")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    clinic: Mapped[Clinic] = relationship(back_populates="messages")

    __table_args__ = (Index("ix_messages_status", "status"), Index("ix_messages_approved_at", "approved_at"))


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clinic_id: Mapped[int | None] = mapped_column(ForeignKey("clinics.id", ondelete="SET NULL"), nullable=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    clinic: Mapped[Clinic | None] = relationship(back_populates="events")

    __table_args__ = (Index("ix_events_type", "type"),)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    clinic: Mapped[Clinic] = relationship(back_populates="payments")
