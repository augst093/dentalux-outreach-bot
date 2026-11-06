"""Entry point: `python -m app.main`.

Webhook mode (Railway): set PUBLIC_WEBHOOK_URL and WEBHOOK_SECRET.
Polling mode (local testing only): leave PUBLIC_WEBHOOK_URL empty and set ALLOW_POLLING=1.
Without either, startup stops with a message, so a misconfigured deploy fails loudly.
Either way, an HTTP server on PORT serves /health.
"""

from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiohttp import web

from app.bot.handlers import build_dispatcher
from app.config import Settings, get_settings
from app.db.session import make_engine, make_session_factory
from app.services.drafts import DraftGenerator
from app.services.limits import SendLimiter
from app.services.operator import Operator
from app.web import WEBHOOK_PATH, create_web_app

logger = logging.getLogger("dentalux.outreach")

_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def missing_settings(settings: Settings) -> list[str]:
    """Problems that must stop startup. Returns human-readable names only, never values."""
    problems: list[str] = []
    if not settings.bot_token or not settings.bot_token.get_secret_value().strip():
        problems.append("BOT_TOKEN")
    if settings.admin_user_id <= 0:
        problems.append("ADMIN_USER_ID (нужен числовой Telegram id владельца)")
    if settings.public_webhook_url:
        secret = settings.webhook_secret.get_secret_value() if settings.webhook_secret else ""
        if not _SECRET_RE.match(secret):
            problems.append("WEBHOOK_SECRET (1-256 символов: буквы, цифры, _ и -)")
    elif not settings.allow_polling:
        problems.append("PUBLIC_WEBHOOK_URL (для локальных тестов можно ALLOW_POLLING=1)")
    return problems


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # aiogram and HTTP libraries log update payloads and URLs at INFO/DEBUG; keep them quiet.
    for noisy in ("aiogram", "aiohttp", "httpx", "anthropic", "sqlalchemy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def run(settings: Settings) -> None:
    token = settings.bot_token.get_secret_value()  # type: ignore[union-attr]
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    api_key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    generator = DraftGenerator(api_key=api_key, model=settings.anthropic_model)
    if not generator.llm_enabled:
        logger.info("ANTHROPIC_API_KEY not set: drafts will use templates")
    limiter = SendLimiter(
        daily_limit=settings.daily_send_limit,
        pause_seconds=settings.send_pause_seconds,
        tz_name=settings.timezone,
    )

    def make_operator(session):  # noqa: ANN001 - AsyncSession, typed in operator
        return Operator(session, generator=generator, limiter=limiter)

    bot = Bot(token=token)
    dispatcher: Dispatcher = build_dispatcher(settings.admin_user_id, session_factory, make_operator)
    allowed_updates = dispatcher.resolve_used_update_types()

    webhook_enabled = bool(settings.public_webhook_url)
    webhook_secret = settings.webhook_secret.get_secret_value() if settings.webhook_secret else None
    app = create_web_app(
        engine=engine,
        bot=bot,
        dispatcher=dispatcher,
        webhook_enabled=webhook_enabled,
        webhook_secret=webhook_secret,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=settings.port).start()
    logger.info("HTTP server on port %s (health: /health)", settings.port)

    try:
        try:
            if webhook_enabled:
                await bot.set_webhook(
                    url=f"{settings.public_webhook_url}{WEBHOOK_PATH}",
                    secret_token=webhook_secret,
                    allowed_updates=allowed_updates,
                    drop_pending_updates=True,
                )
                logger.info("webhook set, serving updates")
                await asyncio.Event().wait()
            else:
                logger.warning("ALLOW_POLLING=1 and no PUBLIC_WEBHOOK_URL: long polling (local testing only)")
                await bot.delete_webhook(drop_pending_updates=True)
                await dispatcher.start_polling(bot, allowed_updates=allowed_updates)
        except TelegramUnauthorizedError as exc:
            # Never print the token: only say what to check.
            raise SystemExit("Telegram отклонил BOT_TOKEN. Проверьте значение переменной BOT_TOKEN.") from exc
    finally:
        await runner.cleanup()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    configure_logging()
    settings = get_settings()
    problems = missing_settings(settings)
    if problems:
        raise SystemExit("Не заданы переменные окружения: " + "; ".join(problems))
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
