"""Жизненный цикл бота: polling (локально) или webhook (Render). Взято из concert-pnl."""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import TelegramObject, Update

from zayavochka.bot.booker import create_booker_router
from zayavochka.bot.form import create_form_router
from zayavochka.config import Settings

logger = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT_SECONDS = 10
RETRY_MIN_SECONDS = 1
RETRY_MAX_SECONDS = 60
SEEN_UPDATES = 1000


class _StopRetrying(Exception):
    """Ошибка, которую повтор не исправит: бот выключается до перезапуска процесса."""


class DropDuplicates(BaseMiddleware):
    """Telegram повторяет webhook, если не дождался ответа (холодный старт Render) —
    без этого группа получила бы два ответа, а букеры две одинаковые заявки."""

    def __init__(self) -> None:
        self.seen: deque[int] = deque(maxlen=SEEN_UPDATES)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            if event.update_id in self.seen:
                logger.info("Повтор апдейта %s пропущен", event.update_id)
                return None
            self.seen.append(event.update_id)
        return await handler(event, data)


def build_dispatcher(settings: Settings) -> Dispatcher:
    # Именованные аргументы диспетчера доступны хендлерам как параметры (settings, cooldowns)
    dp = Dispatcher(settings=settings, cooldowns={}, reply_cooldowns={})
    dp.update.outer_middleware(DropDuplicates())
    # Букерский роутер первым: /chat_id должен работать и в личке, до общего «нажми /start»
    dp.include_router(create_booker_router(settings))
    dp.include_router(create_form_router())
    return dp


def _log_task_exit(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    if exc := task.exception():
        logger.error("Бот остановился с ошибкой, сервер продолжает работать", exc_info=exc)


async def _retry_forever(name: str, attempt: Callable[[], Awaitable[None]]) -> None:
    """Повторяет attempt с растущей паузой: сеть при старте на хостинге бывает недоступна."""
    loop = asyncio.get_running_loop()
    delay = RETRY_MIN_SECONDS
    while True:
        started = loop.time()
        try:
            await attempt()
            return
        except TelegramUnauthorizedError:
            logger.error("Telegram отклонил BOT_TOKEN, бот выключен до перезапуска процесса")
            return
        except _StopRetrying as e:
            logger.error("%s", e)
            return
        except Exception:
            if loop.time() - started > RETRY_MAX_SECONDS:
                delay = RETRY_MIN_SECONDS
            logger.exception("%s: сбой, повтор через %s с", name, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, RETRY_MAX_SECONDS)


async def _stop_task(
    task: asyncio.Task[None], graceful: Callable[[], Awaitable[None]] | None = None
) -> None:
    if not task.done() and graceful is not None:
        try:
            await graceful()
        except RuntimeError:
            pass  # мягко останавливать нечего: polling не запущен (пауза перед повтором)
        else:
            await asyncio.wait({task}, timeout=SHUTDOWN_TIMEOUT_SECONDS)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def run_polling(settings: Settings) -> AsyncIterator[None]:
    bot = Bot(token=settings.bot_token.get_secret_value())
    dp = build_dispatcher(settings)

    async def attempt() -> None:
        # С висящим webhook getUpdates падает бесконечно. Снимать его не будем:
        # локальный запуск с боевым токеном молча сломал бы бота на хостинге
        if (await bot.get_webhook_info()).url:
            raise _StopRetrying(
                "На этом токене включён webhook — похоже, это токен бота с хостинга. "
                "Для локального запуска заведи отдельного бота в @BotFather."
            )
        await dp.start_polling(bot, handle_signals=False, close_bot_session=False)

    task = asyncio.create_task(_retry_forever("polling", attempt), name="bot-polling")
    task.add_done_callback(_log_task_exit)
    try:
        yield
    finally:
        await _stop_task(task, dp.stop_polling)
        await bot.session.close()


@asynccontextmanager
async def run_webhook(settings: Settings) -> AsyncIterator[tuple[Bot, Dispatcher]]:
    """При остановке webhook НЕ снимается: Render усыпляет сервис после 15 минут простоя,
    и снятый webhook значил бы, что бот больше никогда не проснётся."""
    bot = Bot(token=settings.bot_token.get_secret_value())
    dp = build_dispatcher(settings)
    await dp.emit_startup(bot=bot)  # в webhook-режиме это не делает за нас start_polling

    async def attempt() -> None:
        # drop_pending_updates не передаём: в очереди лежит апдейт, который нас разбудил
        await bot.set_webhook(
            url=settings.webhook_url,
            secret_token=settings.webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
        )
        logger.info("Webhook зарегистрирован: %s", settings.webhook_url)

    task = asyncio.create_task(_retry_forever("set_webhook", attempt), name="bot-webhook-setup")
    task.add_done_callback(_log_task_exit)
    try:
        yield bot, dp
    finally:
        await _stop_task(task)
        await dp.emit_shutdown(bot=bot)
        await bot.session.close()
