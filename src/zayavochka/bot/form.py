"""Сторона группы: пошаговая анкета, предпросмотр, отправка, ответ на уточнение площадки.

Состояние анкеты — в FSM (память процесса). Засыпание Render посреди анкеты её сбросит,
поэтому текст вне анкеты не игнорируем молча, а подсказываем /start.
"""

import logging
import time
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyParameters

from zayavochka.bot.keyboards import (
    BACK,
    CANCEL,
    REMOVE,
    SKIP,
    Preview,
    Reply,
    decision_keyboard,
    form_keyboard,
    preview_keyboard,
)
from zayavochka.config import Settings
from zayavochka.core.application import (
    QUESTIONS,
    AnswerError,
    Applicant,
    Status,
    clean_answer,
    preview_for_applicant,
    render_application,
)

logger = logging.getLogger(__name__)

# Не чаще одной заявки от человека за это время (в памяти: после рестарта сбрасывается — ок)
SUBMIT_COOLDOWN_SECONDS = 10 * 60
# Ответы на уточнения площадки — не чаще раза в минуту, чтобы не заспамить чат букеров
REPLY_COOLDOWN_SECONDS = 60
TOTAL_STEPS = len(QUESTIONS) + 1  # + шаг с райдером


def _has_button(message: Message, data: str | None) -> bool:
    markup = message.reply_markup
    if markup is None or data is None:
        return False
    return any(button.callback_data == data for row in markup.inline_keyboard for button in row)


class Form(StatesGroup):
    answering = State()
    rider = State()
    preview = State()
    replying = State()  # группа пишет ответ на уточнение площадки


async def _ask(message: Message, state: FSMContext, step: int) -> None:
    question = QUESTIONS[step]
    await state.set_state(Form.answering)
    await state.update_data(step=step)
    await message.answer(
        f"{step + 1}/{TOTAL_STEPS}. {question.prompt}",
        reply_markup=form_keyboard(can_go_back=step > 0, can_skip=question.optional),
    )


async def _ask_rider(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.rider)
    await message.answer(
        f"{TOTAL_STEPS}/{TOTAL_STEPS}. Есть технический райдер? Пришли его файлом "
        "(скрепка → Файл, PDF или документ). Если нет — «Пропустить».",
        reply_markup=form_keyboard(can_go_back=True, can_skip=True),
    )


async def _show_preview(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.preview)
    data = await state.get_data()
    await message.answer("Почти готово!", reply_markup=REMOVE)
    preview = preview_for_applicant(data["answers"], has_rider=bool(data.get("rider")))
    await message.answer(**preview.as_kwargs(), reply_markup=preview_keyboard())


