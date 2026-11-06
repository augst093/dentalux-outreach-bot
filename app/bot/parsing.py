"""Parse owner's text commands and detect opt-out requests. Pure functions, no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Cmd(str, Enum):
    HELP = "help"
    LIST = "list"
    APPROVE = "approve"
    SKIP = "skip"
    REDO = "redo"
    REPLY = "reply"
    STATUS = "status"
    ADD = "add"
    CANCEL = "cancel"
    PREPAY = "prepay"
    LAUNCH = "launch"
    SUBSCRIPTION = "subscription"
    NEGOTIATE = "negotiate"
    OPT_OUT = "opt_out"
    SENT = "sent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Parsed:
    cmd: Cmd
    name: str | None = None  # clinic name (or query)
    text: str | None = None  # reply text, redo wish
    amount: str | None = None  # raw amount, parsed later
    error: str | None = None  # user-facing format hint


_I = re.IGNORECASE | re.DOTALL

_EXACT: list[tuple[Cmd, re.Pattern[str]]] = [
    (Cmd.HELP, re.compile(r"^(/start|/help|помощь|команды|help)$", _I)),
    (Cmd.LIST, re.compile(r"^(список кому пишешь|список|кому пишешь)$", _I)),
    (Cmd.APPROVE, re.compile(r"^да$", _I)),
    (Cmd.SKIP, re.compile(r"^нет$", _I)),
    (Cmd.STATUS, re.compile(r"^(статус|/status)$", _I)),
    (Cmd.ADD, re.compile(r"^(добавить клинику|добавить)$", _I)),
    (Cmd.CANCEL, re.compile(r"^(отмена|отмени|/cancel)$", _I)),
]

_PREFIX_NAME_TEXT: list[tuple[Cmd, re.Pattern[str], str]] = [
    # (command, pattern with groups (name, rest), format hint)
    (Cmd.REPLY, re.compile(r"^ответ\s+(.+?)\s*:\s*(.+)$", _I), "ответ <клиника>: <текст ответа>"),
    (Cmd.PREPAY, re.compile(r"^предоплата\s+(.+?)\s*:\s*(.+)$", _I), "предоплата <клиника>: <сумма>"),
    (Cmd.SUBSCRIPTION, re.compile(r"^подписка\s+(.+?)\s*:\s*(.+)$", _I), "подписка <клиника>: <сумма>"),
]

_PREFIX_NAME_ONLY: list[tuple[Cmd, re.Pattern[str], str]] = [
    (Cmd.OPT_OUT, re.compile(r"^opt_?out\s+(.+)$", _I), "opt_out <клиника>"),
    (Cmd.SENT, re.compile(r"^отправлено\s+(.+)$", _I), "отправлено <клиника>"),
    (Cmd.NEGOTIATE, re.compile(r"^переговоры\s+(.+)$", _I), "переговоры <клиника>"),
]

_LAUNCH = re.compile(r"^запуск\s+(.+?)(?:\s*:\s*(.+))?$", _I)
_REDO = re.compile(r"^переделай(?:\s*:\s*|\s+|$)(.*)$", _I | re.DOTALL)

_OPT_OUT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"не\s+пишит?е",
        r"не\s+беспокойте",
        r"не\s+беспокоить",
        r"не\s+надо\s+писать",
        r"не\s+нужно\s+писать",
        r"больше\s+не\s+пиш",
        r"не\s+звоните",
        r"отпишит\w*",
        r"отписат\w*",
        r"прекратите",
        r"удалите\s+(меня|нас|наш)",
        r"\bстоп\b",
        r"\bstop\b",
        r"unsubscribe",
        r"\byozmang\b",
        r"bezovta\s+qilmang",
    )
]


_KEYWORDS = {Cmd.REPLY: "ответ", Cmd.PREPAY: "предоплата", Cmd.SUBSCRIPTION: "подписка"}


def cmd_keyword(cmd: Cmd) -> str:
    return _KEYWORDS[cmd]


def detect_opt_out(text: str) -> bool:
    """True if a clinic's reply asks us to stop. Deliberately broad: a false positive only blocks messages."""
    return any(p.search(text) for p in _OPT_OUT_PATTERNS)


def parse_command(raw: str) -> Parsed:
    text = (raw or "").strip()
    norm = " ".join(text.split())

    for cmd, pattern in _EXACT:
        if pattern.match(norm):
            return Parsed(cmd=cmd)

    match = _REDO.match(text)
    if match:
        wish = match.group(1).strip() or None
        return Parsed(cmd=Cmd.REDO, text=wish)

    match = _LAUNCH.match(norm)
    if match:
        return Parsed(cmd=Cmd.LAUNCH, name=match.group(1).strip(), amount=(match.group(2) or None))

    keyword = norm.split(" ", 1)[0].lower()
    for cmd, pattern, hint in _PREFIX_NAME_TEXT:
        if keyword != cmd_keyword(cmd):
            continue
        match = pattern.match(norm)
        if not match:
            return Parsed(cmd=cmd, error=f"Формат: {hint}")
        if cmd == Cmd.REPLY:
            return Parsed(cmd=cmd, name=match.group(1).strip(), text=match.group(2).strip())
        return Parsed(cmd=cmd, name=match.group(1).strip(), amount=match.group(2).strip())

    for cmd, pattern, _hint in _PREFIX_NAME_ONLY:
        match = pattern.match(norm)
        if match:
            return Parsed(cmd=cmd, name=match.group(1).strip())

    return Parsed(cmd=Cmd.UNKNOWN)


def parse_wish_lang(wish: str | None) -> str | None:
    """'узбекский' / 'русский' inside a «Переделай» wish switches the draft language."""
    if not wish:
        return None
    lowered = wish.lower()
    if "узбек" in lowered or "uzbek" in lowered or "o'zbek" in lowered:
        return "uz"
    if "русск" in lowered or "russian" in lowered:
        return "ru"
    return None


HELP_TEXT = """Команды (только для владельца):

список кому пишешь: очередь черновиков (до 5)
Да: одобрить черновик, который сейчас в фокусе (получите текст, отправите сами)
Нет: пропустить его
Переделай: <пожелание>: переписать. Для узбекского: «Переделай: на узбекском»
ответ <клиника>: <текст>: записать ответ клиники и получить черновик ответа
отправлено <клиника>: вы отправили текст вручную
статус: сводка
добавить клинику: пошагово
предоплата <клиника>: <сумма>: записать оплату вручную
запуск <клиника>: <сумма>: запуск (по умолчанию 200)
подписка <клиника>: <сумма>: платёж подписки
переговоры <клиника>: клиника на переговорах
opt_out <клиника>: клиника просила не писать

Бот ничего не отправляет клиникам сам."""
