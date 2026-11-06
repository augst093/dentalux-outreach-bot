"""Message drafts for clinics.

Two sources:
1. Claude API (model from settings, key from ANTHROPIC_API_KEY). Output is validated.
2. Built-in templates. Used when there is no key, the API fails, or the output breaks a rule.

Rules every draft must satisfy (see `validate_draft`):
- at most 600 characters; no exclamation marks; no ALL-CAPS words;
- exactly one question, at the end;
- mentions DentaLux and that the sender is an assistant (not a human);
- money mentioned only as the terms from the product brief (250 $, 200 $, 50 $);
- no revenue or guarantee promises.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)

MAX_LENGTH = 600
ALLOWED_AMOUNTS = {250, 200, 50}  # the only prices in the product brief

FORBIDDEN_PHRASES = (
    "гарант",
    "выручк",
    "100%",
    "увеличим доход",
    "удвоим",
    "guarantee",
    "kafolat",
    "daromad kafolat",
)
ASSISTANT_MARKERS_RU = ("ассистент",)
ASSISTANT_MARKERS_UZ = ("yordamchi", "assistant")

_MONEY_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:\$|usd|долл\w*)|\$\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_CAPS_WORD_RE = re.compile(r"\b[A-ZА-ЯЁ]{4,}\b")


def validate_draft(text: str, *, lang: str, kind: str = "outreach") -> list[str]:
    """Return a list of problems. Empty list means the draft is acceptable."""
    problems: list[str] = []
    body = text.strip()
    lowered = body.lower()

    if not body:
        return ["пустой текст"]
    if len(body) > MAX_LENGTH:
        problems.append(f"длиннее {MAX_LENGTH} символов")
    if "!" in body:
        problems.append("есть восклицательный знак")
    if _CAPS_WORD_RE.search(body):
        problems.append("есть слова капсом")
    if body.count("?") != 1 or not body.endswith("?"):
        problems.append("нужен ровно один вопрос в конце")
    if "dentalux" not in lowered:
        problems.append("не назван DentaLux")
    markers = ASSISTANT_MARKERS_UZ if lang == "uz" else ASSISTANT_MARKERS_RU
    if not any(m in lowered for m in markers):
        problems.append("не указано, что это ассистент")
    for phrase in FORBIDDEN_PHRASES:
        if phrase in lowered:
            problems.append(f"запрещённая формулировка: {phrase}")
    for match in _MONEY_RE.finditer(body):
        raw = match.group(1) or match.group(2)
        amount = float(raw.replace(",", "."))
        if int(amount) not in ALLOWED_AMOUNTS or amount != int(amount):
            problems.append("цена не из условий продукта")
            break
    if kind == "outreach" and _MONEY_RE.search(body):
        problems.append("в первом сообщении не должно быть цен")
    return problems


SYSTEM_PROMPT_RU = """Ты пишешь первое сообщение от имени DentaLux стоматологической клинике Ташкента.
DentaLux — Telegram-запись для стоматологий, напоминания с кнопками и панель администратора.

Жёсткие правила:
- Явно скажи, что это DentaLux и что ты ассистент. Не выдавай себя за человека.
- Упомяни конкретное направление или услугу клиники из заметки. Не выдумывай факты о клинике, которых нет в заметке.
- Не используй цены вообще. Не обещай выручку, гарантии, сроки внедрения.
- Не упоминай интеграции с CRM и медицинские данные пациентов.
- Без восклицательных знаков, без слов капсом, без спама.
- Длина до 600 символов. Ровно один вопрос, в самом конце.
- Призыв: короткий демо-звонок или 3-минутный просмотр.
- Ответь ТОЛЬКО текстом сообщения, без пояснений и кавычек."""

SYSTEM_PROMPT_UZ = """Siz DentaLux nomidan Toshkentdagi stomatologiya klinikasiga birinchi xabar yozasiz.
DentaLux — stomatologiyalar uchun Telegram orqali yozilish tizimi, tugmali eslatmalar va administrator paneli.

