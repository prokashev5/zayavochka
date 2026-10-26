"""Сценарии бота через настоящий Dispatcher aiogram; запросы к Telegram подменены."""

import asyncio
import itertools
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import CopyMessage, EditMessageText, SendDocument, SendMessage
from aiogram.types import Message, Update
from pydantic import SecretStr

from zayavochka.bot import form
from zayavochka.bot.keyboards import BACK, SKIP, Decision, Preview, Reopen, Reply
from zayavochka.bot.runner import build_dispatcher
from zayavochka.config import Settings
from zayavochka.core.application import QUESTIONS, Applicant, Status, render_application

TOKEN = "123456:TEST-token"
BOT_ID = 123456
BOOKERS = -1001234567890
BAND_USER = {"id": 42, "is_bot": False, "first_name": "Иван", "username": "ivan"}
BOOKER_USER = {"id": 7, "is_bot": False, "first_name": "Ольга"}

ANSWERS = {
    "band": "Кошка Шрёдингера",
    "city": "Петербург",
    "genre": "пост-панк",
    "music": "https://music.yandex.ru/artist/1",
    "dates": "пятницы в декабре",
    "guests": "40",
    "lineup": "4 человека",
}


class Harness:
    """Гоняет апдейты через диспетчер и записывает, что бот отправил в Telegram."""

    def __init__(self, monkeypatch, booker_chat_id=BOOKERS):
        self.settings = Settings(
            _env_file=None, bot_token=SecretStr(TOKEN), booker_chat_id=booker_chat_id, bot_mode="off"
        )
        self.dp = build_dispatcher(self.settings)
        self.bot = Bot(token=TOKEN)
        self.calls: list = []
        self.ids = itertools.count(1000)
        self.update_ids = itertools.count(1)
        self.fail_on = None
        monkeypatch.setattr(Bot, "__call__", AsyncMock(side_effect=self._api))
        self.loop = asyncio.new_event_loop()

    async def _api(self, method, *args, **kwargs):
        # fail_on — предикат: какие запросы к Telegram должны упасть
        if self.fail_on and self.fail_on(method):
            raise self.fail_on_error
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendDocument)):
            return Message.model_validate({
                "message_id": next(self.ids), "date": 0,
                "chat": {"id": method.chat_id, "type": "private"}, "text": getattr(method, "text", None),
            })
        if isinstance(method, CopyMessage):
            return {"message_id": next(self.ids)}
        return True

    def feed(self, payload):
        update = Update.model_validate({"update_id": next(self.update_ids), **payload}, context={"bot": self.bot})
        self.loop.run_until_complete(self.dp.feed_update(self.bot, update))
        return update

    def text(self, value, user=BAND_USER, chat=None):
        chat = chat or {"id": user["id"], "type": "private"}
        msg = {"message_id": next(self.ids), "date": 0, "chat": chat, "from": user, "text": value}
        if value.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(value.split()[0])}]
        return self.feed({"message": msg})

    def press(self, data, message, user=BAND_USER):
        return self.feed({"callback_query": {
            "id": str(next(self.ids)), "from": user, "chat_instance": "x", "data": data, "message": message,
        }})

    def sent(self, kind=SendMessage, chat_id=None):
        return [c for c in self.calls if isinstance(c, kind) and (chat_id is None or c.chat_id == chat_id)]

    def last_text(self):
        return self.sent()[-1].text

    def fill_form(self):
        self.text("/start")
        for question in QUESTIONS:
            self.text(ANSWERS.get(question.key, SKIP))
        self.text(SKIP)  # райдер

    def preview_message(self):
        return {"message_id": 1, "date": 0, "chat": {"id": BAND_USER["id"], "type": "private"},
                "from": {"id": BOT_ID, "is_bot": True, "first_name": "Заявочка"}, "text": "Проверь заявку"}


def card_message(status=Status.NEW, message_id=500, chat_id=BOOKERS):
    text, entities = render_application(
        ANSWERS, Applicant(user_id=42, full_name="Иван", username="ivan"), status, "Клуб Гараж"
    ).render()
    return {
        "message_id": message_id, "date": 0, "chat": {"id": chat_id, "type": "supergroup", "title": "Букеры"},
        "from": {"id": BOT_ID, "is_bot": True, "first_name": "Заявочка"},
        "text": text, "entities": [e.model_dump(exclude_none=True) for e in entities],
    }


