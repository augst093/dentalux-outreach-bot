"""Clinic tracker: statuses, drafts, approvals, replies, payments and the audit log.

Rules:
- Every status change is validated against ALLOWED_TRANSITIONS and written to `events`.
- Nothing here talks to Telegram or to clinics. It only changes the database.
- Functions do not commit; the caller (Operator / DB middleware) owns the transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CLINIC_STATUSES, PAYMENT_KINDS, Clinic, Event, Message, Payment


class TrackerError(Exception):
    """User-facing error. The message is Russian text shown to the owner."""


class InvalidTransition(TrackerError):
    pass


class OptedOutError(TrackerError):
    pass


STATUS_RU = {
    "new": "новая",
    "ready": "готово к отправке",
    "approved": "одобрено, ждёт отправки",
    "sent": "отправлено",
    "replied": "ответила",
    "negotiating": "переговоры",
    "prepaid": "предоплата получена",
    "launched": "запущено",
    "opt_out": "просила не писать (opt_out)",
    "skipped": "пропущено",
}

# Allowed status changes. opt_out is reachable from every live status and is terminal.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"ready", "skipped", "opt_out"}),
    "ready": frozenset({"approved", "skipped", "opt_out"}),
    "approved": frozenset({"sent", "opt_out"}),
    "sent": frozenset({"replied", "prepaid", "opt_out"}),
    "replied": frozenset({"negotiating", "prepaid", "opt_out"}),
    "negotiating": frozenset({"prepaid", "opt_out"}),
    "prepaid": frozenset({"launched", "opt_out"}),
    "launched": frozenset({"opt_out"}),
    "skipped": frozenset({"ready", "opt_out"}),
    "opt_out": frozenset(),
}

# Statuses in which a clinic has answered or later; a reply can be recorded from these.
REPLY_ALLOWED_STATUSES = frozenset({"sent", "replied", "negotiating", "prepaid", "launched"})


def _dumps(payload: dict | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, default=str)


def log_event(
    session: AsyncSession,
    event_type: str,
    now: datetime,
    clinic_id: int | None = None,
    payload: dict | None = None,
) -> Event:
    event = Event(clinic_id=clinic_id, type=event_type, payload=_dumps(payload), created_at=now)
    session.add(event)
    return event


# ---------- clinics ----------


async def create_clinic(
    session: AsyncSession,
    *,
    name: str,
    direction: str | None,
    public_contact: str | None,
    note: str | None,
    now: datetime,
) -> Clinic:
    name = name.strip()
    if not name:
        raise TrackerError("Название клиники пустое.")
    existing = await find_clinics(session, name)
    if any(c.name.lower() == name.lower() for c in existing):
        raise TrackerError(f"Клиника «{name}» уже есть в трекере.")
    clinic = Clinic(
        name=name,
        direction=(direction or None),
        public_contact=(public_contact or None),
        note=(note or None),
        status="new",
        created_at=now,
        updated_at=now,
    )
    session.add(clinic)
    await session.flush()
    log_event(session, "created", now, clinic.id, {"name": name})
    return clinic


async def find_clinics(session: AsyncSession, query: str) -> list[Clinic]:
    """Exact (case-insensitive) match first; otherwise substring matches."""
    q = query.strip()
    if not q:
        return []
    lowered = q.lower()
    rows = (await session.execute(select(Clinic).order_by(Clinic.id))).scalars().all()
    exact = [c for c in rows if c.name.lower() == lowered]
    if exact:
        return exact
    return [c for c in rows if lowered in c.name.lower()]


async def resolve_clinic(session: AsyncSession, query: str) -> Clinic:
    matches = await find_clinics(session, query)
    if not matches:
        raise TrackerError(f"Клиника «{query.strip()}» не найдена. Проверьте название или добавьте её.")
    if len(matches) > 1:
        names = ", ".join(f"«{c.name}»" for c in matches[:5])
        raise TrackerError(f"Под «{query.strip()}» подходят несколько клиник: {names}. Уточните название.")
    return matches[0]


async def change_status(
    session: AsyncSession,
    clinic: Clinic,
    new_status: str,
    now: datetime,
    reason: str | None = None,
) -> None:
    if new_status not in CLINIC_STATUSES:
        raise ValueError(f"Unknown status: {new_status}")
    old = clinic.status
    if old == new_status:
        return
    if new_status not in ALLOWED_TRANSITIONS[old]:
        raise InvalidTransition(
            f"Нельзя перевести клинику из статуса «{STATUS_RU[old]}» в «{STATUS_RU[new_status]}»."
        )
    clinic.status = new_status
    clinic.updated_at = now
    log_event(session, "status_changed", now, clinic.id, {"from": old, "to": new_status, "reason": reason})


# ---------- messages / drafts ----------


async def add_draft(
    session: AsyncSession,
    clinic: Clinic,
    *,
    text: str,
    lang: str,
    kind: str,
    now: datetime,
) -> Message:
    """Store a new draft version. Older open drafts of the same kind become 'rejected'."""
    if clinic.status == "opt_out":
        raise OptedOutError("Клиника просила не писать, черновики не создаются.")
    open_drafts = (
        await session.execute(
            select(Message).where(
                Message.clinic_id == clinic.id, Message.kind == kind, Message.status == "draft"
            )
        )
    ).scalars().all()
    for old in open_drafts:
        old.status = "rejected"

    last_version = (
        await session.execute(
            select(func.max(Message.version)).where(Message.clinic_id == clinic.id, Message.kind == kind)
        )
    ).scalar()
    message = Message(
        clinic_id=clinic.id,
        kind=kind,
        text=text,
        lang=lang,
        version=(last_version or 0) + 1,
        status="draft",
        created_at=now,
    )
    session.add(message)
    await session.flush()
    log_event(
        session,
        "draft_created",
        now,
        clinic.id,
        {"message_id": message.id, "kind": kind, "version": message.version, "lang": lang},
    )
    return message


async def get_message(session: AsyncSession, message_id: int) -> Message | None:
    return await session.get(Message, message_id)


async def pending_drafts(session: AsyncSession, limit: int = 5) -> list[tuple[Message, Clinic]]:
    """Open drafts waiting for the owner, oldest first."""
    rows = (
        await session.execute(
            select(Message, Clinic)
            .join(Clinic, Clinic.id == Message.clinic_id)
            .where(Message.status == "draft", Clinic.status != "opt_out")
            .order_by(Message.created_at, Message.id)
            .limit(limit)
        )
    ).all()
    return [(m, c) for m, c in rows]


async def set_focus(session: AsyncSession, message: Message, now: datetime) -> None:
    """Remember which draft a plain-text 'Да' / 'Нет' refers to."""
    log_event(session, "focus", now, message.clinic_id, {"message_id": message.id})


async def get_focus_message(session: AsyncSession) -> Message | None:
    event = (
        await session.execute(
            select(Event).where(Event.type == "focus").order_by(Event.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if event is None or not event.payload:
        return None
    message_id = json.loads(event.payload).get("message_id")
    if message_id is None:
        return None
    return await session.get(Message, int(message_id))


async def get_clinic(session: AsyncSession, clinic_id: int) -> Clinic | None:
    return await session.get(Clinic, clinic_id)


async def approve_draft(session: AsyncSession, message: Message, clinic: Clinic, now: datetime) -> None:
    """Owner said 'Да'. Moves the clinic forward. Limits must be checked by the caller first.

    Does NOT send anything to the clinic. The owner sends the text himself and then marks it sent.
    """
    if clinic.status == "opt_out":
        raise OptedOutError("Клиника просила не писать. Одобрение заблокировано.")
    if message.status != "draft":
        raise TrackerError("Это сообщение уже обработано.")
    if not clinic.public_contact:
        raise TrackerError("У клиники нет публичного контакта. Одобрение заблокировано, добавьте контакт.")
    target = "approved" if message.kind == "outreach" else "negotiating"
    await change_status(session, clinic, target, now, reason=f"approved {message.kind} message")
    message.status = "approved"
    message.approved_at = now
    log_event(
        session,
        "approved",
        now,
        clinic.id,
        {"message_id": message.id, "kind": message.kind, "version": message.version},
    )


async def reject_draft(session: AsyncSession, message: Message, clinic: Clinic, now: datetime) -> None:
    """Owner said 'Нет'. Outreach: clinic becomes skipped. Reply draft: clinic keeps its status."""
    if message.status != "draft":
        raise TrackerError("Это сообщение уже обработано.")
    message.status = "rejected"
    if message.kind == "outreach":
        await change_status(session, clinic, "skipped", now, reason="owner rejected")
    log_event(session, "rejected", now, clinic.id, {"message_id": message.id, "kind": message.kind})


async def mark_sent(session: AsyncSession, clinic: Clinic, now: datetime) -> Message:
    """Owner reports that he sent the approved text himself ('отправлено <клиника>')."""
    approved = (
        await session.execute(
            select(Message)
            .where(Message.clinic_id == clinic.id, Message.status == "approved")
            .order_by(Message.approved_at.desc(), Message.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if approved is None:
        raise TrackerError("Нет одобренного сообщения для этой клиники. Сначала нажмите «Да».")
    if approved.kind == "outreach":
        await change_status(session, clinic, "sent", now, reason="owner confirmed manual send")
    approved.status = "sent"
    approved.sent_at = now
    log_event(session, "sent", now, clinic.id, {"message_id": approved.id, "kind": approved.kind})
    return approved


async def record_reply(
    session: AsyncSession,
    clinic: Clinic,
    *,
    text: str,
    now: datetime,
    opt_out_requested: bool,
) -> None:
    """Store a clinic's reply. If the reply asks to stop, the clinic is opted out instead."""
    if opt_out_requested:
        await opt_out(session, clinic, now, reason="reply asked to stop")
        log_event(session, "reply_received", now, clinic.id, {"text": text, "opt_out": True})
        return
    if clinic.status == "opt_out":
        # Already opted out: keep the reply for the audit trail, change nothing else.
        log_event(session, "reply_received", now, clinic.id, {"text": text, "opt_out": True, "ignored": True})
        return
    if clinic.status not in REPLY_ALLOWED_STATUSES:
        raise TrackerError(
            "Сначала отметьте, что сообщение отправлено: «отправлено <название>». Тогда можно записать ответ."
        )
    if clinic.status == "sent":
        await change_status(session, clinic, "replied", now, reason="clinic replied")
    log_event(session, "reply_received", now, clinic.id, {"text": text, "opt_out": False})


