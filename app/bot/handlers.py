"""Telegram layer: routes updates to the Operator. Contains no business rules.

Every update passes OwnerOnlyMiddleware first. Anyone except ADMIN_USER_ID gets a short refusal
and nothing else. The bot never sends messages to clinics; it replies only to the owner.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    User,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.parsing import Cmd, parse_command
from app.services.operator import Operator, Reply
from app.services.tracker import TrackerError

logger = logging.getLogger(__name__)

REFUSAL_TEXT = "Этот бот работает только для владельца DentaLux."
CANCEL_WORDS = {"отмена", "отмени", "/cancel"}


class AddClinic(StatesGroup):
    name = State()
    direction = State()
    contact = State()
    note = State()


class OwnerOnlyMiddleware(BaseMiddleware):
    """Outer middleware: runs before any handler or FSM state is touched."""

    def __init__(self, admin_user_id: int) -> None:
        self.admin_user_id = admin_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.id != self.admin_user_id:
            if isinstance(event, Message):
                await event.answer(REFUSAL_TEXT)
            elif isinstance(event, CallbackQuery):
                await event.answer(REFUSAL_TEXT, show_alert=True)
            logger.info("rejected non-owner update")
            return None
        return await handler(event, data)


class SessionMiddleware(BaseMiddleware):
    """Creates one DB session per update and injects `session` and `operator`.

    Handlers commit explicitly, before they send replies, so the owner never sees
    'done' for something that was not saved.
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        make_operator: Callable[[AsyncSession], Operator],
    ) -> None:
        self.session_factory = session_factory
        self.make_operator = make_operator

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            data["operator"] = self.make_operator(session)
            try:
                return await handler(event, data)
            except Exception:
                await session.rollback()
                raise


def _markup(buttons: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in buttons
        ]
    )


async def _send_replies(message: Message, replies: list[Reply]) -> None:
    for reply in replies:
        await message.answer(reply.text, reply_markup=_markup(reply.buttons))


def build_dispatcher(admin_user_id: int, session_factory, make_operator) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    router = Router(name="owner")

    owner_guard = OwnerOnlyMiddleware(admin_user_id)
    db = SessionMiddleware(session_factory, make_operator)
    dp.message.outer_middleware(owner_guard)
    dp.callback_query.outer_middleware(owner_guard)
    router.message.middleware(db)
    router.callback_query.middleware(db)

    # --- wizard: add clinic (order matters: cancel and state handlers before generic text) ---

    @router.message(StateFilter(AddClinic), F.text.func(lambda t: t.strip().lower() in CANCEL_WORDS))
    async def wizard_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Добавление отменено.")

    @router.message(F.text.func(lambda t: parse_command(t).cmd == Cmd.ADD))
    async def wizard_start(message: Message, state: FSMContext) -> None:
        await state.set_state(AddClinic.name)
        await message.answer("Название клиники? (отмена — остановить)")

    @router.message(AddClinic.name, F.text)
    async def wizard_name(message: Message, state: FSMContext) -> None:
        await state.update_data(name=message.text.strip())
        await state.set_state(AddClinic.direction)
        await message.answer("Направление (например: импланты, брекеты, виниры)?")

    @router.message(AddClinic.direction, F.text)
    async def wizard_direction(message: Message, state: FSMContext) -> None:
        await state.update_data(direction=message.text.strip())
        await state.set_state(AddClinic.contact)
        await message.answer(
            "Публичный контакт клиники: телефон, @username или ссылка t.me/instagram. Без него сообщение не одобрить."
        )

    @router.message(AddClinic.contact, F.text)
    async def wizard_contact(message: Message, state: FSMContext) -> None:
        contact = message.text.strip()
        if not contact or len(contact) > 300:
            await message.answer("Контакт пустой или слишком длинный. Введите ещё раз.")
            return
        await state.update_data(contact=contact)
        await state.set_state(AddClinic.note)
        await message.answer("Заметка о клинике (что видно публично). Если нет — отправьте «-».")

    @router.message(AddClinic.note, F.text)
    async def wizard_note(message: Message, state: FSMContext, operator: Operator, session: AsyncSession) -> None:
        data = await state.get_data()
        note = message.text.strip()
        note = None if note in {"-", ""} else note
        await state.clear()
        try:
            replies = await operator.create_clinic(
                name=data["name"],
                direction=data.get("direction"),
                public_contact=data.get("contact"),
                note=note,
            )
            await session.commit()
        except TrackerError as exc:
            await session.rollback()
            await message.answer(f"Не добавлено: {exc}")
            return
        await _send_replies(message, replies)

    # --- generic text commands ---

    @router.message(F.text)
    async def on_text(message: Message, operator: Operator, session: AsyncSession) -> None:
        replies = await operator.handle_text(message.text or "")
        await session.commit()
        await _send_replies(message, replies)

    @router.callback_query(F.data.func(lambda d: bool(d) and d.split(":", 1)[0] in {"ok", "no", "rd"}))
    async def on_button(callback: CallbackQuery, operator: Operator, session: AsyncSession) -> None:
        await callback.answer()
        replies = await operator.handle_callback(callback.data or "")
        await session.commit()
        chat_id = callback.from_user.id
        bot = callback.bot
        for reply in replies:
            await bot.send_message(chat_id=chat_id, text=reply.text, reply_markup=_markup(reply.buttons))

    dp.include_router(router)
    return dp