@pytest.fixture
def h(monkeypatch):
    harness = Harness(monkeypatch)
    yield harness
    harness.loop.close()


class TestForm:
    def test_full_flow_delivers_card_to_bookers(self, h):
        h.fill_form()
        assert "Проверь заявку" in h.last_text()
        h.press(Preview(action="send").pack(), h.preview_message())

        [card] = h.sent(chat_id=BOOKERS)
        assert "🆕 Новая заявка" in card.text
        assert "Кошка Шрёдингера" in card.text
        assert "ID заявителя: 42" in card.text
        buttons = [b.callback_data for b in card.reply_markup.inline_keyboard[0]]
        assert buttons == [Decision(action=a, uid=42).pack() for a in ("ok", "no", "ask")]
        assert "Заявка отправлена" in h.last_text()

    def test_invalid_answer_keeps_step(self, h):
        h.text("/start")
        for question in QUESTIONS[:3]:
            h.text(ANSWERS[question.key])
        h.text("мы классные, без ссылок")
        assert "ссылка" in h.last_text()
        h.text("https://vk.com/band")
        assert h.last_text().startswith("5/")  # перешли к соцсетям

    def test_back_returns_to_previous_question(self, h):
        h.text("/start")
        h.text("Кошка")
        assert h.last_text().startswith("2/")
        h.text(BACK)
        assert h.last_text().startswith("1/")

    def test_skip_only_for_optional(self, h):
        h.text("/start")
        h.text(SKIP)  # название группы обязательно
        assert "Попробуй ещё раз" in h.last_text()

    def test_rider_document_forwarded_by_file_id(self, h):
        h.text("/start")
        for question in QUESTIONS:
            h.text(ANSWERS.get(question.key, SKIP))
        h.feed({"message": {
            "message_id": 77, "date": 0, "chat": {"id": 42, "type": "private"}, "from": BAND_USER,
            "document": {"file_id": "RIDER_FILE", "file_unique_id": "u1", "file_name": "rider.pdf"},
        }})
        h.press(Preview(action="send").pack(), h.preview_message())
        [doc] = h.sent(SendDocument)
        assert doc.document == "RIDER_FILE" and doc.chat_id == BOOKERS
        assert "Райдер: файлом ниже" in h.sent(chat_id=BOOKERS)[0].text

    def test_double_tap_sends_once(self, h):
        h.fill_form()
        h.press(Preview(action="send").pack(), h.preview_message())
        h.press(Preview(action="send").pack(), h.preview_message())
        assert len(h.sent(chat_id=BOOKERS)) == 1

    def test_cooldown_between_applications(self, h):
        h.fill_form()
        h.press(Preview(action="send").pack(), h.preview_message())
        h.fill_form()
        h.press(Preview(action="send").pack(), h.preview_message())
        assert len(h.sent(chat_id=BOOKERS)) == 1

    def test_delivery_failure_keeps_application(self, h):
        from aiogram.exceptions import TelegramForbiddenError

        h.fill_form()
        # Бота выгнали из чата букеров: доставка заявки падает, а ответы группе — нет
        h.fail_on = lambda m: isinstance(m, SendMessage) and m.chat_id == BOOKERS
        h.fail_on_error = TelegramForbiddenError(method=None, message="kicked")
        h.press(Preview(action="send").pack(), h.preview_message())
        assert "Не получилось доставить" in h.last_text()
        h.fail_on = None
        h.press(Preview(action="send").pack(), h.preview_message())  # повтор без перезаполнения
        assert len(h.sent(chat_id=BOOKERS)) == 1

    def test_no_booker_chat_configured(self, monkeypatch):
        harness = Harness(monkeypatch, booker_chat_id=None)
        harness.fill_form()
        harness.press(Preview(action="send").pack(), harness.preview_message())
        assert "не подключила чат" in harness.last_text()
        harness.loop.close()

    def test_text_without_form_suggests_start(self, h):
        h.text("привет")
        assert "/start" in h.last_text()

    def test_duplicate_update_processed_once(self, h):
        msg = {"message_id": 5, "date": 0, "chat": {"id": 42, "type": "private"}, "from": BAND_USER,
               "text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}
        update = Update.model_validate({"update_id": 999, "message": msg}, context={"bot": h.bot})
        h.loop.run_until_complete(h.dp.feed_update(h.bot, update))
        before = len(h.calls)
        h.loop.run_until_complete(h.dp.feed_update(h.bot, update))
        assert len(h.calls) == before


class TestBooker:
    def test_accept_updates_card_and_notifies_band(self, h):
        h.press(Decision(action="ok", uid=42).pack(), card_message(), user=BOOKER_USER)
        [edit] = h.sent(EditMessageText)
        assert "✅ Принята" in edit.text and "🆕" not in edit.text.split("\n")[0]
        assert "Ольга" in edit.text
        assert edit.reply_markup.inline_keyboard[0][0].callback_data == Reopen(uid=42).pack()
        [note] = h.sent(chat_id=42)
        assert "приняла заявку «Кошка Шрёдингера»" in note.text

    def test_decision_from_foreign_chat_rejected(self, h):
        h.press(Decision(action="ok", uid=42).pack(), card_message(chat_id=-100999), user=BOOKER_USER)
        assert not h.sent(EditMessageText) and not h.sent(chat_id=42)

    def test_stale_button_cannot_flip_decision(self, h):
        h.press(Decision(action="ok", uid=42).pack(), card_message(), user=BOOKER_USER)
        h.press(Decision(action="no", uid=42).pack(), card_message(), user=BOOKER_USER)  # устаревшая кнопка
        assert len(h.sent(EditMessageText)) == 1
        assert len(h.sent(chat_id=42)) == 1

    def test_reopen_allows_new_decision(self, h):
        h.press(Decision(action="ok", uid=42).pack(), card_message(), user=BOOKER_USER)
        h.press(Reopen(uid=42).pack(), card_message(Status.ACCEPTED), user=BOOKER_USER)
        h.press(Decision(action="no", uid=42).pack(), card_message(Status.ACCEPTED), user=BOOKER_USER)
        last_edit = h.sent(EditMessageText)[-1]
        assert "❌ Отклонена" in last_edit.text
        assert "✅" not in last_edit.text  # прежняя подпись заменена, а не накоплена

    def test_decider_name_is_escaped(self, h):
        evil = {**BOOKER_USER, "first_name": "<b>Хакер</b>"}
        h.press(Decision(action="no", uid=42).pack(), card_message(), user=evil)
        assert "&lt;b&gt;Хакер&lt;/b&gt;" in h.sent(EditMessageText)[0].text

    def test_ask_then_booker_reply_goes_to_band(self, h):
        h.press(Decision(action="ask", uid=42).pack(), card_message(), user=BOOKER_USER)
        assert "Уточняем" in h.sent(EditMessageText)[0].text
        h.feed({"message": {
            "message_id": 600, "date": 0, "chat": card_message()["chat"], "from": BOOKER_USER,
            "text": "Есть барабанщик?", "reply_to_message": card_message(Status.ASKED),
        }})
        header = h.sent(chat_id=42)[-1]
        assert "Вопрос от «Клуб Гараж» по заявке «Кошка Шрёдингера»" in header.text
        [copy] = h.sent(CopyMessage)
        assert copy.chat_id == 42 and copy.message_id == 600
        assert copy.reply_markup.inline_keyboard[0][0].callback_data == Reply(card=500).pack()

    def test_reply_to_non_card_ignored(self, h):
        other = {**card_message(), "from": BOOKER_USER, "text": "просто сообщение", "entities": []}
        h.feed({"message": {
            "message_id": 601, "date": 0, "chat": card_message()["chat"], "from": BOOKER_USER,
            "text": "ок", "reply_to_message": other,
        }})
        assert not h.sent(CopyMessage)

    def test_band_answers_clarification(self, h):
        h.press(Reply(card=500).pack(), h.preview_message())
        h.text("Да, есть!")
        [copy] = h.sent(CopyMessage)
        assert copy.chat_id == BOOKERS and copy.reply_parameters.message_id == 500
        assert "Ответ отправлен" in h.last_text()

    def test_chat_id_command(self, h):
        h.text("/chat_id", user=BOOKER_USER, chat={"id": BOOKERS, "type": "supergroup", "title": "Букеры"})
        assert str(BOOKERS) in h.last_text()


def test_cooldown_constant_is_reasonable():
    assert 60 <= form.SUBMIT_COOLDOWN_SECONDS <= 24 * 3600
