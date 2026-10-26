import hashlib
import os
from functools import lru_cache
from typing import Literal, Self

from aiogram.utils.token import TokenValidationError, validate_token
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Telegram шлёт апдейты POST-запросом сюда (в режиме webhook)
WEBHOOK_PATH = "/tg/webhook"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    bot_token: SecretStr
    # Нужен только для webhook. На Render приходит сам в RENDER_EXTERNAL_URL
    public_url: str | None = Field(
        default=None, validation_alias=AliasChoices("PUBLIC_URL", "RENDER_EXTERNAL_URL")
    )
    # Супергруппа букеров. None — бот работает, но заявки отправлять некуда (подскажет /chat_id)
    booker_chat_id: int | None = None
    venue_name: str = "Клуб Гараж"
    # polling — локально; webhook — на хостинге, который засыпает (Render); off — без бота (тесты).
    # На Render (там всегда RENDER=true) по умолчанию webhook: polling там засыпает навсегда
    bot_mode: Literal["polling", "webhook", "off"] = Field(
        default_factory=lambda: "webhook" if os.environ.get("RENDER") == "true" else "polling"
    )

    @field_validator("bot_token")
    @classmethod
    def _valid_token_format(cls, value: SecretStr) -> SecretStr:
        try:
            validate_token(value.get_secret_value())
        except TokenValidationError as e:
            raise ValueError("BOT_TOKEN не похож на токен от @BotFather") from e
        return value

    @field_validator("booker_chat_id", mode="before")
    @classmethod
    def _parse_chat_id(cls, value: object) -> object:
        if value == "":
            return None
        if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
            # Частая ошибка — вставить ссылку-приглашение вместо id
            raise ValueError(
                "BOOKER_CHAT_ID — это число вида -1001234567890, а не ссылка. "
                "Оставь его пустым, запусти бота и отправь /chat_id в группе букеров."
            )
        return value

    @field_validator("public_url")
    @classmethod
    def _require_https(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("Telegram принимает webhook только по https://")
        return value.rstrip("/") if value else value

    @model_validator(mode="after")
    def _webhook_needs_url(self) -> Self:
        if self.bot_mode == "webhook" and not self.public_url:
            raise ValueError("Для BOT_MODE=webhook нужен PUBLIC_URL (на Render — приходит сам)")
        return self

    @property
    def webhook_url(self) -> str:
        assert self.public_url, "webhook_url нужен только в режиме webhook"
        return self.public_url + WEBHOOK_PATH

    @property
    def webhook_secret(self) -> str:
        """Секрет для заголовка X-Telegram-Bot-Api-Secret-Token, выводится из токена.

        sha256 в hex укладывается в требования Telegram (1–256 символов A-Za-z0-9_-).
        """
        digest = hashlib.sha256(b"webhook:" + self.bot_token.get_secret_value().encode())
        return digest.hexdigest()


@lru_cache
def get_settings() -> Settings:
    return Settings()