async def _start_form(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    await state.update_data(answers={})
    await message.answer(
        f"Привет! Это бот заявок площадки «{settings.venue_name}».\n\n"
        f"Ответь на {TOTAL_STEPS} коротких вопросов — займёт пару минут. Заявка уйдёт букерам, "
        "а их ответ придёт сюда."
    )
    await _ask(message, state, 0)


def create_form_router() -> Router:
    """Новый роутер на каждый диспетчер: aiogram не даёт подключить один роутер дважды."""
    router = Router(name="form")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext, settings: Settings) -> None:
        await _start_form(message, state, settings)

    @router.message(StateFilter(Form.answering, Form.rider, Form.preview), F.text == CANCEL)
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Заявка отменена. Начать заново — /start", reply_markup=REMOVE)

    @router.message(Form.answering)
    async def answer(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        step: int = data["step"]
        answers: dict[str, str] = data["answers"]
        question = QUESTIONS[step]

        if message.text == BACK:
            await _ask(message, state, max(step - 1, 0))
            return
        if message.text == SKIP:
            # Текст кнопки не должен стать ответом — например, названием группы
            if not question.optional:
                await message.answer("Этот вопрос обязательный — его не пропустить. Попробуй ещё раз.")
                return
            answers.pop(question.key, None)
        else:
            try:
                answers[question.key] = clean_answer(question, message.text)
            except AnswerError as e:
                await message.answer(f"{e} Попробуй ещё раз.")
                return
        await state.update_data(answers=answers)
        if step + 1 < len(QUESTIONS):
            await _ask(message, state, step + 1)
        else:
            await _ask_rider(message, state)

    @router.message(Form.rider)
    async def rider(message: Message, state: FSMContext) -> None:
        if message.text == BACK:
            await _ask(message, state, len(QUESTIONS) - 1)
            return
        if message.text == SKIP:
            await state.update_data(rider=None)
        elif message.document and not message.media_group_id:
            # Храним только file_id: файл не скачиваем, площадке пересылаем по id
            await state.update_data(rider=message.document.file_id)
        else:
            await message.answer("Пришли райдер одним файлом (скрепка → Файл) или нажми «Пропустить».")
            return
        await _show_preview(message, state)

    @router.callback_query(Preview.filter(F.action == "restart"))
    async def restart(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
        await callback.answer()
        with suppress(TelegramBadRequest):  # двойной тап: кнопки уже убраны
            await callback.message.edit_reply_markup(reply_markup=None)
        await _start_form(callback.message, state, settings)

    @router.message(Form.preview)
    async def preview_hint(message: Message) -> None:
        # Клавиатуры анкеты здесь уже нет; /start сбросил бы заполненную заявку
        await message.answer("Нажми «✅ Отправить» или «🔄 Заново» под заявкой выше.")

    @router.callback_query(Preview.filter(F.action == "send"))
    async def send(
        callback: CallbackQuery, state: FSMContext, bot: Bot, settings: Settings, cooldowns: dict[int, float]
    ) -> None:
        # Проверка состояния и clear() идут без await-пауз между ними — двойной тап не отправит дважды
        if await state.get_state() != Form.preview:
            await callback.answer("Эта заявка уже отправлена или сброшена.")
            return
        user = callback.from_user
        last = cooldowns.get(user.id)
        if last is not None and time.monotonic() - last < SUBMIT_COOLDOWN_SECONDS:
            wait = int((SUBMIT_COOLDOWN_SECONDS - (time.monotonic() - last)) // 60) + 1
            await callback.answer(f"Ты недавно отправлял заявку. Подожди ~{wait} мин.", show_alert=True)
            return
        data = await state.get_data()
        await state.clear()
        cooldowns[user.id] = time.monotonic()
        await callback.answer()

        if settings.booker_chat_id is None:
            logger.error("BOOKER_CHAT_ID не задан — заявку некуда отправить")
            await callback.message.answer("Площадка ещё не подключила чат для заявок. Попробуй позже.")
            cooldowns.pop(user.id, None)
            await state.set_state(Form.preview)
            await state.set_data(data)
            return

        applicant = Applicant(user_id=user.id, full_name=user.full_name, username=user.username)
        rider_id = data.get("rider")
        card_text = render_application(
            data["answers"], applicant, Status.NEW, settings.venue_name, has_rider=bool(rider_id)
        )
        try:
            card = await bot.send_message(
                settings.booker_chat_id, **card_text.as_kwargs(), reply_markup=decision_keyboard(user.id)
            )
        except TelegramAPIError:
            # Чат удалён, бот выгнан, группа стала супергруппой (сменился id) — env устарел
            logger.exception("Не удалось доставить заявку в чат букеров %s", settings.booker_chat_id)
            cooldowns.pop(user.id, None)
            await state.set_state(Form.preview)
            await state.set_data(data)
            await callback.message.answer("Не получилось доставить заявку площадке. Нажми «Отправить» ещё раз чуть позже.")
            return

        # Карточка уже у букеров: сбой райдера — не повод слать заявку повторно (будет дубль)
        if rider_id:
            try:
                await bot.send_document(
                    settings.booker_chat_id, document=rider_id,
                    reply_parameters=ReplyParameters(message_id=card.message_id),
                )
            except TelegramAPIError:
                logger.exception("Райдер не прикрепился к заявке %s", card.message_id)
                with suppress(TelegramAPIError):
                    await bot.send_message(
                        settings.booker_chat_id, "⚠️ Райдер к этой заявке не прикрепился — попросите прислать заново.",
                        reply_parameters=ReplyParameters(message_id=card.message_id),
                    )
                await callback.message.answer("Заявка ушла, но райдер не прикрепился — площадка может попросить его отдельно.")

        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"🎉 Заявка отправлена в «{settings.venue_name}»! Ответ площадки придёт сюда. "
            "Новую заявку можно подать через /start."
        )

    @router.callback_query(Reply.filter())
    async def start_reply(
        callback: CallbackQuery, callback_data: Reply, state: FSMContext, bot: Bot,
        reply_cooldowns: dict[int, float],
    ) -> None:
        # callback_data присылает клиент: принимаем только кнопку, которую бот сам повесил на свой вопрос
        message = callback.message
        if message is None or message.from_user is None or message.from_user.id != bot.id or not _has_button(message, callback.data):
            await callback.answer("Эта кнопка недействительна.", show_alert=True)
            return
        if await state.get_state() in (Form.answering, Form.rider, Form.preview):
            await callback.answer("Сначала отправь или отмени заявку, которую заполняешь.", show_alert=True)
            return
        last = reply_cooldowns.get(callback.from_user.id)
        if last is not None and time.monotonic() - last < REPLY_COOLDOWN_SECONDS:
            await callback.answer("Подожди минуту перед следующим ответом.", show_alert=True)
            return
        await state.set_state(Form.replying)
        await state.update_data(card=callback_data.card)
        await callback.answer()
        await message.answer("Напиши ответ одним сообщением — перешлю площадке.", reply_markup=REMOVE)

    @router.message(Form.replying)
    async def send_reply(
        message: Message, state: FSMContext, bot: Bot, settings: Settings, reply_cooldowns: dict[int, float]
    ) -> None:
        data = await state.get_data()
        await state.clear()
        user = message.from_user
        reply_to = ReplyParameters(message_id=data["card"], allow_sending_without_reply=True)
        try:
            # Подпись: даже если карточку удалили, видно, чей это ответ
            await bot.send_message(
                settings.booker_chat_id,
                f"💬 Ответ группы от {user.full_name}\nID заявителя: {user.id}",
                reply_parameters=reply_to,
            )
            await bot.copy_message(
                settings.booker_chat_id, from_chat_id=message.chat.id, message_id=message.message_id,
                reply_parameters=reply_to,
            )
        except TelegramAPIError:
            logger.exception("Не удалось переслать ответ группы букерам")
            await message.answer("Не получилось переслать ответ. Нажми «Ответить площадке» ещё раз.")
            return
        reply_cooldowns[user.id] = time.monotonic()
        await message.answer("✅ Ответ отправлен площадке.")

    @router.message()
    async def fallback(message: Message) -> None:
        # Сюда попадают и анкеты, сброшенные засыпанием сервиса: подсказываем, а не молчим
        await message.answer("Чтобы подать заявку на выступление — нажми /start")

    return router
