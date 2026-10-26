"""Анкета заявки: вопросы, проверка ответов, текст карточки для букеров.

Чистая логика без бота: из aiogram только utils.formatting — он экранирует
пользовательский текст (название «<b>» или «&» не сломает сообщение).
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from aiogram.utils.formatting import Bold, Text, TextLink, as_key_value, as_line, as_list

MAX_MESSAGE = 4096  # лимит Telegram на текст сообщения
URL_RE = re.compile(r"(https?://|t\.me/|vk\.com/)\S+", re.IGNORECASE)
# Строка целиком: ответы анкеты и имя Telegram стоят выше и могут содержать подделку
APPLICANT_ID_RE = re.compile(r"^ID заявителя: (\d+)$", re.MULTILINE)


class AnswerError(ValueError):
    """Ответ не подходит; сообщение — для пользователя."""


@dataclass(frozen=True)
class Question:
    key: str
    label: str  # подпись в карточке заявки
    prompt: str  # вопрос в боте
    max_len: int
    optional: bool = False


QUESTIONS: tuple[Question, ...] = (
    Question("band", "Группа", "Как называется группа или проект?", 100),
    Question("city", "Город", "Из какого вы города?", 60),
    Question("genre", "Жанр", "Жанр — в двух-трёх словах. Например: «пост-панк, шугейз».", 100),
    Question(
        "music", "Музыка",
        "Ссылки на вашу музыку: Яндекс Музыка, VK, Bandcamp, YouTube. Можно несколько, каждую с новой строки.",
        600,
    ),
    Question("socials", "Соцсети", "Соцсети группы (VK, Telegram-канал). Если нет — нажми «Пропустить».", 300, True),
    Question("dates", "Даты", "Какие даты вам подходят? Можно диапазон: «пятницы в декабре» или «после 15 ноября».", 200),
    Question("guests", "Ожидаемо гостей", "Сколько гостей примерно приведёте? Числом, например 40.", 5),
    Question("lineup", "Состав", "Состав: сколько человек и какие инструменты?", 200),
    Question("contact", "Контакт", "Как ещё с вами связаться? Телефон или другой Telegram. Если не нужно — «Пропустить».", 100, True),
    Question("comment", "Комментарий", "Что ещё важно знать площадке? Райдер текстом, пожелания. Или «Пропустить».", 800, True),
)
QUESTION_BY_KEY = {q.key: q for q in QUESTIONS}
MAX_GUESTS = 5000


def clean_answer(question: Question, raw: str | None) -> str:
    """Проверяет и нормализует ответ. Бросает AnswerError с понятным текстом."""
    value = (raw or "").strip()
    if not value:
        raise AnswerError("Пришли ответ текстом.")
    if len(value) > question.max_len:
        raise AnswerError(f"Слишком длинно: {len(value)} символов, можно до {question.max_len}.")
    if question.key == "music" and not URL_RE.search(value):
        raise AnswerError("Нужна хотя бы одна ссылка на музыку — площадка будет слушать.")
    if question.key == "guests":
        digits = value.replace(" ", "")
        if not digits.isdigit() or int(digits) > MAX_GUESTS:
            raise AnswerError(f"Нужно число от 0 до {MAX_GUESTS}, например 40.")
        value = str(int(digits))
    return value


@dataclass(frozen=True)
class Applicant:
    user_id: int
    full_name: str
    username: str | None = None


class Status(StrEnum):
    NEW = "🆕 Новая заявка"
    ACCEPTED = "✅ Принята"
    DECLINED = "❌ Отклонена"
    ASKED = "❓ Уточняем детали"


def render_application(
    answers: dict[str, str],
    applicant: Applicant,
    status: Status,
    venue: str,
    has_rider: bool = False,
) -> Text:
    """Карточка заявки для чата букеров.

    Строка «ID заявителя» — источник истины для ответа группе: БД нет, и после
    засыпания сервиса бот берёт id прямо из этого сообщения.
    """
    sender = applicant.full_name + (f" (@{applicant.username})" if applicant.username else "")
    lines: list = [
        as_line(Bold(status), " · ", venue),
        as_line(Bold("🎸 "), TextLink(answers["band"], url=f"tg://user?id={applicant.user_id}")),
        "",
    ]
    for question in QUESTIONS:
        if question.key == "band" or question.key not in answers:
            continue
        lines.append(as_key_value(question.label, answers[question.key]))
    if has_rider:
        lines.append(as_key_value("Райдер", "файлом ниже"))
    lines += ["", as_key_value("Отправил", sender), f"ID заявителя: {applicant.user_id}"]
    return as_list(*lines)


def parse_applicant_id(card_text: str | None) -> int | None:
    """id группы из карточки. Берём ПОСЛЕДНЕЕ совпадение: настоящая строка идёт после
    всех ответов анкеты, а в комментарий или имя можно вписать «ID заявителя: 555»."""
    matches = APPLICANT_ID_RE.findall(card_text or "")
    return int(matches[-1]) if matches else None


def preview_for_applicant(answers: dict[str, str], has_rider: bool) -> Text:
    lines: list = [Bold("Проверь заявку:"), ""]
    for question in QUESTIONS:
        if question.key in answers:
            lines.append(as_key_value(question.label, answers[question.key]))
    lines.append(as_key_value("Райдер", "прикреплён" if has_rider else "нет"))
    return as_list(*lines)
