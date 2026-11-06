"""HTTP side: /health for Railway and the Telegram webhook endpoint."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

WEBHOOK_PATH = "/webhook"
ENGINE_KEY = web.AppKey("engine", AsyncEngine)


async def health(request: web.Request) -> web.Response:
    engine: AsyncEngine = request.app[ENGINE_KEY]
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return web.json_response({"status": "degraded", "database": "down"}, status=503)
    return web.json_response({"status": "ok", "database": "ok"})


def create_web_app(
    *,
    engine: AsyncEngine,
    bot: Bot,
    dispatcher: Dispatcher,
    webhook_enabled: bool,
    webhook_secret: str | None,
) -> web.Application:
    app = web.Application()
    app[ENGINE_KEY] = engine
    app.router.add_get("/health", health)
    if webhook_enabled:
        # aiogram rejects requests whose X-Telegram-Bot-Api-Secret-Token header does not match.
        SimpleRequestHandler(dispatcher=dispatcher, bot=bot, secret_token=webhook_secret).register(
            app, path=WEBHOOK_PATH
        )
    return app
