import pytest
from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.main import missing_settings
from app.web import WEBHOOK_PATH, create_web_app
from tests.test_bot_flow import BOT_TOKEN, FakeSession


def _settings(monkeypatch, **env) -> Settings:
    for key in [
        "BOT_TOKEN",
        "ADMIN_USER_ID",
        "DATABASE_URL",
        "PUBLIC_WEBHOOK_URL",
        "WEBHOOK_SECRET",
        "ANTHROPIC_API_KEY",
        "ALLOW_POLLING",
    ]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_defaults_match_the_brief(monkeypatch):
    s = _settings(monkeypatch)
    assert s.daily_send_limit == 10
    assert s.send_pause_seconds == 180
    assert s.anthropic_model == "claude-sonnet-5-5"
    assert s.anthropic_api_key is None


def test_blank_optional_variables_fall_back_to_defaults(monkeypatch):
    s = _settings(monkeypatch, DAILY_SEND_LIMIT="", SEND_PAUSE_SECONDS="", DATABASE_URL="", PORT="")
    assert s.daily_send_limit == 10
    assert s.send_pause_seconds == 180
    assert s.port == 8080
    assert s.database_url.startswith("sqlite+aiosqlite")


def test_railway_postgres_url_is_converted_to_asyncpg(monkeypatch):
    s = _settings(monkeypatch, DATABASE_URL="postgresql://u:p@host:5432/db")
    assert s.database_url == "postgresql+asyncpg://u:p@host:5432/db"
    s2 = _settings(monkeypatch, DATABASE_URL="postgres://u:p@host/db")
    assert s2.database_url.startswith("postgresql+asyncpg://")


def test_secrets_are_not_shown_in_repr(monkeypatch):
    s = _settings(monkeypatch, BOT_TOKEN="123:SECRETVALUE", ANTHROPIC_API_KEY="sk-SECRETVALUE")
    assert "SECRETVALUE" not in repr(s)
    assert "SECRETVALUE" not in str(s.model_dump())


def test_startup_reports_missing_names_only(monkeypatch):
    problems = missing_settings(_settings(monkeypatch))
    text = "; ".join(problems)
    assert "BOT_TOKEN" in text and "ADMIN_USER_ID" in text


def test_webhook_requires_valid_secret(monkeypatch):
    base = {"BOT_TOKEN": "123:abc", "ADMIN_USER_ID": "42", "PUBLIC_WEBHOOK_URL": "https://x.up.railway.app"}
    assert "WEBHOOK_SECRET" in "; ".join(missing_settings(_settings(monkeypatch, **base)))
    bad = _settings(monkeypatch, **base, WEBHOOK_SECRET="has spaces!")
    assert "WEBHOOK_SECRET" in "; ".join(missing_settings(bad))
    good = _settings(monkeypatch, **base, WEBHOOK_SECRET="abc_DEF-123")
    assert missing_settings(good) == []


def test_production_without_webhook_url_refuses_to_start_in_polling(monkeypatch):
    s = _settings(monkeypatch, BOT_TOKEN="123:abc", ADMIN_USER_ID="42")
    assert any("PUBLIC_WEBHOOK_URL" in p for p in missing_settings(s))


def test_polling_only_when_explicitly_allowed(monkeypatch):
    s = _settings(monkeypatch, BOT_TOKEN="123:abc", ADMIN_USER_ID="42", ALLOW_POLLING="1")
    assert missing_settings(s) == []


async def test_health_endpoint_reports_database_ok(engine):
    bot = Bot(token=BOT_TOKEN, session=FakeSession())
    app = create_web_app(engine=engine, bot=bot, dispatcher=Dispatcher(), webhook_enabled=False, webhook_secret=None)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/health")
        assert response.status == 200
        assert (await response.json())["status"] == "ok"


async def test_webhook_rejects_wrong_or_missing_secret(engine):
    bot = Bot(token=BOT_TOKEN, session=FakeSession())
    app = create_web_app(
        engine=engine,
        bot=bot,
        dispatcher=Dispatcher(),
        webhook_enabled=True,
        webhook_secret="correct-secret",
    )
    update = {"update_id": 1}
    async with TestClient(TestServer(app)) as client:
        missing = await client.post(WEBHOOK_PATH, json=update)
        wrong = await client.post(
            WEBHOOK_PATH, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}
        )
        # aiogram answers 401 both when the secret header is absent and when it is wrong
        assert missing.status == 401
        assert wrong.status == 401


@pytest.mark.parametrize("path", ["/", "/admin"])
async def test_unknown_paths_do_not_expose_anything(engine, path):
    bot = Bot(token=BOT_TOKEN, session=FakeSession())
    app = create_web_app(engine=engine, bot=bot, dispatcher=Dispatcher(), webhook_enabled=True, webhook_secret="s")
    async with TestClient(TestServer(app)) as client:
        response = await client.get(path)
        assert response.status == 404
