"""Точка входа: uvicorn zayavochka.app:create_app --factory

FastAPI здесь только хост для webhook и /health (Render проверяет, что сервис жив).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from zayavochka.bot import webhook
from zayavochka.bot.runner import run_polling, run_webhook
from zayavochka.config import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.booker_chat_id is None:
        logger.warning("BOOKER_CHAT_ID не задан: добавь бота в чат букеров и отправь там /chat_id")
    match settings.bot_mode:
        case "polling":
            async with run_polling(settings):
                yield
        case "webhook":
            async with run_webhook(settings) as (bot, dp):
                app.state.bot, app.state.dp = bot, dp
                app.state.webhook_secret = settings.webhook_secret
                yield
        case "off":
            yield


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    app = FastAPI(title="Заявочка", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if settings.bot_mode == "webhook":
        app.include_router(webhook.router)
    return app