async def opt_out(session: AsyncSession, clinic: Clinic, now: datetime, reason: str) -> bool:
    """Mark the clinic as opt_out and close all open drafts. Returns False if it already was."""
    if clinic.status == "opt_out":
        return False
    await change_status(session, clinic, "opt_out", now, reason=reason)
    open_drafts = (
        await session.execute(
            select(Message).where(Message.clinic_id == clinic.id, Message.status == "draft")
        )
    ).scalars().all()
    for draft in open_drafts:
        draft.status = "rejected"
    log_event(session, "opt_out", now, clinic.id, {"reason": reason, "rejected_drafts": len(open_drafts)})
    return True


# ---------- payments ----------


def parse_amount(raw: str) -> Decimal:
    cleaned = raw.strip().replace(" ", "").replace("$", "").replace(",", ".").lower()
    cleaned = cleaned.replace("usd", "").replace("долларов", "").replace("долл", "")
    try:
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        raise TrackerError("Сумма не распознана. Пример: 250 или 250$.")
    if amount <= 0 or amount > 100_000:
        raise TrackerError("Сумма должна быть больше нуля и не больше 100000 $.")
    return amount.quantize(Decimal("0.01"))


async def record_payment(
    session: AsyncSession,
    clinic: Clinic,
    *,
    amount: Decimal,
    kind: str,
    note: str | None,
    now: datetime,
) -> Payment:
    """Owner logs a payment he received by hand. The bot does not take payments."""
    if kind not in PAYMENT_KINDS:
        raise ValueError(f"Unknown payment kind: {kind}")
    if clinic.status != "opt_out":
        if kind == "prepayment" and clinic.status in ("sent", "replied", "negotiating"):
            await change_status(session, clinic, "prepaid", now, reason="prepayment received")
        elif kind == "prepayment" and clinic.status not in ("prepaid", "launched"):
            raise TrackerError(
                f"Предоплату нельзя записать для статуса «{STATUS_RU[clinic.status]}». "
                "Сначала отметьте отправку и ответ клиники."
            )
        elif kind == "launch":
            if clinic.status == "prepaid":
                await change_status(session, clinic, "launched", now, reason="launch payment")
            elif clinic.status != "launched":
                raise TrackerError("Платёж за запуск можно записать только после предоплаты.")
        elif kind == "subscription" and clinic.status != "launched":
            raise TrackerError("Подписка начинается после запуска. Сначала запишите запуск.")
    payment = Payment(
        clinic_id=clinic.id,
        amount_usd=amount,
        kind=kind,
        note=(note or None),
        received_at=now,
    )
    session.add(payment)
    await session.flush()
    log_event(session, "payment", now, clinic.id, {"amount_usd": str(amount), "kind": kind})
    return payment


