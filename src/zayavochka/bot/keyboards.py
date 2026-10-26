from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

BACK = "⬅️ Назад"
SKIP = "Пропустить ➡️"
CANCEL = "Отменить заявку"


class Decision(CallbackData, prefix="d"):
    """Кнопка букера под заявкой. uid — кому сообщить; проверяем чат перед доверием."""

    action: Literal["ok", "no", "ask"]
    uid: int


class Reopen(CallbackData, prefix="u"):
    """«Изменить решение» — вернуть кнопки решения."""

    uid: int


class Reply(CallbackData, prefix="r"):
    """Кнопка группы «Ответить площадке». card — id сообщения-заявки в чате букеров."""

    card: int


class Preview(CallbackData, prefix="p"):
    action: Literal["send", "restart"]


def form_keyboard(*, can_go_back: bool, can_skip: bool) -> ReplyKeyboardMarkup:
    row = []
    if can_go_back:
        row.append(KeyboardButton(text=BACK))
    if can_skip:
        row.append(KeyboardButton(text=SKIP))
    rows = [row] if row else []
    rows.append([KeyboardButton(text=CANCEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


REMOVE = ReplyKeyboardRemove()


def preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data=Preview(action="send").pack()),
        InlineKeyboardButton(text="🔄 Заново", callback_data=Preview(action="restart").pack()),
    ]])


def decision_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=Decision(action="ok", uid=uid).pack()),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=Decision(action="no", uid=uid).pack()),
        InlineKeyboardButton(text="❓ Уточнить", callback_data=Decision(action="ask", uid=uid).pack()),
    ]])


def reopen_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Изменить решение", callback_data=Reopen(uid=uid).pack()),
    ]])


def reply_keyboard(card: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Ответить площадке", callback_data=Reply(card=card).pack()),
    ]])
