"""Owner-facing workflow. Turns commands and button presses into database changes and replies.

This module never sends anything to a clinic. Its replies go only to the owner.
Telegram-specific code lives in app/bot/handlers.py; this module can be tested without Telegram.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.parsing import Cmd, detect_opt_out, parse_command, parse_wish_lang, HELP_TEXT
from app.db.models import Clinic, Event, Message, utcnow
from app.services import tracker
from app.services.drafts import DraftGenerator, DraftRequest
from app.services.limits import SendLimiter
from app.services.tracker import TrackerError

logger = logging.getLogger(__name__)

Buttons = list[list[tuple[str, str]]]  # rows of (label, callback_data)


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: Buttons = field(default_factory=list)


def _contact_hint(contact: str | None) -> str:
    if not contact:
        return "публичного контакта нет"
    if contact.startswith("@"):
        return f"{contact} (https://t.me/{contact[1:]})"
    return contact


def _card(clinic: Clinic, message: Message) -> Reply:
    kind = "ответ клиники" if message.kind == "reply" else "первое сообщение"
    lines = [
        f"#{clinic.id} {clinic.name}",
        f"Направление: {clinic.direction or 'не указано'}",
        f"Тип: {kind}, язык: {message.lang}, версия {message.version}",
        f"Контакт: {_contact_hint(clinic.public_contact)}",
        "",
        message.text,
    ]
    buttons: Buttons = [
        [("Отправить (одобрить)", f"ok:{message.id}"), ("Пропустить", f"no:{message.id}")],
        [("Переделать", f"rd:{message.id}")],
    ]
    return Reply(text="\n".join(lines), buttons=buttons)


class Operator:
    def __init__(
        self,
        session: AsyncSession,
        *,
        generator: DraftGenerator,
        limiter: SendLimiter,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session = session
        self.generator = generator
        self.limiter = limiter
        self.clock = clock

    # ---------- entry points ----------

    async def handle_text(self, text: str) -> list[Reply]:
        parsed = parse_command(text)
        if parsed.error:
            return [Reply(parsed.error)]
        try:
            return await self._dispatch(parsed)
        except TrackerError as exc:
            return [Reply(str(exc))]

    async def handle_callback(self, data: str) -> list[Reply]:
        action, _, raw_id = data.partition(":")
        try:
            message_id = int(raw_id)
        except ValueError:
            return [Reply("Кнопка устарела. Откройте список заново: «список кому пишешь».")]
        message = await tracker.get_message(self.session, message_id)
        if message is None:
            return [Reply("Черновик не найден. Откройте список заново.")]
        try:
            if action == "ok":
                return await self._approve(message)
            if action == "no":
                return await self._skip(message)
            if action == "rd":
                await tracker.set_focus(self.session, message, self.clock())
                return [Reply("Напишите одной строкой: «Переделай: <пожелание>». Например: «Переделай: короче».")]
        except TrackerError as exc:
            return [Reply(str(exc))]
        return [Reply("Неизвестная кнопка.")]

    async def create_clinic(
        self,
        *,
        name: str,
        direction: str | None,
        public_contact: str | None,
        note: str | None,
    ) -> list[Reply]:
        now = self.clock()
        clinic = await tracker.create_clinic(
            self.session, name=name, direction=direction, public_contact=public_contact, note=note, now=now
        )
        draft = await self.generator.generate(
            DraftRequest(clinic_name=clinic.name, direction=clinic.direction, note=clinic.note)
        )
        await tracker.change_status(self.session, clinic, "ready", now, reason="draft generated")
        message = await tracker.add_draft(
            self.session, clinic, text=draft.text, lang=draft.lang, kind="outreach", now=now
        )
        await tracker.set_focus(self.session, message, now)
        head = f"Клиника добавлена: {clinic.name}. Черновик готов."
        if draft.note:
            head += f"\n({draft.note})"
        return [Reply(head), _card(clinic, message)]

    # ---------- dispatch ----------

    async def _dispatch(self, parsed) -> list[Reply]:
        cmd = parsed.cmd
        if cmd == Cmd.HELP:
            return [Reply(HELP_TEXT)]
        if cmd == Cmd.LIST:
            return await self._list()
        if cmd == Cmd.APPROVE:
            return await self._approve_focus()
        if cmd == Cmd.SKIP:
            return await self._skip_focus()
        if cmd == Cmd.REDO:
            return await self._redo(parsed.text)
        if cmd == Cmd.REPLY:
            return await self._reply(parsed.name or "", parsed.text or "")
        if cmd == Cmd.STATUS:
            return [Reply(await self._status())]
        if cmd == Cmd.PREPAY:
            return [await self._payment(parsed.name, parsed.amount, "prepayment")]
        if cmd == Cmd.LAUNCH:
            amount = parsed.amount or "200"
            return [await self._payment(parsed.name, amount, "launch")]
        if cmd == Cmd.SUBSCRIPTION:
            return [await self._payment(parsed.name, parsed.amount, "subscription")]
        if cmd == Cmd.NEGOTIATE:
            return [await self._negotiate(parsed.name or "")]
        if cmd == Cmd.OPT_OUT:
            return [await self._opt_out(parsed.name or "")]
        if cmd == Cmd.SENT:
            return [await self._sent(parsed.name or "")]
        if cmd == Cmd.ADD:
            return [Reply("Добавление клиники: отправьте «добавить клинику», мастер проведёт по шагам.")]
        return [Reply("Команда не распознана. Напишите «помощь», чтобы увидеть список.")]

    # ---------- list / approve / skip / redo ----------

    async def _list(self) -> list[Reply]:
        pending = await tracker.pending_drafts(self.session, limit=5)
        if not pending:
            return [Reply("Очередь пуста. Добавьте клинику: «добавить клинику».")]
        first_message = pending[0][0]
        await tracker.set_focus(self.session, first_message, self.clock())
        replies = [
            Reply(
                f"К отправке: {len(pending)}. «Да» и «Нет» текстом относятся к первой карточке. "
                "Остальные карточки — кнопками. «Отправить» только одобряет текст: отправляете вы сами."
            )
        ]
        replies += [_card(clinic, message) for message, clinic in pending]
        return replies

    async def _approve_focus(self) -> list[Reply]:
        message = await tracker.get_focus_message(self.session)
        if message is None:
            return [Reply("Сейчас нет черновика в фокусе. Откройте список: «список кому пишешь».")]
        return await self._approve(message)

    async def _skip_focus(self) -> list[Reply]:
        message = await tracker.get_focus_message(self.session)
        if message is None:
            return [Reply("Сейчас нет черновика в фокусе. Откройте список: «список кому пишешь».")]
        return await self._skip(message)

    async def _approve(self, message: Message) -> list[Reply]:
        clinic = await tracker.get_clinic(self.session, message.clinic_id)
        if clinic is None:
            raise TrackerError("Клиника не найдена.")
        if message.status != "draft":
            raise TrackerError("Это сообщение уже обработано.")
        if clinic.status == "opt_out":
            raise TrackerError(f"«{clinic.name}» просила не писать. Одобрение заблокировано.")

        now = self.clock()
        decision = await self.limiter.check(self.session, now)
        if not decision.allowed:
            return [Reply(decision.reason + " Черновик остался в очереди.")]

        await tracker.approve_draft(self.session, message, clinic, now)
        instructions = (
            f"Одобрено: {clinic.name}.\n"
            f"Отправьте текст сами. Контакт: {_contact_hint(clinic.public_contact)}.\n"
            f"Когда отправите, напишите: отправлено {clinic.name}\n"
            f"Следующее одобрение — не раньше чем через {self.limiter.pause_seconds // 60} мин."
        )
        return [Reply(instructions), Reply(message.text)]

    async def _skip(self, message: Message) -> list[Reply]:
        clinic = await tracker.get_clinic(self.session, message.clinic_id)
        if clinic is None:
            raise TrackerError("Клиника не найдена.")
        await tracker.reject_draft(self.session, message, clinic, self.clock())
        if message.kind == "outreach":
            return [Reply(f"Пропущено: {clinic.name}. Статус: пропущено.")]
        return [Reply(f"Черновик ответа для {clinic.name} отброшен. Статус клиники не изменился.")]

    async def _redo(self, wish: str | None) -> list[Reply]:
        message = await tracker.get_focus_message(self.session)
        if message is None:
            return [Reply("Сначала откройте карточку: «список кому пишешь» или «ответ <клиника>: …».")]
        if not wish:
            return [Reply("Напишите пожелание: «Переделай: <пожелание>». Например: «Переделай: короче».")]
        if message.status != "draft":
            raise TrackerError("Это сообщение уже обработано, переделывать нечего.")
        clinic = await tracker.get_clinic(self.session, message.clinic_id)
        if clinic is None:
            raise TrackerError("Клиника не найдена.")
        if clinic.status == "opt_out":
            raise TrackerError(f"«{clinic.name}» просила не писать. Новые черновики не создаются.")

        now = self.clock()
        lang = parse_wish_lang(wish) or message.lang
        clinic_reply = None
        if message.kind == "reply":
            clinic_reply = await self._latest_clinic_reply(clinic.id)
        draft = await self.generator.generate(
            DraftRequest(
                clinic_name=clinic.name,
                direction=clinic.direction,
                note=clinic.note,
                lang=lang,
                kind=message.kind,
                instruction=wish,
                previous_text=message.text,
                clinic_reply=clinic_reply,
            )
        )
        new_message = await tracker.add_draft(
            self.session, clinic, text=draft.text, lang=draft.lang, kind=message.kind, now=now
        )
        await tracker.set_focus(self.session, new_message, now)
        replies: list[Reply] = []
        if draft.note:
            replies.append(Reply(f"({draft.note})"))
        replies.append(_card(clinic, new_message))
        return replies

    # ---------- replies from clinics ----------

    async def _reply(self, name: str, text: str) -> list[Reply]:
        if not text:
            raise TrackerError("Формат: ответ <клиника>: <текст ответа>")
        clinic = await tracker.resolve_clinic(self.session, name)
        now = self.clock()

        if clinic.status == "opt_out":
            await tracker.record_reply(self.session, clinic, text=text, now=now, opt_out_requested=False)
            # record_reply logs the reply for opt_out clinics without changing status.
            return [Reply(f"«{clinic.name}» уже в статусе opt_out. Ответ записан, новых сообщений нет.")]

        if detect_opt_out(text):
            await tracker.record_reply(self.session, clinic, text=text, now=now, opt_out_requested=True)
            return [
                Reply(
                    f"«{clinic.name}» просит не писать. Статус: opt_out. "
                    "Новые сообщения заблокированы, открытые черновики закрыты. Ответ записан."
                )
            ]

        await tracker.record_reply(self.session, clinic, text=text, now=now, opt_out_requested=False)
        draft = await self.generator.generate(
            DraftRequest(
                clinic_name=clinic.name,
                direction=clinic.direction,
                note=clinic.note,
                kind="reply",
                clinic_reply=text,
            )
        )
        message = await tracker.add_draft(self.session, clinic, text=draft.text, lang=draft.lang, kind="reply", now=now)
        await tracker.set_focus(self.session, message, now)
        replies = [Reply(f"Ответ записан: {clinic.name}. Черновик ответа ниже, ждёт вашего «Да».")]
        if draft.note:
            replies.append(Reply(f"({draft.note})"))
        replies.append(_card(clinic, message))
        return replies

    async def _latest_clinic_reply(self, clinic_id: int) -> str | None:
        event = (
            await self.session.execute(
                select(Event)
                .where(Event.clinic_id == clinic_id, Event.type == "reply_received")
                .order_by(Event.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if event is None or not event.payload:
            return None
        return json.loads(event.payload).get("text")


    # ---------- status changes typed by the owner ----------

    async def _sent(self, name: str) -> Reply:
        clinic = await tracker.resolve_clinic(self.session, name)
        await tracker.mark_sent(self.session, clinic, self.clock())
        return Reply(f"Отмечено: отправлено «{clinic.name}». Статус: {tracker.STATUS_RU[clinic.status]}.")

    async def _negotiate(self, name: str) -> Reply:
        clinic = await tracker.resolve_clinic(self.session, name)
        await tracker.change_status(self.session, clinic, "negotiating", self.clock(), reason="owner set")
        return Reply(f"«{clinic.name}»: переговоры.")

    async def _opt_out(self, name: str) -> Reply:
        clinic = await tracker.resolve_clinic(self.session, name)
        changed = await tracker.opt_out(self.session, clinic, self.clock(), reason="owner command")
        if not changed:
            return Reply(f"«{clinic.name}» уже в статусе opt_out.")
        return Reply(f"«{clinic.name}»: opt_out. Новые сообщения заблокированы.")

    async def _payment(self, name: str | None, amount_raw: str | None, kind: str) -> Reply:
        if not name:
            raise TrackerError("Укажите клинику: «предоплата <клиника>: <сумма>».")
        if amount_raw is None:
            raise TrackerError("Укажите сумму, например: 250")
        clinic = await tracker.resolve_clinic(self.session, name)
        amount = tracker.parse_amount(amount_raw)
        await tracker.record_payment(
            self.session, clinic, amount=amount, kind=kind, note=None, now=self.clock()
        )
        labels = {"prepayment": "предоплата", "launch": "запуск (остаток)", "subscription": "подписка"}
        return Reply(
            f"Записано: {labels[kind]} {amount} $ от «{clinic.name}». Статус: {tracker.STATUS_RU[clinic.status]}."
        )

    async def _status(self) -> str:
        stats = await tracker.stats(self.session)
        order = ["new", "ready", "approved", "sent", "replied", "negotiating", "prepaid", "launched", "skipped", "opt_out"]
        status_lines = [
            f"  {tracker.STATUS_RU[s]}: {stats.by_status[s]}" for s in order if stats.by_status.get(s)
        ]
        pay = stats.payments_by_kind
        lines = [
            "Сводка",
            f"Клиник в трекере: {stats.total_clinics}",
            f"Первых сообщений отправлено вручную: {stats.outreach_sent}",
            f"Ответили: {stats.clinics_replied}",
            f"На переговорах: {stats.by_status.get('negotiating', 0)}",
            f"Предоплаты (введено вручную): {stats.prepayment_total} $",
            f"Запуски: {pay.get('launch', 0)} $; подписки: {pay.get('subscription', 0)} $",
        ]
        if status_lines:
            lines.append("По статусам:")
            lines.extend(status_lines)
        return "\n".join(lines)