# ---------- stats ----------


@dataclass(frozen=True)
class Stats:
    total_clinics: int
    by_status: dict[str, int]
    outreach_sent: int
    clinics_replied: int
    payments_by_kind: dict[str, Decimal]

    @property
    def prepayment_total(self) -> Decimal:
        return self.payments_by_kind.get("prepayment", Decimal("0"))


async def stats(session: AsyncSession) -> Stats:
    status_rows = (
        await session.execute(select(Clinic.status, func.count(Clinic.id)).group_by(Clinic.status))
    ).all()
    by_status = {status: int(count) for status, count in status_rows}
    total = sum(by_status.values())

    outreach_sent = (
        await session.execute(
            select(func.count(Message.id)).where(Message.kind == "outreach", Message.status == "sent")
        )
    ).scalar_one()

    clinics_replied = (
        await session.execute(
            select(func.count(func.distinct(Event.clinic_id))).where(Event.type == "reply_received")
        )
    ).scalar_one()

    payment_rows = (
        await session.execute(select(Payment.kind, func.sum(Payment.amount_usd)).group_by(Payment.kind))
    ).all()
    payments = {kind: Decimal(str(total_amount or 0)) for kind, total_amount in payment_rows}

    return Stats(
        total_clinics=total,
        by_status=by_status,
        outreach_sent=int(outreach_sent),
        clinics_replied=int(clinics_replied),
        payments_by_kind=payments,
    )
