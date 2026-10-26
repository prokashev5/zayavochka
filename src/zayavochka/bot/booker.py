"""Сторона площадки: решения по заявке в чате букеров, уточнения через reply, /chat_id.

БД нет: всё нужное лежит в самой карточке-заявке (статус в первой строке, id группы
в строке «ID заявителя»), поэтому бот переживает засыпание и рестарты.
"""

import asyncio
import html
import logging
import re
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from zayavochka.bot.keyboards import Decision, Reopen, decision_keyboard, reopen_keyboard, reply_keyboard
from zayavochka.config import Settings
from zayavochka.core.application import Status, parse_applicant_id

logger = logging.getLogger(__name__)

STATUS_BY_ACTION = {"ok": Status.ACCEPTED, "no": Status.DECLINED, "ask": Status.ASKED}
BAND_RE = re.compile(r"🎸 (.+)")


def band_of(card_text: str | None) -> str:
    match = BAND_RE.search(card_text or "")
    return match[1].strip() if match else "ваша заявка"


DECIDER_LINE = re.compile(r"\n<i>(?:" + "|".join(re.escape(s.value) for s in Status) + r") — .*</i>$")


def with_status(card_html: str, status: Status, decider: str) -> str:
    """Меняет статус в первой строке и подпись «кто решил» в конце; остальное не трогает.

    decider приходит из Telegram (имя букера) — экранируем, это HTML.
    """
    first, _, rest = card_html.partition("\n")
    for old in Status:
        first = first.replace(old.value, status.value)
    rest = DECIDER_LINE.sub("", rest)  # прежнее решение заменяем, а не копим
    return f"{first}\n{rest}\n<i>{status.value} — {html.escape(decider)}</i>"


def applicant_message(status: Status, venue: str, band: str) -> str | None:
    match status:
        case Status.ACCEPTED:
            return f"🎉 «{venue}» приняла заявку «{band}»! Букеры скоро свяжутся с вами насчёт даты."
        case Status.DECLINED:
            return f"«{venue}» пока не готова взять «{band}». Спасибо за заявку — пробуйте ещё!"
    return None


def create_booker_router(settings: Settings) -> Router:
    router = Router(name="booker")
    locks: dict[int, asyncio.Lock] = {}
    # Заявки с окончательным решением: менять только через «Изменить решение».
    # Устаревшая кнопка у второго букера иначе молча перевернула бы решение.
    # В памяти: после рестарта сбросится, но гонка двух нажатий живёт секунды
    decided: set[int] = set()

    def in_booker_chat(chat_id: int) -> bool:
        return settings.booker_chat_id is not None and chat_id == settings.booker_chat_id

    @router.message(Command("chat_id"))
    async def chat_id(message: Message) -> None:
        # id чата не секрет: команда нужна, чтобы прописать BOOKER_CHAT_ID при настройке
        await message.answer(f"ID этого чата: <code>{message.chat.id}</code>", parse_mode="HTML")

    @router.callback_query(Decision.filter())
    async def decide(callback: CallbackQuery, callback_data: Decision, bot: Bot) -> None:
        # callback_data присылает клиент: доверяем uid только из чата букеров
        if callback.message is None or not in_booker_chat(callback.message.chat.id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        card = callback.message
        # «Решено» проверяем до создания замка: иначе каждое нажатие оставляло бы замок в памяти
        if card.message_id in decided:
            await callback.answer("Решение уже принято. Чтобы поменять — «Изменить решение».", show_alert=True)
            return
        status = STATUS_BY_ACTION[callback_data.action]
        final = status is not Status.ASKED
        if not final and Status.ASKED.value in (card.text or "").partition("\n")[0]:
            await callback.answer("Уже ждём ответа группы — ответьте реплаем на заявку.")
            return
        lock = locks.setdefault(card.message_id, asyncio.Lock())
        if lock.locked():
            await callback.answer("Другой букер уже решает эту заявку.", show_alert=True)
            return
        keyboard = reopen_keyboard(callback_data.uid) if final else decision_keyboard(callback_data.uid)
        try:
            async with lock:
                await card.edit_text(
                    with_status(card.html_text, status, callback.from_user.full_name),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                # Решённой считаем только после того, как карточка реально обновилась
                if final:
                    decided.add(card.message_id)
        except TelegramAPIError:
            logger.exception("Не удалось обновить карточку %s", card.message_id)
            await callback.answer("Не удалось обновить заявку, попробуйте ещё раз.", show_alert=True)
            return
        finally:
            locks.pop(card.message_id, None)

        # Уведомление — вне лока: сеть до группы не должна задерживать решение
        if not final:
            await callback.answer()
            await card.reply("Ответьте <b>реплаем на заявку</b> — текст уйдёт группе.", parse_mode="HTML")
            return
        try:
            await bot.send_message(callback_data.uid, applicant_message(status, settings.venue_name, band_of(card.text)))
            await callback.answer("Группа уведомлена.")
        except TelegramForbiddenError:
            await callback.answer("Группа заблокировала бота — уведомление не доставлено.", show_alert=True)
        except TelegramAPIError:
            logger.exception("Не удалось уведомить группу %s", callback_data.uid)
            await callback.answer("Не удалось уведомить группу.", show_alert=True)

    @router.callback_query(Reopen.filter())
    async def reopen(callback: CallbackQuery, callback_data: Reopen) -> None:
        if callback.message is None or not in_booker_chat(callback.message.chat.id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        decided.discard(callback.message.message_id)
        await callback.answer("Выберите новое решение.")
        with suppress(TelegramBadRequest):  # двойной тап: кнопки уже такие
            await callback.message.edit_reply_markup(reply_markup=decision_keyboard(callback_data.uid))

    @router.message(F.reply_to_message, F.chat.func(lambda chat: in_booker_chat(chat.id)))
    async def ask_band(message: Message, bot: Bot) -> None:
        card = message.reply_to_message
        if card.from_user is None or card.from_user.id != bot.id:
            return  # обычный разговор букеров между собой
        uid = parse_applicant_id(card.text)
        if uid is None:
            # Реплай на служебное сообщение бота: молчать нельзя — букер решит, что вопрос ушёл
            await message.reply("Чтобы написать группе, ответьте реплаем на карточку заявки или на подпись «💬 Ответ группы».")
            return
        band = band_of(card.text)
        try:
            await bot.send_message(uid, f"❓ Вопрос от «{settings.venue_name}» по заявке «{band}»:")
            await bot.copy_message(
                uid, from_chat_id=message.chat.id, message_id=message.message_id,
                reply_markup=reply_keyboard(card.message_id),
            )
        except TelegramForbiddenError:
            await message.reply("Группа заблокировала бота — вопрос не доставлен.")
            return
        except TelegramAPIError:
            logger.exception("Не удалось переслать вопрос группе %s", uid)
            await message.reply("Не удалось переслать вопрос группе.")
            return
        await message.reply("✉️ Отправлено группе. Ответ придёт реплаем на заявку.")

    return router
