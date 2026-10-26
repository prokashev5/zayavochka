import pytest

from zayavochka.core.application import (
    MAX_MESSAGE,
    QUESTION_BY_KEY,
    QUESTIONS,
    AnswerError,
    Applicant,
    Status,
    clean_answer,
    parse_applicant_id,
    render_application,
)

APPLICANT = Applicant(user_id=123456789, full_name="Иван Петров", username="ivan")
ANSWERS = {
    "band": "Кошка Шрёдингера",
    "city": "Петербург",
    "genre": "пост-панк",
    "music": "https://music.yandex.ru/artist/1",
    "dates": "пятницы в декабре",
    "guests": "40",
    "lineup": "4 человека: гитара, бас, барабаны, вокал",
}


def q(key):
    return QUESTION_BY_KEY[key]


class TestCleanAnswer:
    def test_strips_spaces(self):
        assert clean_answer(q("city"), "  Москва \n") == "Москва"

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_empty_rejected(self, raw):
        with pytest.raises(AnswerError, match="текстом"):
            clean_answer(q("band"), raw)

    def test_too_long(self):
        with pytest.raises(AnswerError, match="до 100"):
            clean_answer(q("band"), "я" * 101)

    @pytest.mark.parametrize("raw", ["https://vk.com/x", "слушайте t.me/band", "vk.com/band\nеще"])
    def test_music_needs_link(self, raw):
        assert clean_answer(q("music"), raw) == raw

    def test_music_without_link(self):
        with pytest.raises(AnswerError, match="ссылка"):
            clean_answer(q("music"), "мы классные")

    @pytest.mark.parametrize(("raw", "expected"), [("40", "40"), ("1 000", "1000"), ("007", "7"), ("0", "0")])
    def test_guests_number(self, raw, expected):
        assert clean_answer(q("guests"), raw) == expected

    @pytest.mark.parametrize("raw", ["сорок", "-5", "5001", "4.5"])
    def test_guests_invalid(self, raw):
        with pytest.raises(AnswerError):
            clean_answer(q("guests"), raw)


class TestCard:
    def test_contains_answers_status_and_applicant_id(self):
        text, _ = render_application(ANSWERS, APPLICANT, Status.NEW, "Клуб Гараж").render()
        assert "🆕 Новая заявка" in text
        assert "Клуб Гараж" in text
        assert "Кошка Шрёдингера" in text
        assert "Жанр: пост-панк" in text
        assert "@ivan" in text
        assert parse_applicant_id(text) == APPLICANT.user_id

    def test_band_links_to_applicant(self):
        card = render_application(ANSWERS, APPLICANT, Status.NEW, "Клуб Гараж")
        assert 'href="tg://user?id=123456789"' in card.as_html()

    def test_html_in_answers_is_escaped(self):
        answers = {**ANSWERS, "band": "<b>Злые</b> & Добрые", "genre": "<script>"}
        html_text = render_application(answers, APPLICANT, Status.NEW, "Клуб Гараж").as_html()
        assert "<script>" not in html_text
        assert "&lt;b&gt;Злые&lt;/b&gt; &amp; Добрые" in html_text

    def test_optional_answers_skipped(self):
        text, _ = render_application(ANSWERS, APPLICANT, Status.NEW, "Клуб Гараж").render()
        assert "Соцсети" not in text
        assert "Райдер" not in text

    def test_rider_mentioned(self):
        text, _ = render_application(ANSWERS, APPLICANT, Status.NEW, "Клуб Гараж", has_rider=True).render()
        assert "Райдер: файлом ниже" in text

    def test_longest_possible_card_fits_telegram_limit(self):
        # Все поля на максимуме, кириллицей и эмодзи (эмодзи — 2 единицы UTF-16, как считает Telegram)
        answers = {question.key: ("🎸" + "я" * question.max_len)[: question.max_len] for question in QUESTIONS}
        applicant = Applicant(user_id=10**12, full_name="Я" * 128, username="u" * 32)
        text, _ = render_application(answers, applicant, Status.ASKED, "Я" * 64, has_rider=True).render()
        assert len(text.encode("utf-16-le")) // 2 <= MAX_MESSAGE

    def test_parse_applicant_id_missing(self):
        assert parse_applicant_id("просто текст") is None
        assert parse_applicant_id(None) is None


class TestSettings:
    @staticmethod
    def make(**overrides):
        from pydantic import SecretStr

        from zayavochka.config import Settings

        return Settings(_env_file=None, bot_token=SecretStr("123456:TEST-token"), bot_mode="off", **overrides)

    def test_invite_link_instead_of_chat_id_explained(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="не ссылка"):
            self.make(booker_chat_id="https://t.me/+xxxxxxxx")

    @pytest.mark.parametrize(("raw", "expected"), [("", None), ("-1001234567890", -1001234567890)])
    def test_chat_id_parsing(self, raw, expected):
        assert self.make(booker_chat_id=raw).booker_chat_id == expected