Qat'iy qoidalar:
- DentaLux ekanligingizni va siz yordamchi ekanligingizni aniq yozing. Odam sifatida ko'rsatmang.
- Yozuvdagi aniq yo'nalish yoki xizmatni eslatib o'ting. Yozuvda bo'lmagan faktlarni o'ylab topmang.
- Hech qanday narx yozmang. Daromad yoki kafolat va'da qilmang.
- Faqat LATIN yozuvida, o'zbek tilida yozing.
- Undov belgisi yo'q, KATTA HARFLAR bilan so'z yo'q.
- 600 belgigacha. Aynan bitta savol, oxirida.
- Chaqiruv: qisqa demo qo'ng'iroq yoki 3 daqiqalik ko'rish.
- Faqat xabar matnini qaytaring, izohsiz va tirnoqsiz."""


@dataclass(frozen=True)
class DraftRequest:
    clinic_name: str
    direction: str | None
    note: str | None
    lang: str = "ru"  # "ru" or "uz"
    kind: str = "outreach"  # "outreach" or "reply"
    instruction: str | None = None  # owner's «Переделай: …» wish
    previous_text: str | None = None
    clinic_reply: str | None = None  # clinic's answer (for kind == "reply")


@dataclass(frozen=True)
class GeneratedDraft:
    text: str
    lang: str
    source: str  # "claude" or "template"
    note: str | None = None  # shown to the owner, e.g. why the template was used


LLMCall = Callable[[str, str], Awaitable[str]]  # (system, user) -> text


class _ClientLike(Protocol):  # minimal surface of anthropic.AsyncAnthropic we use
    messages: object


def _user_prompt(req: DraftRequest) -> str:
    lines = [
        f"Клиника: {req.clinic_name}",
        f"Направление/услуги из заметки: {req.direction or 'не указано'}",
        f"Заметка: {req.note or 'нет'}",
        f"Тип сообщения: {'ответ на ответ клиники' if req.kind == 'reply' else 'первое сообщение'}",
    ]
    if req.kind == "reply" and req.clinic_reply:
        lines.append(f"Что ответила клиника: {req.clinic_reply}")
    if req.previous_text:
        lines.append(f"Предыдущая версия: {req.previous_text}")
    if req.instruction:
        lines.append(f"Пожелание владельца: {req.instruction}")
    return "\n".join(lines)


def _template(req: DraftRequest) -> str:
    name = req.clinic_name
    direction = req.direction or "стоматология"
    if req.lang == "uz":
        if req.kind == "reply":
            return (
                f"Javobingiz uchun rahmat, {name}. Men DentaLux yordamchisiman. "
                "Qisqa demo yoki 3 daqiqalik ko'rish sizga qulaymi?"
            )
        return (
            f"Assalomu alaykum, {name}. Men DentaLux yordamchisiman. "
            f"Biz klinikalar uchun Telegram orqali onlayn yozilish tizimini taklif qilamiz "
            f"(yo'nalish: {direction}). Bemorlar uchun eslatmalar tugmalar bilan, "
            "administrator paneli ham bor. Qisqa demo qo'ng'iroq yoki 3 daqiqalik ko'rish sizga qiziqarlimi?"
        )
    if req.kind == "reply":
        return (
            f"Спасибо за ответ, {name}. Это ассистент DentaLux. "
            "Можем показать демо-версию записи за 3 минуты или созвониться коротко. Что удобнее?"
        )
    return (
        f"Здравствуйте. Это ассистент DentaLux, мы работаем с клиниками по онлайн-записи. "
        f"Увидели, что вы ведёте направление: {direction}. "
        "DentaLux — это запись через Telegram, напоминания пациентам с кнопками и панель администратора. "
        "Вам было бы интересно посмотреть 3-минутный просмотр или короткий демо-звонок?"
    )


class DraftGenerator:
    """Builds drafts with Claude when possible, otherwise with templates.

    `llm` is injectable for tests: an async function (system, user) -> text.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "claude-sonnet-5-5",
        llm: LLMCall | None = None,
    ) -> None:
        self._model = model
        self._llm: LLMCall | None = llm
        if self._llm is None and api_key:
            self._llm = self._make_anthropic_call(api_key)

    @property
    def llm_enabled(self) -> bool:
        return self._llm is not None

    def _make_anthropic_call(self, api_key: str) -> LLMCall:
        from anthropic import AsyncAnthropic  # imported lazily so tests without the SDK still run

        client = AsyncAnthropic(api_key=api_key)
        model = self._model

        async def call(system: str, user: str) -> str:
            response = await client.messages.create(
                model=model,
                max_tokens=700,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
            return "".join(parts).strip()

        return call

    async def generate(self, req: DraftRequest) -> GeneratedDraft:
        if self._llm is None:
            return self._fallback(req, note="ключ ANTHROPIC_API_KEY не задан, показан шаблон")

        system = SYSTEM_PROMPT_UZ if req.lang == "uz" else SYSTEM_PROMPT_RU
        try:
            text = await self._llm(system, _user_prompt(req))
        except Exception as exc:  # network, auth, rate limit: never crash the bot
            logger.warning("draft generation via API failed: %s", type(exc).__name__)
            return self._fallback(req, note="Claude API недоступен, показан шаблон")

        problems = validate_draft(text, lang=req.lang, kind=req.kind)
        if problems:
            logger.warning("API draft rejected by validator: %s", "; ".join(problems))
            return self._fallback(req, note="ответ модели не прошёл проверку, показан шаблон")
        return GeneratedDraft(text=text.strip(), lang=req.lang, source="claude")

    def _fallback(self, req: DraftRequest, note: str) -> GeneratedDraft:
        text = _template(req)
        # Templates are checked too, so a bad edit to them fails tests rather than production.
        problems = validate_draft(text, lang=req.lang, kind=req.kind)
        if problems:  # pragma: no cover - guarded by tests
            raise RuntimeError(f"template is invalid: {problems}")
        return GeneratedDraft(text=text, lang=req.lang, source="template", note=note)
