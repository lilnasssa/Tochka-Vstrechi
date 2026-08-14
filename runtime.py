"""[runtime] Точка встречи - телеграм-бот с карточкой профиля и анкетами (версия 2).

Всего четыре команды: /start, /profile, /search, /random.
Всё остальное живёт на инлайн-кнопках.

Запуск: python main.py  (или python bot.py)
Нужен BOT_TOKEN в переменных окружения, в .env или в token.txt
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

import card
import qr
import database as db_module
from database import (
    MAX_ANKETA_PHOTOS,
    ANKETA_TEXT_LIMIT,
    REACTION_MODES,
    REACTION_MODE_TITLES,
    REACTION_WORD_LIMIT,
    SOCIALS,
    SOCIAL_ORDER,
    Database,
    make_tag,
    social_link,
)

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("tochka")
BUILD = "v3-chat-rules-2026-08-14"

# Лимит Telegram на фото - 10 МБ, берем запас.
MAX_PHOTO_BYTES = 9 * 1024 * 1024
# По умолчанию 2k: такая карточка весит около 90 КБ и улетает мгновенно.
# Нужен 4k - поставьте CARD_SIZE=4k в переменных окружения.
CARD_SIZE = (os.getenv("CARD_SIZE") or "2k").strip().lower()
if CARD_SIZE not in card.CARD_SIZES:
    CARD_SIZE = "2k"
SIZE_CHAIN = {
    "4k": ("4k", "2k", "hd", "sd"),
    "2k": ("2k", "hd", "sd"),
    "hd": ("hd", "sd"),
    "sd": ("sd",),
}

TAG_RE = re.compile(r"^#?\d{5}$")
CLEAR_WORDS = {"-", "нет", "нету", "убрать", "удалить", "очистить", "skip", "clear"}
BOT_USERNAME = ""

EMOJI = {
    "banner": chr(0x1F5BC),
    "friends": chr(0x1F465),
    "settings": chr(0x2699),
    "theme": chr(0x1F3A8),
    "refresh": chr(0x1F504),
    "like": chr(0x1F44D),
    "dislike": chr(0x1F44E),
    "word": chr(0x1F4AC),
    "plus": chr(0x2795),
    "dice": chr(0x1F3B2),
    "clip": chr(0x1F4CE),
    "ok": chr(0x2705),
    "warn": chr(0x26A0),
    "stop": chr(0x1F6D1),
    "qr": chr(0x1F517),
    "scan": chr(0x1F4F7),
    "camera": chr(0x1F5BC),
    "card": chr(0x1FAAA),
    "anketa": chr(0x1F4C4),
    "pencil": chr(0x270F),
    "star": chr(0x2B50),
    "name": chr(0x1F3F7),
    "social": chr(0x1F310),
    "back": chr(0x2B05),
    "next": chr(0x27A1),
    "cross": chr(0x274C),
    "clock": chr(0x23F3),
    "person": chr(0x1F464),
    "bell": chr(0x1F514),
    "lock": chr(0x1F512),
    "unlock": chr(0x1F513),
    "chat": chr(0x1F4AC),
    "letter": chr(0x2709),
    "report": chr(0x1F6A9),
    "rules": chr(0x1F4DC),
    "share": chr(0x1F4E4),
    "search": chr(0x1F50D),
    "cake": chr(0x1F382),
    "gender": chr(0x26A7),
    "eye": chr(0x1F441),
    "note": chr(0x1F4DD),
    "sparkle": chr(0x2728),
    "hash": chr(0x0023) + chr(0xFE0F) + chr(0x20E3),
}

SOCIAL_EMOJI = {
    "tiktok": chr(0x1F3B5),
    "blink": chr(0x1F441),
    "telegram": chr(0x2708),
    "discord": chr(0x1F3AE),
    "instagram": chr(0x1F4F8),
    "vk": chr(0x1F535),
    "youtube": chr(0x25B6),
    "pinterest": chr(0x1F4CC),
}


# --------------------------------------------------------------------------- #
# Конфиг
# --------------------------------------------------------------------------- #
def read_token():
    token = (os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or "").strip()
    if token:
        return token
    for name in (".env", "token.txt"):
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if name == "token.txt":
                return line
            if "=" in line:
                key, value = line.split("=", 1)
                if key.strip() in ("BOT_TOKEN", "TOKEN"):
                    return value.strip().strip('"').strip("'")
    return ""


def resolve_db_path():
    """На bothost диск бывает только для чтения - аккуратно деградируем."""
    override = (os.getenv("DB_PATH") or "").strip()
    candidates = [Path(override)] if override else []
    candidates.append(ROOT / "data" / "bot.sqlite3")
    candidates.append(Path(tempfile.gettempdir()) / "tochka-bot.sqlite3")
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / (".write-test-%d" % os.getpid())
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return str(path)
        except OSError as error:
            LOG.warning("Путь %s не подходит для базы: %s", path, error)
    LOG.error("Нет записи на диск - база будет только в памяти!")
    return ":memory:"


# --------------------------------------------------------------------------- #
# Защита от второго экземпляра (TelegramConflictError)
# --------------------------------------------------------------------------- #
_lock_handle = None


def acquire_single_instance_lock(token):
    """Один процесс на токен в пределах машины."""
    global _lock_handle
    suffix = token.split(":")[0] if ":" in token else "bot"
    lock_path = Path(tempfile.gettempdir()) / ("tochka-%s.lock" % suffix)
    try:
        import fcntl
    except ImportError:  # Windows
        return True
    try:
        handle = open(lock_path, "w", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        LOG.critical(
            "%s На этой машине уже запущен бот с тем же токеном (файл %s). "
            "Второй экземпляр не запускаю.", EMOJI["stop"], lock_path,
        )
        return False
    _lock_handle = handle
    return True


class ConflictWatchdog(logging.Handler):
    """Гасит спам TelegramConflictError и выключает лишний экземпляр."""

    def __init__(self, limit=6):
        super().__init__(level=logging.ERROR)
        self.limit = limit
        self.hits = 0
        self.loop = None
        self.dispatcher = None

    def bind(self, loop, dispatcher):
        self.loop = loop
        self.dispatcher = dispatcher

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return
        if "TelegramConflictError" not in message and "Conflict" not in message:
            return
        self.hits += 1
        if self.hits == 1 or self.hits % 5 == 0:
            LOG.error(
                "%s Конфликт getUpdates (%d раз): тот же токен слушает еще один бот. "
                "Оставьте только один запущенный процесс.", EMOJI["warn"], self.hits,
            )
        if self.hits >= self.limit and self.loop and self.dispatcher:
            LOG.critical(
                "%s Конфликт не ушел за %d попыток - завершаю этот экземпляр, "
                "чтобы не мешать основному.", EMOJI["stop"], self.hits,
            )
            dispatcher = self.dispatcher
            self.dispatcher = None
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(dispatcher.stop_polling())
                )


class ConflictNoiseFilter(logging.Filter):
    """Не дает логам утонуть в повторах Sleep for ... try again."""

    def __init__(self):
        super().__init__()
        self.seen = 0

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        if "try again" not in message and "TelegramConflictError" not in message:
            return True
        self.seen += 1
        return self.seen <= 3 or self.seen % 10 == 0


async def preflight(bot):
    """Снимаем вебхук, проверяем токен и возвращаем данные бота.

    Возвращает объект бота (у него есть username и id) или None,
    если запускаться нельзя.
    """
    with contextlib.suppress(TelegramAPIError):
        await bot.delete_webhook(drop_pending_updates=True)

    try:
        me = await bot.get_me()
    except TelegramAPIError as error:
        LOG.critical("%s Телеграм не принял токен: %s", EMOJI["stop"], error)
        return None

    for attempt in range(1, 4):
        try:
            await bot.get_updates(offset=-1, timeout=0, limit=1)
            return me
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after)
        except TelegramAPIError as error:
            if "Conflict" not in str(error):
                LOG.warning("Проверка getUpdates: %s", error)
                return me
            LOG.warning(
                "%s Попытка %d/3: токен занят другим процессом, жду 5 секунд...",
                EMOJI["warn"], attempt,
            )
            await asyncio.sleep(5)

    LOG.critical(
        "%s Токен уже используется другим запущенным ботом.\n"
        "Что сделать:\n"
        "  1) На bothost остановите все копии проекта с этим токеном и запустите одну.\n"
        "  2) Закройте локальный python main.py, если ��н еще работает.\n"
        "  3) Если нужны две копии - сделайте второго бота у @BotFather.",
        EMOJI["stop"],
    )
    return None

