"""Приём апдейтов Telegram в режиме webhook. Взято из concert-pnl."""

import logging
import secrets
from typing import Annotated

from aiogram import Bot, Dispatcher
from aiogram.methods import TelegramMethod
from aiogram.types import Update
from fastapi import APIRouter, Header, Request, Response, status

from zayavochka.config import WEBHOOK_PATH

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> Response:
    secret: str = request.app.state.webhook_secret
    if not secrets.compare_digest((x_telegram_bot_api_secret_token or "").encode(), secret.encode()):
        logger.warning("Запрос на webhook с неверным секретом")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    bot: Bot = request.app.state.bot
    dp: Dispatcher = request.app.state.dp
    # Дальше всегда 200: на ошибку Telegram ответит повтором того же апдейта, и он застрянет в цикле
    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
    except Exception:
        logger.exception("Не удалось разобрать апдейт")
        return Response(status_code=status.HTTP_200_OK)
    try:
        result = await dp.feed_webhook_update(bot, update)
        if isinstance(result, TelegramMethod):
            await dp.silent_call_request(bot, result)
    except Exception as e:
        # Трейсбек aiogram уже записал сам — без exc_info, чтобы не дублировать
        logger.warning("Апдейт %s обработан с ошибкой: %r", update.update_id, e)
    return Response(status_code=status.HTTP_200_OK)
