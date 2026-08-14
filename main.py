"""Точка встречи - телеграм-бот с карточкой, анкетами и личными сообщениями.

Версия 3. Четыре команды: /start, /profile, /search, /random.
Всё остальное - инлайн-кнопки в одном сообщении профиля.
Анкеты, личные сообщения и уведомления идут отдельными сообщениями.

Запуск: python main.py  (или python bot.py)
Нужен BOT_TOKEN в переменных окружения, в .env или в token.txt
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import html
import logging
import os
import sys
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
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
import safety
import database as db_module
from database import (
    ANKETA_TEXT_LIMIT,
    CHAT_UNLOCK_SECONDS,
    CUSTOM_LABEL_LIMIT,
    MAX_AGE,
    MAX_ANKETA_PHOTOS,
    MIN_AGE,
    REACTION_MODES,
    REACTION_MODE_TITLES,
    REACTION_WORD_LIMIT,
    SOCIALS,
    SOCIAL_ORDER,
    Database,
    make_tag,
    social_link,
)
from runtime import (
    BUILD,
    CARD_SIZE,
    CLEAR_WORDS,
    EMOJI,
    LOG,
    MAX_PHOTO_BYTES,
    ROOT,
    SIZE_CHAIN,
    SOCIAL_EMOJI,
    TAG_RE,
    ConflictNoiseFilter,
    ConflictWatchdog,
    acquire_single_instance_lock,
    preflight,
    read_token,
    resolve_db_path,
)

BOT_USERNAME = ""


# --------------------------------------------------------------------------- #
# Состояния
# --------------------------------------------------------------------------- #
class Form(StatesGroup):
    name = State()
    rename = State()
    age = State()
    bio = State()
    avatar = State()
    banner = State()
    anketa_text = State()
    anketa_photo = State()
    social = State()
    like_word = State()
    dislike_word = State()
    reaction_word = State()
    qr_scan = State()


# --------------------------------------------------------------------------- #
# Мелкие помощники
# --------------------------------------------------------------------------- #
def _value(profile, key, default=None):
    if profile is None:
        return default
    try:
        value = profile[key]
    except (KeyError, IndexError, TypeError):
        value = getattr(profile, key, default)
    return default if value is None else value


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def display_name(profile) -> str:
    return str(_value(profile, "bot_name", "") or "Без имени")


def time_left_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours:
        return "%d ч %02d мин" % (hours, minutes)
    if minutes:
        return "%d мин" % minutes
    return "меньше минуты"


def meta_line(profile) -> str:
    """Цифра возраста и знак пола. Нет данных - нет строки."""
    parts = []
    age = Database.age_of(profile)
    if age:
        parts.append(str(age))
    gender = str(_value(profile, "gender", "") or "").lower()
    glyph = card.GENDER_GLYPHS.get(gender)
    if glyph:
        parts.append(glyph)
    return " ".join(parts)


def card_profile(profile) -> dict:
    """Данные для рисовалки: имя в боте вместо юзернейма."""
    name = _value(profile, "bot_name", "") or ""
    return {
        "display_name": name,
        "username": name,
        "bot_name": name,
        "tag": _value(profile, "tag", "") or make_tag(_value(profile, "user_id", 0)),
        "age": _value(profile, "age"),
        "gender": _value(profile, "gender", ""),
        "bio": _value(profile, "bio", ""),
        "theme": _value(profile, "theme", "blue"),
    }


# --------------------------------------------------------------------------- #
# Кэши и рендер карточки
#
# Главный секрет скорости: готовая карточка запоминается в телеграме как
# file_id. Пока профиль не менялся, бот пересылает готовую картинку без
# рисования и без загрузки - это десятки миллисекунд вместо двух секунд.
# --------------------------------------------------------------------------- #
_RENDER_LIMIT = asyncio.Semaphore(2)      # рисуем максимум две карточки разом
_FILE_CACHE = {}                          # file_id -> bytes (аватарки и баннеры)
_FILE_CACHE_ORDER = []
_FILE_CACHE_MAX_ITEMS = 24
_FILE_CACHE_MAX_BYTES = 12 * 1024 * 1024
_file_cache_bytes = 0


def _cache_get(file_id):
    data = _FILE_CACHE.get(file_id)
    if data is not None:
        with contextlib.suppress(ValueError):
            _FILE_CACHE_ORDER.remove(file_id)
        _FILE_CACHE_ORDER.append(file_id)
    return data


def _cache_put(file_id, data):
    global _file_cache_bytes
    if not data or len(data) > 4 * 1024 * 1024:
        return
    _FILE_CACHE[file_id] = data
    _FILE_CACHE_ORDER.append(file_id)
    _file_cache_bytes += len(data)
    while (_FILE_CACHE_ORDER and
           (len(_FILE_CACHE_ORDER) > _FILE_CACHE_MAX_ITEMS or
            _file_cache_bytes > _FILE_CACHE_MAX_BYTES)):
        oldest = _FILE_CACHE_ORDER.pop(0)
        dropped = _FILE_CACHE.pop(oldest, None)
        if dropped:
            _file_cache_bytes -= len(dropped)


def cache_forget(file_id):
    global _file_cache_bytes
    dropped = _FILE_CACHE.pop(file_id, None)
    if dropped:
        _file_cache_bytes -= len(dropped)
    with contextlib.suppress(ValueError):
        _FILE_CACHE_ORDER.remove(file_id)


def extract_image_file_id(message: Message):
    if message.photo:
        return message.photo[-1].file_id
    document = message.document
    if document and (document.mime_type or "").startswith("image/"):
        if (document.file_size or 0) <= MAX_PHOTO_BYTES:
            return document.file_id
    return None


async def download_file(bot: Bot, file_id):
    """Скачивает файл один раз и держит в памяти небольшой запас."""
    if not file_id:
        return None
    cached = _cache_get(file_id)
    if cached is not None:
        return cached
    try:
        info = await bot.get_file(file_id)
        if (info.file_size or 0) > MAX_PHOTO_BYTES:
            LOG.warning("Файл %s слишком большой (%s)", file_id, info.file_size)
            return None
        buffer = await bot.download_file(info.file_path)
        data = buffer.read() if hasattr(buffer, "read") else bytes(buffer)
    except TelegramAPIError as error:
        LOG.warning("Не скачать файл: %s", error)
        return None
    _cache_put(file_id, data)
    return data


def card_digest(profile, size: str) -> str:
    """Отпечаток всего, что видно на карточке."""
    parts = [
        BUILD, str(size),
        str(_value(profile, "bot_name", "")),
        str(_value(profile, "tag", "")),
        str(_value(profile, "age", "")),
        str(_value(profile, "gender", "")),
        str(_value(profile, "bio", "")),
        str(_value(profile, "theme", "")),
        str(_value(profile, "photo_file_id", "")),
        str(_value(profile, "banner_file_id", "")),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


async def render_card_bytes(bot: Bot, profile, size: str):
    """Рисует карточку. Память освобождается сразу после работы."""
    avatar = await download_file(bot, _value(profile, "photo_file_id"))
    banner = await download_file(bot, _value(profile, "banner_file_id"))
    data = card_profile(profile)
    chain = SIZE_CHAIN.get(size, (size,))
    payload = None
    try:
        async with _RENDER_LIMIT:
            for step in chain:
                payload = await asyncio.to_thread(
                    card.render_card, data, avatar, banner, step)
                if len(payload) <= MAX_PHOTO_BYTES:
                    if step != size:
                        LOG.info("Карточка сжата до %s (%d КБ)", step, len(payload) // 1024)
                    return payload
            # всё ещё тяжело - отдаём jpeg
            payload = await asyncio.to_thread(
                card.render_card, data, avatar, banner, chain[-1],
                None, None, None, "JPEG", 88)
            return payload
    finally:
        avatar = None
        banner = None
        gc.collect()


async def card_media(bot: Bot, profile):
    """Возвращает (что отправлять, отпечаток, из_кэша)."""
    user_id = int(_value(profile, "user_id", 0) or 0)
    digest = card_digest(profile, CARD_SIZE)
    cached = db.get_card_file_id(user_id, CARD_SIZE, digest) if user_id else None
    if cached:
        return cached, digest, True
    started = time.monotonic()
    payload = await render_card_bytes(bot, profile, CARD_SIZE)
    LOG.info("Карточка %s: %d КБ за %.2f с", CARD_SIZE,
             len(payload) // 1024, time.monotonic() - started)
    name = "tochka-%s-%s.png" % (_value(profile, "tag", "card"), CARD_SIZE)
    return BufferedInputFile(payload, filename=name), digest, False


def remember_card(profile, digest, message: Message):
    """Запоминает file_id только что отправленной карточки."""
    if not message or not message.photo:
        return
    user_id = int(_value(profile, "user_id", 0) or 0)
    if user_id:
        db.set_card_file_id(user_id, CARD_SIZE, digest, message.photo[-1].file_id)


def card_changed(user_id: int):
    """После любой правки карточки кэш больше не нужен."""
    db.drop_card_cache(user_id)


# --------------------------------------------------------------------------- #
# Тексты
# --------------------------------------------------------------------------- #
WELCOME = (
    "%s <b>Точка встречи</b>\n\n"
    "Здесь две разные вещи:\n"
    "%s <b>Профиль</b> - ваша карточка: имя, возраст, описание, аватарка, баннер.\n"
    "%s <b>Анкета</b> - то, что люди видят в поиске: текст и фото.\n\n"
    "Сначала занямите имя в боте - оно будет на карточке вместо юзернейма телеграма."
) % (EMOJI["sparkle"], EMOJI["card"], EMOJI["anketa"])

NAME_RULES = (
    "%s <b>Имя в боте</b>\n\n"
    "От 3 до 20 символов, латинские буквы, цифры и подчёркивание.\n"
    "Менять можно раз в 48 часов. Напишите желаемое имя одним сообщением."
) % EMOJI["name"]

RULES_TEXT = (
    "%s <b>Правила публикации анкет</b>\n\n"
    "1. Пишите о себе. Чужие фото и чужие данные запрещены.\n"
    "2. Возраст указывайте честно: от него зависит, кому покажут вашу анкету.\n"
    "3. Без интима, эротики и намёков на неё.\n"
    "4. Без рекламы, ссылок, продаж и просьб о деньгах.\n"
    "5. Без агрессии, угроз, травли и политической агитации.\n"
    "6. Соцсети - только в разделе Соцсети, в тексте анкеты ссылки не пройдут.\n"
    "7. Нарушения - анкета скрывается без предупреждения.\n\n"
    "Нажмите кнопку ниже, чтобы согласиться и публиковать анкету."
) % EMOJI["rules"]

HELP_HINT = (
    "%s Команды: /profile - ваш профиль, /random - анкеты, "
    "/search #12345 - поиск по хэшу."
) % EMOJI["search"]


def socials_lines(data: dict) -> list:
    lines = []
    for key in SOCIAL_ORDER:
        value = (data or {}).get(key)
        if not value:
            continue
        title = SOCIALS[key][0]
        link = social_link(key, value)
        icon = SOCIAL_EMOJI.get(key, EMOJI["social"])
        if link:
            lines.append('%s <a href="%s">%s</a>' % (icon, esc(link), esc(title)))
        else:
            lines.append("%s %s: %s" % (icon, esc(title), esc(value)))
    return lines


def profile_caption(profile, viewer_id=None, unlocked=False, chat_seconds=0) -> str:
    """Описание к карточке. Свой профиль показывает больше служебного."""
    user_id = int(_value(profile, "user_id", 0) or 0)
    own = viewer_id is None or int(viewer_id) == user_id
    head = "%s <b>%s</b> <code>#%s</code>" % (
        EMOJI["card"], esc(display_name(profile)),
        esc(_value(profile, "tag", "") or make_tag(user_id)))
    lines = [head]

    meta = meta_line(profile)
    if meta:
        lines.append(meta)

    bio = str(_value(profile, "bio", "") or "").strip()
    if bio:
        lines.append("<i>%s</i>" % esc(bio))

    stats = db.reaction_stats(user_id)
    like_word, dislike_word = Database.reaction_labels(profile)
    lines.append("")
    lines.append("%s %s %d %s %s %d %s %s %d" % (
        EMOJI["star"], EMOJI["like"], stats["like"],
        chr(0x00B7), EMOJI["dislike"], stats["dislike"],
        chr(0x00B7), EMOJI["word"], stats["word"]))

    if own:
        friends = db.count_friends(user_id)
        requests = db.count_incoming_requests(user_id)
        friends_line = "%s Друзья: %d" % (EMOJI["friends"], friends)
        if requests:
            friends_line += " (заявок: %d)" % requests
        lines.append(friends_line)
        anketa_ready = db.anketa_is_ready(profile)
        lines.append("%s Анкета: %s" % (
            EMOJI["anketa"], "опубликована" if anketa_ready else "не готова"))
        socials = db.get_socials(user_id)
        filled = len([1 for key in SOCIAL_ORDER if (socials or {}).get(key)])
        lines.append("%s Соцсети: %d из %d" % (EMOJI["social"], filled, len(SOCIAL_ORDER)))
        lines.append("%s Свои слова реакций: %s / %s" % (
            EMOJI["note"], esc(like_word), esc(dislike_word)))
    else:
        socials = db.get_socials(user_id)
        lines.append("")
        if unlocked:
            rows = socials_lines(socials)
            if rows:
                lines.append("%s <b>Соцсети</b>" % EMOJI["unlock"])
                lines.extend(rows)
            else:
                lines.append("%s Соцсети открыты, но человек их не указал." % EMOJI["unlock"])
            username = str(_value(profile, "tg_username", "") or "").strip()
            if username:
                lines.append("%s Телеграм: @%s" % (EMOJI["letter"], esc(username)))
        else:
            left = max(0, CHAT_UNLOCK_SECONDS - int(chat_seconds or 0))
            lines.append("%s Соцсети и телеграм откроются после 10 минут общения "
                         "в боте или сразу после добавления в друзья." % EMOJI["lock"])
            if chat_seconds:
                lines.append("%s Осталось примерно: %s" % (
                    EMOJI["clock"], time_left_text(left)))
    return "\n".join(lines)


def anketa_caption(profile, viewer_id=None) -> str:
    lines = ["%s <b>%s</b> <code>#%s</code>" % (
        EMOJI["anketa"], esc(display_name(profile)),
        esc(_value(profile, "tag", "") or make_tag(_value(profile, "user_id", 0))))]
    meta = meta_line(profile)
    if meta:
        lines.append(meta)
    text = str(_value(profile, "anketa_text", "") or "").strip()
    if text:
        lines.append("")
        lines.append(esc(text))
    photos = db.get_anketa_photos(_value(profile, "user_id", 0))
    if photos:
        lines.append("")
        lines.append("%s Фото в анкете: %d" % (EMOJI["camera"], len(photos)))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Кнопки
# --------------------------------------------------------------------------- #
def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def button(text, data) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


BACK_TO_PROFILE = "p:root"


def profile_keyboard(profile) -> InlineKeyboardMarkup:
    """Главное меню профиля: девять разделов в заданном порядке."""
    user_id = int(_value(profile, "user_id", 0) or 0)
    requests = db.count_incoming_requests(user_id)
    friends_text = "%s Друзья" % EMOJI["friends"]
    if requests:
        friends_text += " %s%d" % (EMOJI["bell"], requests)
    return kb([
        [button("%s Редактировать профиль" % EMOJI["pencil"], "p:edit")],
        [button("%s Поделиться" % EMOJI["share"], "p:share"),
         button(friends_text, "p:friends")],
        [button("%s Реакции" % EMOJI["star"], "p:reactions"),
         button("%s Поиск анкет" % EMOJI["search"], "p:search")],
        [button("%s Моя анкета" % EMOJI["anketa"], "p:anketa"),
         button("%s Соцсети" % EMOJI["social"], "p:socials")],
        [button("%s Настройки" % EMOJI["settings"], "p:settings"),
         button("%s Обновить" % EMOJI["refresh"], "p:refresh")],
    ])


def edit_keyboard() -> InlineKeyboardMarkup:
    return kb([
        [button("%s Аватарка" % EMOJI["person"], "e:avatar"),
         button("%s Баннер" % EMOJI["banner"], "e:banner")],
        [button("%s Описание" % EMOJI["note"], "e:bio"),
         button("%s Возраст" % EMOJI["cake"], "e:age")],
        [button("%s Пол" % EMOJI["gender"], "e:gender"),
         button("%s Цвет карточки" % EMOJI["theme"], "e:themes")],
        [button("%s Имя в боте" % EMOJI["name"], "e:name")],
        [button("%s Назад" % EMOJI["back"], BACK_TO_PROFILE)],
    ])


def gender_keyboard() -> InlineKeyboardMarkup:
    return kb([
        [button("%s Парень" % chr(0x2642), "e:g:male"),
         button("%s Девушка" % chr(0x2640), "e:g:female")],
        [button("%s Не указывать" % EMOJI["cross"], "e:g:none")],
        [button("%s Назад" % EMOJI["back"], "p:edit")],
    ])


def theme_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for key in card.THEME_TITLES:
        row.append(button(card.THEME_TITLES.get(key, key), "e:th:%s" % key))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([button("%s Назад" % EMOJI["back"], "p:edit")])
    return kb(rows)


def share_keyboard() -> InlineKeyboardMarkup:
    return kb([
        [button("%s Показать QR" % EMOJI["qr"], "p:qr")],
        [button("%s Назад" % EMOJI["back"], BACK_TO_PROFILE)],
    ])


def friends_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [[button("%s Сканер QR" % EMOJI["scan"], "p:scan")]]
    if db.count_incoming_requests(user_id):
        rows.append([button("%s Заявки в друзья" % EMOJI["bell"], "p:requests")])
    rows.append([button("%s Список друзей" % EMOJI["friends"], "p:friendlist")])
    rows.append([button("%s Назад" % EMOJI["back"], BACK_TO_PROFILE)])
    return kb(rows)


def anketa_keyboard(profile) -> InlineKeyboardMarkup:
    active = bool(_value(profile, "anketa_active", 1))
    return kb([
        [button("%s Текст анкеты" % EMOJI["note"], "a:text"),
         button("%s Фото" % EMOJI["camera"], "a:photo")],
        [button("%s Предпросмотр" % EMOJI["eye"], "a:preview"),
         button("%s Правила" % EMOJI["rules"], "a:rules")],
        [button(("%s Выключить анкету" % EMOJI["stop"]) if active
                else ("%s Включить анкету" % EMOJI["ok"]), "a:toggle")],
        [button("%s Очистить фото" % EMOJI["cross"], "a:clear")],
        [button("%s Назад" % EMOJI["back"], BACK_TO_PROFILE)],
    ])


def socials_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for key in SOCIAL_ORDER:
        icon = SOCIAL_EMOJI.get(key, EMOJI["social"])
        row.append(button("%s %s" % (icon, SOCIALS[key][0]), "e:soc:%s" % key))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([button("%s Назад" % EMOJI["back"], BACK_TO_PROFILE)])
    return kb(rows)


def settings_keyboard(profile) -> InlineKeyboardMarkup:
    return kb([
        [button("%s Своё слово вместо лайка" % EMOJI["like"], "s:like")],
        [button("%s Своё слово вместо дизлайка" % EMOJI["dislike"], "s:dislike")],
        [button("%s Сбросить слова" % EMOJI["refresh"], "s:reset")],
        [button("%s Режим реакций" % EMOJI["word"], "s:modes")],
        [button("%s Правила анкет" % EMOJI["rules"], "a:rules")],
        [button("%s Назад" % EMOJI["back"], BACK_TO_PROFILE)],
    ])


def modes_keyboard(profile) -> InlineKeyboardMarkup:
    current = str(_value(profile, "reaction_mode", "buttons"))
    rows = []
    for mode in REACTION_MODES:
        mark = EMOJI["ok"] if mode == current else EMOJI["word"]
        rows.append([button("%s %s" % (mark, REACTION_MODE_TITLES[mode]), "s:mode:%s" % mode)])
    rows.append([button("%s Назад" % EMOJI["back"], "p:settings")])
    return kb(rows)


def back_keyboard(target=BACK_TO_PROFILE) -> InlineKeyboardMarkup:
    return kb([[button("%s Назад" % EMOJI["back"], target)]])


def cancel_keyboard(target=BACK_TO_PROFILE) -> InlineKeyboardMarkup:
    return kb([[button("%s Отмена" % EMOJI["cross"], target)]])


def rules_keyboard() -> InlineKeyboardMarkup:
    return kb([
        [button("%s Принимаю правила" % EMOJI["ok"], "a:accept")],
        [button("%s Назад" % EMOJI["back"], "p:anketa")],
    ])


def reaction_row(owner, viewer_id: int) -> list:
    owner_id = int(_value(owner, "user_id", 0) or 0)
    mode = str(_value(owner, "reaction_mode", "buttons"))
    like_word, dislike_word = Database.reaction_labels(owner)
    row = []
    if mode in ("buttons", "both"):
        row.append(button("%s %s" % (EMOJI["like"], like_word), "r:like:%d" % owner_id))
        row.append(button("%s %s" % (EMOJI["dislike"], dislike_word), "r:dis:%d" % owner_id))
    if mode in ("words", "both"):
        row.append(button("%s Своё слово" % EMOJI["word"], "r:word:%d" % owner_id))
    return row


def anketa_view_keyboard(owner, viewer_id: int, with_next=True) -> InlineKeyboardMarkup:
    owner_id = int(_value(owner, "user_id", 0) or 0)
    rows = []
    reactions = reaction_row(owner, viewer_id)
    if reactions:
        rows.append(reactions)
    third = [button("%s Написать" % EMOJI["chat"], "c:open:%d" % owner_id)]
    if db.are_friends(viewer_id, owner_id):
        third.append(button("%s Уже друзья" % EMOJI["ok"], "noop"))
    else:
        third.append(button("%s В друзья" % EMOJI["friends"], "fr:add:%d" % owner_id))
    rows.append(third)
    rows.append([button("%s Профиль" % EMOJI["card"], "v:profile:%d" % owner_id),
                 button("%s Жалоба" % EMOJI["report"], "rp:%d" % owner_id)])
    if with_next:
        rows.append([button("%s Следующая анкета" % EMOJI["next"], "go:random")])
    return kb(rows)


def other_profile_keyboard(owner, viewer_id: int, unlocked: bool) -> InlineKeyboardMarkup:
    owner_id = int(_value(owner, "user_id", 0) or 0)
    rows = [[button("%s Написать" % EMOJI["chat"], "c:open:%d" % owner_id)]]
    if not db.are_friends(viewer_id, owner_id):
        rows[0].append(button("%s В друзья" % EMOJI["friends"], "fr:add:%d" % owner_id))
    if db.anketa_is_ready(owner):
        rows.append([button("%s Анкета" % EMOJI["anketa"], "v:anketa:%d" % owner_id)])
    rows.append([button("%s Жалоба" % EMOJI["report"], "rp:%d" % owner_id)])
    return kb(rows)


def chat_keyboard(peer_id: int) -> InlineKeyboardMarkup:
    return kb([
        [button("%s Профиль" % EMOJI["card"], "v:profile:%d" % peer_id),
         button("%s Завершить" % EMOJI["cross"], "c:stop:%d" % peer_id)],
    ])


def start_keyboard() -> InlineKeyboardMarkup:
    return kb([
        [button("%s Мой профиль" % EMOJI["card"], "p:root")],
        [button("%s Смотреть анкеты" % EMOJI["dice"], "go:random")],
    ])


# --------------------------------------------------------------------------- #
# Единое сообщение профиля
#
# На каждого человека держим одно сообщение с карточкой и меню.
# Все разделы перерисовывают его на месте, а не плодят новые сообщения.
# Анкеты, личные сообщения и уведомления - отдельные сообщения.
# --------------------------------------------------------------------------- #
db = None            # заполняется в main()
dp = Dispatcher()
MENU = {}            # user_id -> {"chat", "msg", "kind", "digest"}
ANKETA_MSG = {}      # user_id -> (chat_id, message_id) последней показанной анкеты


def _ignorable(error: Exception) -> bool:
    text = str(error).lower()
    return "not modified" in text or "message to edit not found" in text


async def _edit_caption(bot: Bot, entry, caption, keyboard):
    try:
        await bot.edit_message_caption(
            chat_id=entry["chat"], message_id=entry["msg"],
            caption=caption, reply_markup=keyboard)
        return True
    except TelegramAPIError as error:
        if "not modified" in str(error).lower():
            return True
        return False


async def _edit_media(bot: Bot, entry, media, caption, keyboard):
    try:
        return await bot.edit_message_media(
            chat_id=entry["chat"], message_id=entry["msg"],
            media=InputMediaPhoto(media=media, caption=caption,
                                  parse_mode=ParseMode.HTML),
            reply_markup=keyboard)
    except TelegramAPIError as error:
        if "not modified" in str(error).lower():
            return True
        return None


async def menu_forget(bot: Bot, user_id: int, delete=True):
    entry = MENU.pop(user_id, None)
    if entry and delete:
        with contextlib.suppress(TelegramAPIError):
            await bot.delete_message(entry["chat"], entry["msg"])


async def menu_card(bot: Bot, user_id: int, chat_id: int, profile,
                    caption: str, keyboard, force_new=False):
    """Показывает карточку в ��дином сообщении."""
    entry = MENU.get(user_id)
    if force_new:
        await menu_forget(bot, user_id)
        entry = None

    if entry and entry.get("kind") == "card":
        digest = card_digest(profile, CARD_SIZE)
        if entry.get("digest") == digest:
            if await _edit_caption(bot, entry, caption, keyboard):
                return
            await menu_forget(bot, user_id)
            entry = None

    media, digest, cached = await card_media(bot, profile)
    if entry:
        result = await _edit_media(bot, entry, media, caption, keyboard)
        if result:
            entry["kind"] = "card"
            entry["digest"] = digest
            if not cached and isinstance(result, Message):
                remember_card(profile, digest, result)
            return
        await menu_forget(bot, user_id)

    message = await bot.send_photo(chat_id, media, caption=caption,
                                   reply_markup=keyboard)
    if not cached:
        remember_card(profile, digest, message)
    MENU[user_id] = {"chat": chat_id, "msg": message.message_id,
                     "kind": "card", "digest": digest}


async def menu_photo(bot: Bot, user_id: int, chat_id: int, payload: bytes,
                     caption: str, keyboard, kind: str, filename: str):
    """Показывает в том же сообщении другую картинку (например QR)."""
    media = BufferedInputFile(payload, filename=filename)
    entry = MENU.get(user_id)
    if entry:
        result = await _edit_media(bot, entry, media, caption, keyboard)
        if result:
            entry["kind"] = kind
            entry["digest"] = None
            return
        await menu_forget(bot, user_id)
    message = await bot.send_photo(chat_id, media, caption=caption,
                                   reply_markup=keyboard)
    MENU[user_id] = {"chat": chat_id, "msg": message.message_id,
                     "kind": kind, "digest": None}


async def menu_text(bot: Bot, user_id: int, chat_id: int, caption: str, keyboard):
    """Меняет только подпись и кнопки - самый быстрый путь."""
    entry = MENU.get(user_id)
    if entry and await _edit_caption(bot, entry, caption, keyboard):
        return
    profile = db.get_profile(user_id)
    await menu_card(bot, user_id, chat_id, profile, caption, keyboard, force_new=True)


async def show_profile(bot: Bot, user_id: int, chat_id: int, notice="", force_new=False):
    profile = db.get_profile(user_id)
    caption = profile_caption(profile)
    if notice:
        caption = "%s\n\n%s" % (notice, caption)
    await menu_card(bot, user_id, chat_id, profile, caption[:1024],
                    profile_keyboard(profile), force_new=force_new)


async def ask_input(bot: Bot, user_id: int, chat_id: int, state: FSMContext,
                    form_state: State, text: str, cancel="p:edit"):
    """Просит ввод прямо в том же сообщении."""
    await state.set_state(form_state)
    await state.update_data(cancel=cancel)
    await menu_text(bot, user_id, chat_id, text[:1024], cancel_keyboard(cancel))


async def drop_user_message(message: Message):
    """Чистим чат: ввод пользователя больше не нужен."""
    with contextlib.suppress(TelegramAPIError):
        await message.delete()


# --------------------------------------------------------------------------- #
# Анкеты и просмотр чужих профилей (отдельные сообщения)
# --------------------------------------------------------------------------- #
async def send_anketa(bot: Bot, viewer_id: int, chat_id: int, owner,
                      with_next=True, replace=True):
    caption = anketa_caption(owner, viewer_id)[:1024]
    keyboard = anketa_view_keyboard(owner, viewer_id, with_next=with_next)
    photos = db.get_anketa_photos(_value(owner, "user_id", 0))
    if replace:
        old = ANKETA_MSG.pop(viewer_id, None)
        if old:
            with contextlib.suppress(TelegramAPIError):
                await bot.delete_message(old[0], old[1])
    if photos:
        message = await bot.send_photo(chat_id, photos[0], caption=caption,
                                       reply_markup=keyboard)
    else:
        media, digest, cached = await card_media(bot, owner)
        message = await bot.send_photo(chat_id, media, caption=caption,
                                       reply_markup=keyboard)
        if not cached:
            remember_card(owner, digest, message)
    ANKETA_MSG[viewer_id] = (chat_id, message.message_id)
    return message


async def send_other_profile(bot: Bot, viewer_id: int, chat_id: int, owner):
    owner_id = int(_value(owner, "user_id", 0) or 0)
    unlocked = db.socials_unlocked(viewer_id, owner_id)
    seconds = db.chat_seconds(viewer_id, owner_id)
    caption = profile_caption(owner, viewer_id=viewer_id, unlocked=unlocked,
                              chat_seconds=seconds)[:1024]
    keyboard = other_profile_keyboard(owner, viewer_id, unlocked)
    media, digest, cached = await card_media(bot, owner)
    message = await bot.send_photo(chat_id, media, caption=caption,
                                   reply_markup=keyboard)
    if not cached:
        remember_card(owner, digest, message)
    return message


async def show_next_anketa(bot: Bot, user_id: int, chat_id: int):
    row = db.next_anketa(user_id)
    if row is None:
        wait = db.view_wait_seconds(user_id)
        if wait > 0:
            text = ("%s Новые анкеты закончились.\n"
                    "Показанные раньше вернутся в ленту через %s.") % (
                EMOJI["clock"], time_left_text(wait))
        else:
            text = ("%s Пока нет готовых анкет кроме вашей. "
                    "Загляните позже или позовите друзей через QR.") % EMOJI["dice"]
        await bot.send_message(chat_id, text, reply_markup=back_keyboard())
        return
    db.mark_view(user_id, int(row["user_id"]))
    await send_anketa(bot, user_id, chat_id, row)


# --------------------------------------------------------------------------- #
# Общие проверки
# --------------------------------------------------------------------------- #
def sync_user(message: Message):
    user = message.from_user
    profile = db.ensure_profile(user.id, user.username or "")
    db.touch_seen(user.id)
    return profile


async def require_name(bot: Bot, message: Message, state: FSMContext, profile):
    """Без имени в боте ничего не работает."""
    if _value(profile, "bot_name", ""):
        return True
    await state.set_state(Form.name)
    await message.answer("%s\n\n%s" % (WELCOME, NAME_RULES),
                         reply_markup=None)
    return False


def parse_tag(text: str):
    value = (text or "").strip()
    if not value:
        return None
    if not TAG_RE.match(value):
        return None
    return value.lstrip("#")


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #
@dp.message(CommandStart(deep_link=True))
async def cmd_start_deep(message: Message, command: CommandObject, state: FSMContext):
    profile = sync_user(message)
    payload = (command.args or "").strip()
    if payload.startswith("add_"):
        code = payload[4:]
        target = db.find_by_qr_code(code)
        if target is None:
            await message.answer("%s Код не найден или устарел." % EMOJI["warn"])
        else:
            await handle_friend_request(message.bot, message.from_user.id,
                                        int(target["user_id"]), message.chat.id)
    if not _value(profile, "bot_name", ""):
        await state.set_state(Form.name)
        await message.answer("%s\n\n%s" % (WELCOME, NAME_RULES))
        return
    await state.clear()
    await show_profile(message.bot, message.from_user.id, message.chat.id,
                       force_new=True)


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    profile = sync_user(message)
    if not _value(profile, "bot_name", ""):
        await state.set_state(Form.name)
        await message.answer("%s\n\n%s" % (WELCOME, NAME_RULES))
        return
    await state.clear()
    await message.answer("%s\n%s" % (WELCOME, HELP_HINT), reply_markup=start_keyboard())


@dp.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext):
    profile = sync_user(message)
    if not await require_name(message.bot, message, state, profile):
        return
    await state.clear()
    await show_profile(message.bot, message.from_user.id, message.chat.id,
                       force_new=True)


@dp.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject, state: FSMContext):
    profile = sync_user(message)
    if not await require_name(message.bot, message, state, profile):
        return
    tag = parse_tag(command.args or "")
    if tag is None:
        await message.answer(
            "%s <b>Поиск по хэшу</b>\n\n"
            "Напишите так: <code>/search #%s</code>\n"
            "Хэш - это пять цифр из карточки человека.\n"
            "Ваш хэш: <code>#%s</code>" % (
                EMOJI["search"], "12345",
                esc(_value(profile, "tag", "") or make_tag(message.from_user.id))))
        return
    await run_search(message.bot, message.from_user.id, message.chat.id, tag)


async def run_search(bot: Bot, viewer_id: int, chat_id: int, tag: str):
    row = db.find_by_tag(tag)
    if row is None or not _value(row, "bot_name", ""):
        await bot.send_message(
            chat_id, "%s Никого с хэшем <code>#%s</code> нет." % (EMOJI["warn"], esc(tag)))
        return
    owner_id = int(row["user_id"])
    if owner_id == viewer_id:
        await bot.send_message(chat_id, "%s Это ваш собственный хэш." % EMOJI["card"])
        return
    viewer = db.get_profile(viewer_id)
    if db.is_blocked(row) or not db.contact_allowed(viewer, row):
        await bot.send_message(chat_id, "%s Анкета сейчас недоступна." % EMOJI["lock"])
        return
    if db.anketa_is_ready(row):
        await send_anketa(bot, viewer_id, chat_id, row, with_next=False)
    else:
        await send_other_profile(bot, viewer_id, chat_id, row)


@dp.message(Command("random"))
async def cmd_random(message: Message, state: FSMContext):
    profile = sync_user(message)
    if not await require_name(message.bot, message, state, profile):
        return
    await state.clear()
    await show_next_anketa(message.bot, message.from_user.id, message.chat.id)


# --------------------------------------------------------------------------- #
# Тексты разделов (всё живёт в одном сообщении)
# --------------------------------------------------------------------------- #
def edit_view_text(profile) -> str:
    age = Database.age_of(profile)
    gender = str(_value(profile, "gender", "") or "")
    theme = card.resolve_theme(_value(profile, "theme", "blue"))
    return "\n".join([
        "%s <b>Редактирование профиля</b>" % EMOJI["pencil"],
        "",
        "%s Имя: <b>%s</b>" % (EMOJI["name"], esc(display_name(profile))),
        "%s Возраст: %s" % (EMOJI["cake"], age if age else "не указан"),
        "%s Пол: %s" % (EMOJI["gender"],
                            card.GENDER_TITLES.get(gender, "не указан")),
        "%s Описание: %s" % (
            EMOJI["note"],
            "есть" if str(_value(profile, "bio", "") or "").strip() else "пусто"),
        "%s Аватарка: %s" % (
            EMOJI["person"],
            "есть" if _value(profile, "photo_file_id") else "нет"),
        "%s Баннер: %s" % (
            EMOJI["banner"],
            "есть" if _value(profile, "banner_file_id") else "нет"),
        "%s Цвет: %s" % (EMOJI["theme"], card.THEME_TITLES.get(theme, theme)),
    ])


def share_view_text(profile) -> str:
    code = str(_value(profile, "qr_code", "") or "")
    tag = _value(profile, "tag", "") or make_tag(_value(profile, "user_id", 0))
    link = "https://t.me/%s?start=add_%s" % (BOT_USERNAME, code) if BOT_USERNAME else ""
    lines = [
        "%s <b>Поделиться профилем</b>" % EMOJI["share"],
        "",
        "Ваш хэш: <code>#%s</code>" % esc(tag),
        "Его можно искать так: <code>/search #%s</code>" % esc(tag),
    ]
    if link:
        lines.append("")
        lines.append('%s <a href="%s">Ссылка-приглашение в друзья</a>' % (
            EMOJI["qr"], esc(link)))
        lines.append("Кто откроет ссылку или сканирует QR - сразу отправит вам заявку.")
    return "\n".join(lines)


def friends_view_text(user_id: int) -> str:
    friends = db.count_friends(user_id)
    requests = db.count_incoming_requests(user_id)
    lines = [
        "%s <b>Друзья</b>" % EMOJI["friends"],
        "",
        "В друзьях: <b>%d</b>" % friends,
        "Входящие заявки: <b>%d</b>" % requests,
        "",
        "%s Друзьям сразу видны ваши соцсети и телеграм." % EMOJI["unlock"],
    ]
    scanner = "работает" if qr.scanner_available() else "код можно ввести текстом"
    lines.append("%s Сканер QR: %s" % (EMOJI["scan"], scanner))
    return "\n".join(lines)


def reactions_view_text(profile) -> str:
    user_id = int(_value(profile, "user_id", 0) or 0)
    stats = db.reaction_stats(user_id)
    like_word, dislike_word = Database.reaction_labels(profile)
    words = db.recent_words(user_id, limit=8)
    lines = [
        "%s <b>Реакции на вас</b>" % EMOJI["star"],
        "",
        "%s %s: <b>%d</b>" % (EMOJI["like"], esc(like_word), stats["like"]),
        "%s %s: <b>%d</b>" % (EMOJI["dislike"], esc(dislike_word), stats["dislike"]),
        "%s Словами: <b>%d</b>" % (EMOJI["word"], stats["word"]),
    ]
    if words:
        lines.append("")
        lines.append("%s Последние слова:" % EMOJI["word"])
        lines.append(esc(", ".join(words)))
    return "\n".join(lines)


def search_view_text(profile) -> str:
    tag = _value(profile, "tag", "") or make_tag(_value(profile, "user_id", 0))
    return "\n".join([
        "%s <b>Поиск анкет</b>" % EMOJI["search"],
        "",
        "%s Кнопка ниже - случайные анкеты. "
        "Одна и та же анкета повторяется не чаще раза в 5 часов." % EMOJI["dice"],
        "",
        "%s Поиск по хэшу: команда <code>/search #12345</code>" % EMOJI["hash"],
        "Ваш хэш: <code>#%s</code>" % esc(tag),
        "В найденной анкете есть кнопка <b>Написать</b> - личные сообщения идут через бота.",
    ])


def anketa_view_text(profile) -> str:
    user_id = int(_value(profile, "user_id", 0) or 0)
    text = str(_value(profile, "anketa_text", "") or "").strip()
    photos = db.get_anketa_photos(user_id)
    active = bool(_value(profile, "anketa_active", 1))
    ready = db.anketa_is_ready(profile)
    lines = [
        "%s <b>Моя анкета</b>" % EMOJI["anketa"],
        "",
        "Состояние: <b>%s</b>" % ("в поиске" if ready and active else "не показывается"),
        "Фото: <b>%d из %d</b>" % (len(photos), MAX_ANKETA_PHOTOS),
        "Правила: <b>%s</b>" % ("приняты" if Database.rules_accepted(profile)
                                    else "не приняты"),
    ]
    if text:
        lines.append("")
        lines.append(esc(text[:400]))
    else:
        lines.append("")
        lines.append("Текста пока нет. Анкета показывается в /random другим людям.")
    return "\n".join(lines)


def socials_view_text(profile) -> str:
    user_id = int(_value(profile, "user_id", 0) or 0)
    data = db.get_socials(user_id)
    rows = socials_lines(data)
    lines = ["%s <b>Соцсети</b>" % EMOJI["social"], ""]
    if rows:
        lines.extend(rows)
    else:
        lines.append("Пока ничего не указано.")
    lines.append("")
    lines.append("%s Соцсети видны друзьям сразу, а остальным - после "
                 "10 минут общения в боте." % EMOJI["lock"])
    return "\n".join(lines)


def settings_view_text(profile) -> str:
    like_word, dislike_word = Database.reaction_labels(profile)
    mode = str(_value(profile, "reaction_mode", "buttons"))
    return "\n".join([
        "%s <b>Настройки</b>" % EMOJI["settings"],
        "",
        "%s Вместо лайка: <b>%s</b>" % (EMOJI["like"], esc(like_word)),
        "%s Вместо дизлайка: <b>%s</b>" % (EMOJI["dislike"], esc(dislike_word)),
        "%s Режим реакций: <b>%s</b>" % (
            EMOJI["word"], REACTION_MODE_TITLES.get(mode, mode)),
        "",
        "Свои слова видят все, кто смотрит вашу анкету: до %d символов."
        % CUSTOM_LABEL_LIMIT,
    ])


# --------------------------------------------------------------------------- #
# Кнопки: общие помощники
# --------------------------------------------------------------------------- #
def cb_user(callback: CallbackQuery):
    user = callback.from_user
    profile = db.ensure_profile(user.id, user.username or "")
    db.touch_seen(user.id)
    return profile


async def quick(callback: CallbackQuery, text="", alert=False):
    with contextlib.suppress(TelegramAPIError):
        await callback.answer(str(text)[:190], show_alert=alert)


def cb_target(callback: CallbackQuery, prefix: str):
    raw = (callback.data or "").split(":")[-1]
    try:
        return int(raw)
    except ValueError:
        return 0


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await quick(callback)


@dp.callback_query(F.data.in_({"p:root", "go:profile"}))
async def cb_root(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    cb_user(callback)
    await state.clear()
    await show_profile(callback.bot, callback.from_user.id, callback.message.chat.id)


@dp.callback_query(F.data == "p:refresh")
async def cb_refresh(callback: CallbackQuery, state: FSMContext):
    await quick(callback, "Обновляю")
    cb_user(callback)
    await state.clear()
    card_changed(callback.from_user.id)
    await show_profile(callback.bot, callback.from_user.id, callback.message.chat.id)


@dp.callback_query(F.data == "p:edit")
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    profile = cb_user(callback)
    await state.clear()
    await menu_text(callback.bot, callback.from_user.id, callback.message.chat.id,
                    edit_view_text(profile), edit_keyboard())


@dp.callback_query(F.data == "p:share")
async def cb_share(callback: CallbackQuery):
    await quick(callback)
    profile = cb_user(callback)
    await menu_card(callback.bot, callback.from_user.id, callback.message.chat.id,
                    profile, share_view_text(profile), share_keyboard())


@dp.callback_query(F.data == "p:qr")
async def cb_qr(callback: CallbackQuery):
    await quick(callback)
    profile = cb_user(callback)
    code = str(_value(profile, "qr_code", "") or "")
    if not code or not BOT_USERNAME:
        await quick(callback, "QR пока недоступен", alert=True)
        return
    link = "https://t.me/%s?start=add_%s" % (BOT_USERNAME, code)
    payload = await asyncio.to_thread(qr.make_qr, link, 720, (20, 24, 32),
                                     (255, 255, 255), display_name(profile))
    caption = ("%s <b>QR для друзей</b>\n\n"
               "Покажите код человеку рядом или отправьте картинкой.\n"
               "Он открывает заявку в друзья без поиска и хэша.") % EMOJI["qr"]
    await menu_photo(callback.bot, callback.from_user.id, callback.message.chat.id,
                     payload, caption, share_keyboard(), "qr", "tochka-qr.png")


@dp.callback_query(F.data == "p:friends")
async def cb_friends(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    profile = cb_user(callback)
    await state.clear()
    user_id = callback.from_user.id
    await menu_card(callback.bot, user_id, callback.message.chat.id, profile,
                    friends_view_text(user_id), friends_keyboard(user_id))


@dp.callback_query(F.data == "p:scan")
async def cb_scan(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    cb_user(callback)
    hint = ("%s <b>Сканер QR</b>\n\n"
            "Пришлите фото чужого QR или отправьте его код текстом.\n"
            "Ссылка-приглашение тоже подойдёт." % EMOJI["scan"])
    await ask_input(callback.bot, callback.from_user.id, callback.message.chat.id,
                    state, Form.qr_scan, hint, cancel="p:friends")


@dp.callback_query(F.data == "p:requests")
async def cb_requests(callback: CallbackQuery):
    await quick(callback)
    cb_user(callback)
    user_id = callback.from_user.id
    rows = db.incoming_requests(user_id, limit=5)
    if not rows:
        await menu_text(callback.bot, user_id, callback.message.chat.id,
                        "%s Заявок пока нет." % EMOJI["bell"],
                        back_keyboard("p:friends"))
        return
    lines = ["%s <b>Заявки в друзья</b>" % EMOJI["bell"], ""]
    keys = []
    for row in rows:
        other_id = int(row["user_id"])
        name = display_name(row)
        meta = meta_line(row)
        lines.append("%s <b>%s</b> <code>#%s</code> %s" % (
            EMOJI["person"], esc(name), esc(row["tag"] or ""), meta))
        keys.append([
            button("%s %s" % (EMOJI["ok"], name[:12]), "fr:acc:%d" % other_id),
            button("%s" % EMOJI["cross"], "fr:dec:%d" % other_id),
            button("%s" % EMOJI["card"], "v:profile:%d" % other_id),
        ])
    keys.append([button("%s Назад" % EMOJI["back"], "p:friends")])
    await menu_text(callback.bot, user_id, callback.message.chat.id,
                    "\n".join(lines)[:1024], kb(keys))


@dp.callback_query(F.data == "p:friendlist")
async def cb_friendlist(callback: CallbackQuery):
    await quick(callback)
    cb_user(callback)
    user_id = callback.from_user.id
    rows = db.get_friends(user_id, limit=8)
    lines = ["%s <b>Список друзей</b>" % EMOJI["friends"], ""]
    keys = []
    if not rows:
        lines.append("Пока пусто. Покажите свой QR или отправьте заявку из анкеты.")
    for row in rows:
        other_id = int(row["user_id"])
        lines.append("%s <b>%s</b> <code>#%s</code> %s" % (
            EMOJI["person"], esc(display_name(row)), esc(row["tag"] or ""),
            meta_line(row)))
        keys.append([
            button("%s %s" % (EMOJI["chat"], display_name(row)[:12]),
                   "c:open:%d" % other_id),
            button("%s" % EMOJI["card"], "v:profile:%d" % other_id),
        ])
    keys.append([button("%s Назад" % EMOJI["back"], "p:friends")])
    await menu_text(callback.bot, user_id, callback.message.chat.id,
                    "\n".join(lines)[:1024], kb(keys))


@dp.callback_query(F.data == "p:reactions")
async def cb_reactions(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    profile = cb_user(callback)
    await state.clear()
    await menu_card(callback.bot, callback.from_user.id, callback.message.chat.id,
                    profile, reactions_view_text(profile),
                    kb([[button("%s Свои слова" % EMOJI["note"], "p:settings")],
                        [button("%s Назад" % EMOJI["back"], BACK_TO_PROFILE)]]))


@dp.callback_query(F.data == "p:search")
async def cb_search_view(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    profile = cb_user(callback)
    await state.clear()
    await menu_card(callback.bot, callback.from_user.id, callback.message.chat.id,
                    profile, search_view_text(profile),
                    kb([[button("%s Случайная анкета" % EMOJI["dice"], "go:random")],
                        [button("%s Назад" % EMOJI["back"], BACK_TO_PROFILE)]]))


@dp.callback_query(F.data == "p:anketa")
async def cb_anketa(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    profile = cb_user(callback)
    await state.clear()
    await menu_card(callback.bot, callback.from_user.id, callback.message.chat.id,
                    profile, anketa_view_text(profile), anketa_keyboard(profile))


@dp.callback_query(F.data == "p:socials")
async def cb_socials(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    profile = cb_user(callback)
    await state.clear()
    await menu_card(callback.bot, callback.from_user.id, callback.message.chat.id,
                    profile, socials_view_text(profile), socials_keyboard())


@dp.callback_query(F.data == "p:settings")
async def cb_settings(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    profile = cb_user(callback)
    await state.clear()
    await menu_card(callback.bot, callback.from_user.id, callback.message.chat.id,
                    profile, settings_view_text(profile), settings_keyboard(profile))


@dp.callback_query(F.data == "s:modes")
async def cb_modes(callback: CallbackQuery):
    await quick(callback)
    profile = cb_user(callback)
    await menu_text(callback.bot, callback.from_user.id, callback.message.chat.id,
                    settings_view_text(profile), modes_keyboard(profile))


@dp.callback_query(F.data.startswith("s:mode:"))
async def cb_mode_set(callback: CallbackQuery):
    mode = (callback.data or "").split(":")[-1]
    cb_user(callback)
    if mode not in REACTION_MODES:
        await quick(callback, "Неизвестный режим")
        return
    db.update_profile(callback.from_user.id, reaction_mode=mode)
    await quick(callback, REACTION_MODE_TITLES.get(mode, mode))
    profile = db.get_profile(callback.from_user.id)
    await menu_text(callback.bot, callback.from_user.id, callback.message.chat.id,
                    settings_view_text(profile), modes_keyboard(profile))


@dp.callback_query(F.data == "s:reset")
async def cb_reset_words(callback: CallbackQuery):
    cb_user(callback)
    db.set_reaction_label(callback.from_user.id, "like", None)
    db.set_reaction_label(callback.from_user.id, "dislike", None)
    await quick(callback, "Слова сброшены")
    profile = db.get_profile(callback.from_user.id)
    await menu_text(callback.bot, callback.from_user.id, callback.message.chat.id,
                    settings_view_text(profile), settings_keyboard(profile))


@dp.callback_query(F.data.in_({"s:like", "s:dislike"}))
async def cb_custom_word(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    cb_user(callback)
    kind = "like" if callback.data == "s:like" else "dislike"
    form = Form.like_word if kind == "like" else Form.dislike_word
    title = "лайка" if kind == "like" else "дизлайка"
    text = ("%s <b>Своё слово вместо %s</b>\n\n"
            "Напишите короткое слово до %d символов.\n"
            "Чтобы вернуть обычное - отправьте прочерк.") % (
        EMOJI["note"], title, CUSTOM_LABEL_LIMIT)
    await ask_input(callback.bot, callback.from_user.id, callback.message.chat.id,
                    state, form, text, cancel="p:settings")


# --------------------------------------------------------------------------- #
# Редактирование карточки
# --------------------------------------------------------------------------- #
@dp.callback_query(F.data == "e:avatar")
async def cb_ask_avatar(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    cb_user(callback)
    await ask_input(
        callback.bot, callback.from_user.id, callback.message.chat.id, state,
        Form.avatar,
        "%s <b>Аватарка</b>\n\nПришлите фото или картинку файлом.\n"
        "Квадратное фото смотрится лучше всего." % EMOJI["person"])


@dp.callback_query(F.data == "e:banner")
async def cb_ask_banner(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    cb_user(callback)
    await ask_input(
        callback.bot, callback.from_user.id, callback.message.chat.id, state,
        Form.banner,
        "%s <b>Баннер</b>\n\nПришлите картинку для прямоугольника внизу карточки.\n"
        "Лучше всего широкая картинка.\n"
        "Чтобы убрать баннер - отправьте прочерк." % EMOJI["banner"])


@dp.callback_query(F.data == "e:bio")
async def cb_ask_bio(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    cb_user(callback)
    await ask_input(
        callback.bot, callback.from_user.id, callback.message.chat.id, state,
        Form.bio,
        "%s <b>Описание</b>\n\nДо %d символов, три строчки на карточке.\n"
        "Чтобы очистить - отправьте прочерк." % (EMOJI["note"], card.BIO_LIMIT))


@dp.callback_query(F.data == "e:age")
async def cb_ask_age(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    cb_user(callback)
    await ask_input(
        callback.bot, callback.from_user.id, callback.message.chat.id, state,
        Form.age,
        "%s <b>Возраст</b>\n\nНапишите число от %d до %d.\n"
        "Возраст влияет на то, кому покажут вашу анкету.\n"
        "Чтобы убрать - отправьте прочерк." % (EMOJI["cake"], MIN_AGE, MAX_AGE))


@dp.callback_query(F.data == "e:gender")
async def cb_ask_gender(callback: CallbackQuery):
    await quick(callback)
    cb_user(callback)
    await menu_text(callback.bot, callback.from_user.id, callback.message.chat.id,
                    "%s <b>Пол</b>\n\nНа карточке отобразится только значок."
                    % EMOJI["gender"], gender_keyboard())


@dp.callback_query(F.data.startswith("e:g:"))
async def cb_set_gender(callback: CallbackQuery):
    value = (callback.data or "").split(":")[-1]
    cb_user(callback)
    gender = value if value in ("male", "female") else None
    db.update_profile(callback.from_user.id, gender=gender)
    card_changed(callback.from_user.id)
    await quick(callback, "Готово")
    profile = db.get_profile(callback.from_user.id)
    await menu_card(callback.bot, callback.from_user.id, callback.message.chat.id,
                    profile, edit_view_text(profile), edit_keyboard())


@dp.callback_query(F.data == "e:themes")
async def cb_themes(callback: CallbackQuery):
    await quick(callback)
    cb_user(callback)
    await menu_text(callback.bot, callback.from_user.id, callback.message.chat.id,
                    "%s <b>Цвет карто��ки</b>\n\nВыберите один из 12 вариантов."
                    % EMOJI["theme"], theme_keyboard())


@dp.callback_query(F.data.startswith("e:th:"))
async def cb_set_theme(callback: CallbackQuery):
    key = (callback.data or "").split(":")[-1]
    cb_user(callback)
    theme = card.resolve_theme(key)
    db.update_profile(callback.from_user.id, theme=theme)
    card_changed(callback.from_user.id)
    await quick(callback, card.THEME_TITLES.get(theme, theme))
    profile = db.get_profile(callback.from_user.id)
    await menu_card(callback.bot, callback.from_user.id, callback.message.chat.id,
                    profile, edit_view_text(profile), edit_keyboard())


@dp.callback_query(F.data == "e:name")
async def cb_ask_name(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    cb_user(callback)
    left = db.name_cooldown_left(callback.from_user.id)
    if left > 0:
        await menu_text(
            callback.bot, callback.from_user.id, callback.message.chat.id,
            "%s Менять имя можно раз в 48 часов.\nОсталось: %s." % (
                EMOJI["clock"], time_left_text(left)), back_keyboard("p:edit"))
        return
    await ask_input(callback.bot, callback.from_user.id, callback.message.chat.id,
                    state, Form.rename, NAME_RULES)


@dp.callback_query(F.data.startswith("e:soc:"))
async def cb_ask_social(callback: CallbackQuery, state: FSMContext):
    key = (callback.data or "").split(":")[-1]
    await quick(callback)
    cb_user(callback)
    if key not in SOCIALS:
        return
    title, template = SOCIALS[key][0], SOCIALS[key][1]
    await state.set_state(Form.social)
    await state.update_data(social=key, cancel="p:socials")
    hint = "Шаблон ссылки: <code>%s</code>" % esc(template % "ник") if template else ""
    await menu_text(
        callback.bot, callback.from_user.id, callback.message.chat.id,
        "%s <b>%s</b>\n\nПришлите ник или ссылку.\n%s\n"
        "Чтобы убрать - отправьте прочерк." % (
            SOCIAL_EMOJI.get(key, EMOJI["social"]), esc(title), hint),
        cancel_keyboard("p:socials"))


# --------------------------------------------------------------------------- #
# Анкета и правила
# --------------------------------------------------------------------------- #
@dp.callback_query(F.data == "a:rules")
async def cb_rules(callback: CallbackQuery):
    await quick(callback)
    cb_user(callback)
    await menu_text(callback.bot, callback.from_user.id, callback.message.chat.id,
                    RULES_TEXT, rules_keyboard())


@dp.callback_query(F.data == "a:accept")
async def cb_accept_rules(callback: CallbackQuery):
    cb_user(callback)
    db.accept_rules(callback.from_user.id)
    await quick(callback, "Правила приняты")
    profile = db.get_profile(callback.from_user.id)
    await menu_card(callback.bot, callback.from_user.id, callback.message.chat.id,
                    profile, anketa_view_text(profile), anketa_keyboard(profile))


async def require_rules(callback: CallbackQuery, profile) -> bool:
    if Database.rules_accepted(profile):
        return True
    await menu_text(callback.bot, callback.from_user.id, callback.message.chat.id,
                    RULES_TEXT, rules_keyboard())
    return False


@dp.callback_query(F.data == "a:text")
async def cb_anketa_text(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    profile = cb_user(callback)
    if not await require_rules(callback, profile):
        return
    await ask_input(
        callback.bot, callback.from_user.id, callback.message.chat.id, state,
        Form.anketa_text,
        "%s <b>Текст анкеты</b>\n\nДо %d символов. Расскажите о себе и о том, "
        "кого ищете. Ссылки в анкете не пройдут.\n"
        "Чтобы очистить - отправьте прочерк." % (EMOJI["note"], ANKETA_TEXT_LIMIT),
        cancel="p:anketa")


@dp.callback_query(F.data == "a:photo")
async def cb_anketa_photo(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    profile = cb_user(callback)
    if not await require_rules(callback, profile):
        return
    photos = db.get_anketa_photos(callback.from_user.id)
    await ask_input(
        callback.bot, callback.from_user.id, callback.message.chat.id, state,
        Form.anketa_photo,
        "%s <b>Фото в анкету</b>\n\nСейчас: %d из %d.\n"
        "Пришлите фото. Первое фото люди видят в поиске." % (
            EMOJI["camera"], len(photos), MAX_ANKETA_PHOTOS),
        cancel="p:anketa")


@dp.callback_query(F.data == "a:clear")
async def cb_anketa_clear(callback: CallbackQuery):
    cb_user(callback)
    db.clear_anketa_photos(callback.from_user.id)
    await quick(callback, "Фото убраны")
    profile = db.get_profile(callback.from_user.id)
    await menu_card(callback.bot, callback.from_user.id, callback.message.chat.id,
                    profile, anketa_view_text(profile), anketa_keyboard(profile))


@dp.callback_query(F.data == "a:toggle")
async def cb_anketa_toggle(callback: CallbackQuery):
    profile = cb_user(callback)
    active = bool(_value(profile, "anketa_active", 1))
    if not active and not Database.rules_accepted(profile):
        await quick(callback)
        await menu_text(callback.bot, callback.from_user.id, callback.message.chat.id,
                        RULES_TEXT, rules_keyboard())
        return
    db.update_profile(callback.from_user.id, anketa_active=0 if active else 1)
    await quick(callback, "Анкета выключена" if active else "Анкета включена")
    profile = db.get_profile(callback.from_user.id)
    await menu_card(callback.bot, callback.from_user.id, callback.message.chat.id,
                    profile, anketa_view_text(profile), anketa_keyboard(profile))


@dp.callback_query(F.data == "a:preview")
async def cb_anketa_preview(callback: CallbackQuery):
    await quick(callback)
    profile = cb_user(callback)
    if not db.anketa_is_ready(profile):
        await quick(callback, "Сначала текст или фото", alert=True)
        return
    caption = "%s <b>Так вашу анкету видят другие</b>\n\n%s" % (
        EMOJI["eye"], anketa_caption(profile))
    photos = db.get_anketa_photos(callback.from_user.id)
    keyboard = back_keyboard("p:anketa")
    if photos:
        await callback.bot.send_photo(callback.message.chat.id, photos[0],
                                      caption=caption[:1024], reply_markup=keyboard)
    else:
        media, digest, cached = await card_media(callback.bot, profile)
        message = await callback.bot.send_photo(
            callback.message.chat.id, media, caption=caption[:1024],
            reply_markup=keyboard)
        if not cached:
            remember_card(profile, digest, message)


# --------------------------------------------------------------------------- #
# Анкеты, реакции, друзья, жалобы
# --------------------------------------------------------------------------- #
@dp.callback_query(F.data == "go:random")
async def cb_random(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    cb_user(callback)
    await state.clear()
    await show_next_anketa(callback.bot, callback.from_user.id, callback.message.chat.id)


@dp.callback_query(F.data.startswith("v:profile:"))
async def cb_view_profile(callback: CallbackQuery):
    await quick(callback)
    viewer = cb_user(callback)
    owner_id = cb_target(callback, "v:profile")
    owner = db.get_profile(owner_id)
    if owner is None or not db.contact_allowed(viewer, owner):
        await quick(callback, "Анкета сейчас недоступна", alert=True)
        return
    await send_other_profile(callback.bot, callback.from_user.id,
                             callback.message.chat.id, owner)


@dp.callback_query(F.data.startswith("v:anketa:"))
async def cb_view_anketa(callback: CallbackQuery):
    await quick(callback)
    viewer = cb_user(callback)
    owner_id = cb_target(callback, "v:anketa")
    owner = db.get_profile(owner_id)
    if owner is None or not db.contact_allowed(viewer, owner):
        await quick(callback, "Анкета сейчас недоступна", alert=True)
        return
    await send_anketa(callback.bot, callback.from_user.id, callback.message.chat.id,
                      owner, with_next=False)


@dp.callback_query(F.data.startswith("r:"))
async def cb_reaction(callback: CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await quick(callback)
        return
    kind = parts[1]
    owner_id = cb_target(callback, "r")
    viewer = cb_user(callback)
    viewer_id = callback.from_user.id
    owner = db.get_profile(owner_id)
    if owner is None or owner_id == viewer_id or not db.contact_allowed(viewer, owner):
        await quick(callback, "Недоступно", alert=True)
        return
    like_word, dislike_word = Database.reaction_labels(owner)
    if kind == "word":
        await quick(callback)
        await state.set_state(Form.reaction_word)
        await state.update_data(target=owner_id, cancel="go:random")
        await callback.bot.send_message(
            callback.message.chat.id,
            "%s Напишите одно слово о анкете <b>%s</b> (до %d символов)." % (
                EMOJI["word"], esc(display_name(owner)), REACTION_WORD_LIMIT),
            reply_markup=cancel_keyboard("go:random"))
        return
    if kind == "like":
        db.set_reaction(viewer_id, owner_id, "like")
        await quick(callback, like_word)
    elif kind == "dis":
        db.set_reaction(viewer_id, owner_id, "dislike")
        await quick(callback, dislike_word)
    else:
        await quick(callback)
        return
    with contextlib.suppress(TelegramAPIError):
        if kind == "like":
            await callback.bot.send_message(
                owner_id,
                "%s Ваша анкета понравилась: <b>%s</b>" % (
                    EMOJI["like"], esc(display_name(viewer))),
                reply_markup=kb([[button("%s Посмотреть" % EMOJI["card"],
                                        "v:profile:%d" % viewer_id),
                                 button("%s Написать" % EMOJI["chat"],
                                        "c:open:%d" % viewer_id)]]))
    if kind == "like" and db.has_mutual_like(viewer_id, owner_id):
        for first, second in ((viewer_id, owner), (owner_id, viewer)):
            with contextlib.suppress(TelegramAPIError):
                await callback.bot.send_message(
                    first,
                    "%s Взаимно с <b>%s</b>. Можно написать!" % (
                        EMOJI["sparkle"], esc(display_name(second))),
                    reply_markup=kb([[button("%s Написать" % EMOJI["chat"],
                                            "c:open:%d" % int(_value(second, "user_id", 0)))]]))


async def handle_friend_request(bot: Bot, from_id: int, to_id: int, chat_id: int):
    sender = db.get_profile(from_id)
    target = db.get_profile(to_id)
    if target is None:
        await bot.send_message(chat_id, "%s Профиль не найден." % EMOJI["warn"])
        return
    if not db.contact_allowed(sender, target):
        await bot.send_message(chat_id, "%s Анкета сейчас недоступна." % EMOJI["lock"])
        return
    status = db.add_friend_request(from_id, to_id)
    if status == "self":
        await bot.send_message(chat_id, "%s Это ваш собственный код." % EMOJI["card"])
        return
    if status == "friends":
        await bot.send_message(chat_id, "%s Вы уже друзья с <b>%s</b>." % (
            EMOJI["ok"], esc(display_name(target))))
        return
    if status == "already":
        await bot.send_message(chat_id, "%s Заявка уже отправлена." % EMOJI["clock"])
        return
    if status == "accepted":
        for first, second in ((from_id, target), (to_id, sender)):
            with contextlib.suppress(TelegramAPIError):
                await bot.send_message(
                    first, "%s Теперь вы друзья с <b>%s</b>. Соцсети открыты." % (
                        EMOJI["friends"], esc(display_name(second))),
                    reply_markup=kb([[button("%s Профиль" % EMOJI["card"],
                                            "v:profile:%d" % int(_value(second, "user_id", 0))),
                                      button("%s Написать" % EMOJI["chat"],
                                            "c:open:%d" % int(_value(second, "user_id", 0)))]]))
        return
    await bot.send_message(chat_id, "%s Заявка отправлена <b>%s</b>." % (
        EMOJI["friends"], esc(display_name(target))))
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(
            to_id, "%s Новая заявка в друзья от <b>%s</b>." % (
                EMOJI["bell"], esc(display_name(sender))),
            reply_markup=kb([[button("%s Принять" % EMOJI["ok"], "fr:acc:%d" % from_id),
                              button("%s Отклонить" % EMOJI["cross"], "fr:dec:%d" % from_id)],
                             [button("%s Профиль" % EMOJI["card"], "v:profile:%d" % from_id)]]))


@dp.callback_query(F.data.startswith("fr:add:"))
async def cb_friend_add(callback: CallbackQuery):
    await quick(callback)
    cb_user(callback)
    target_id = cb_target(callback, "fr:add")
    await handle_friend_request(callback.bot, callback.from_user.id, target_id,
                                callback.message.chat.id)


@dp.callback_query(F.data.startswith("fr:acc:"))
async def cb_friend_accept(callback: CallbackQuery):
    cb_user(callback)
    other_id = cb_target(callback, "fr:acc")
    if not db.accept_friend_request(other_id, callback.from_user.id):
        await quick(callback, "Заявка уже неактуальна", alert=True)
        return
    await quick(callback, "Теперь вы друзья")
    me = db.get_profile(callback.from_user.id)
    with contextlib.suppress(TelegramAPIError):
        await callback.bot.send_message(
            other_id, "%s <b>%s</b> принял(а) вашу заявку. Соцсети открыты." % (
                EMOJI["friends"], esc(display_name(me))),
            reply_markup=kb([[button("%s Написать" % EMOJI["chat"],
                                    "c:open:%d" % callback.from_user.id)]]))
    await cb_requests(callback)


@dp.callback_query(F.data.startswith("fr:dec:"))
async def cb_friend_decline(callback: CallbackQuery):
    cb_user(callback)
    other_id = cb_target(callback, "fr:dec")
    db.decline_friend_request(other_id, callback.from_user.id)
    await quick(callback, "Заявка отклонена")
    await cb_requests(callback)


@dp.callback_query(F.data.startswith("rp:"))
async def cb_report(callback: CallbackQuery):
    cb_user(callback)
    target_id = cb_target(callback, "rp")
    status = db.add_report(callback.from_user.id, target_id)
    if status == "self":
        await quick(callback, "Нельзя пожаловаться на себя")
        return
    if status == "already":
        await quick(callback, "Жалоба уже учтена")
        return
    await quick(callback, "Спасибо, проверим", alert=True)


# --------------------------------------------------------------------------- #
# Личные сообщения через бота
# --------------------------------------------------------------------------- #
@dp.callback_query(F.data.startswith("c:open:"))
async def cb_chat_open(callback: CallbackQuery, state: FSMContext):
    await quick(callback)
    viewer = cb_user(callback)
    peer_id = cb_target(callback, "c:open")
    peer = db.get_profile(peer_id)
    if peer is None or peer_id == callback.from_user.id:
        await quick(callback, "Недоступно", alert=True)
        return
    if not db.contact_allowed(viewer, peer):
        await quick(callback, "Анкета сейчас недоступна", alert=True)
        return
    await state.clear()
    db.open_chat(callback.from_user.id, peer_id)
    left = db.unlock_left(callback.from_user.id, peer_id)
    unlocked = db.socials_unlocked(callback.from_user.id, peer_id)
    tail = ("Соцсети уже открыты." if unlocked else
            "Ещё %s общения и откроются соцсети и телеграм." % time_left_text(left))
    await callback.bot.send_message(
        callback.message.chat.id,
        "%s <b>Диалог с %s</b>\n\n"
        "Пишите сюда - я передам сообщение. Ваш телеграм остаётся скрытым.\n%s" % (
            EMOJI["chat"], esc(display_name(peer)), tail),
        reply_markup=chat_keyboard(peer_id))


@dp.callback_query(F.data.startswith("c:stop:"))
async def cb_chat_stop(callback: CallbackQuery):
    cb_user(callback)
    db.close_chat(callback.from_user.id)
    await quick(callback, "Диалог завершён")
    with contextlib.suppress(TelegramAPIError):
        await callback.message.edit_reply_markup(reply_markup=None)


async def deliver_dm(bot: Bot, sender, peer_id: int, text: str):
    """Передаёт сообщение и следит за открытием соцсетей."""
    sender_id = int(_value(sender, "user_id", 0) or 0)
    peer = db.get_profile(peer_id)
    if peer is None or not db.contact_allowed(sender, peer):
        return False, "Диалог больше недоступен."
    ok, rule = safety.check_text(text, "dm")
    if not ok:
        db.add_strike(sender_id, "dm", rule, safety.safe_snippet(text))
        return False, safety.REJECT_TEXT
    try:
        await bot.send_message(
            peer_id,
            "%s <b>%s</b>\n%s" % (EMOJI["chat"], esc(display_name(sender)), esc(text)),
            reply_markup=chat_keyboard(sender_id))
    except TelegramAPIError:
        return False, "Сообщение не дошло: человек не открывал бота или заблокировал его."
    result = db.register_message(sender_id, peer_id)
    if result.get("just_unlocked"):
        for first, second in ((sender_id, peer), (peer_id, sender)):
            with contextlib.suppress(TelegramAPIError):
                await bot.send_message(
                    first,
                    "%s Вы общаетесь больше 10 минут - соцсети и телеграм "
                    "<b>%s</b> теперь видны в профиле." % (
                        EMOJI["unlock"], esc(display_name(second))),
                    reply_markup=kb([[button("%s Профиль" % EMOJI["card"],
                                            "v:profile:%d" % int(_value(second, "user_id", 0)))]]))
    return True, None


# --------------------------------------------------------------------------- #
# Состояния: ввод текста и фото
# --------------------------------------------------------------------------- #
from aiogram.filters import StateFilter  # noqa: E402  (рядом с обработчиками)


async def finish_edit(bot: Bot, user_id: int, chat_id: int, state: FSMContext,
                      notice="", target="edit"):
    await state.clear()
    profile = db.get_profile(user_id)
    if target == "anketa":
        text, keyboard = anketa_view_text(profile), anketa_keyboard(profile)
    elif target == "socials":
        text, keyboard = socials_view_text(profile), socials_keyboard()
    elif target == "settings":
        text, keyboard = settings_view_text(profile), settings_keyboard(profile)
    elif target == "friends":
        text, keyboard = friends_view_text(user_id), friends_keyboard(user_id)
    else:
        text, keyboard = edit_view_text(profile), edit_keyboard()
    if notice:
        text = "%s\n\n%s" % (notice, text)
    await menu_card(bot, user_id, chat_id, profile, text[:1024], keyboard)


def is_clear(text: str) -> bool:
    return (text or "").strip().lower() in CLEAR_WORDS


@dp.message(Form.name, F.text)
async def st_name(message: Message, state: FSMContext):
    sync_user(message)
    ok, error = db.claim_name(message.from_user.id, message.text or "", first_time=True)
    if not ok:
        await message.answer("%s %s\n\n%s" % (EMOJI["warn"], esc(error), NAME_RULES))
        return
    await drop_user_message(message)
    await state.clear()
    card_changed(message.from_user.id)
    await show_profile(message.bot, message.from_user.id, message.chat.id,
                       notice="%s Имя закреплено. Добро пожаловать!" % EMOJI["ok"],
                       force_new=True)


@dp.message(Form.rename, F.text)
async def st_rename(message: Message, state: FSMContext):
    sync_user(message)
    await drop_user_message(message)
    ok, error = db.claim_name(message.from_user.id, message.text or "")
    if not ok:
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s %s" % (EMOJI["warn"], esc(error)))
        return
    card_changed(message.from_user.id)
    await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                      notice="%s Имя изменено." % EMOJI["ok"])


@dp.message(Form.age, F.text)
async def st_age(message: Message, state: FSMContext):
    sync_user(message)
    await drop_user_message(message)
    raw = (message.text or "").strip()
    if is_clear(raw):
        db.update_profile(message.from_user.id, age=None)
        card_changed(message.from_user.id)
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s Возраст убран." % EMOJI["ok"])
        return
    digits = "".join(ch for ch in raw if ch.isdigit())[:3]
    try:
        value = int(digits)
    except ValueError:
        value = 0
    if not MIN_AGE <= value <= MAX_AGE:
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s Нужно число от %d до %d." % (
                              EMOJI["warn"], MIN_AGE, MAX_AGE))
        return
    db.update_profile(message.from_user.id, age=value)
    card_changed(message.from_user.id)
    await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                      notice="%s Возраст: %d." % (EMOJI["ok"], value))


@dp.message(Form.bio, F.text)
async def st_bio(message: Message, state: FSMContext):
    sync_user(message)
    await drop_user_message(message)
    raw = (message.text or "").strip()
    if is_clear(raw):
        db.update_profile(message.from_user.id, bio=None)
        card_changed(message.from_user.id)
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s Описание очищено." % EMOJI["ok"])
        return
    ok, rule = safety.check_text(raw, "bio")
    if not ok:
        db.add_strike(message.from_user.id, "bio", rule, safety.safe_snippet(raw))
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s %s" % (EMOJI["warn"], safety.REJECT_TEXT))
        return
    db.update_profile(message.from_user.id, bio=raw[:card.BIO_LIMIT])
    card_changed(message.from_user.id)
    await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                      notice="%s Описание обновлено." % EMOJI["ok"])


@dp.message(Form.avatar, F.photo | F.document | F.text)
async def st_avatar(message: Message, state: FSMContext):
    sync_user(message)
    if message.text and is_clear(message.text):
        await drop_user_message(message)
        db.update_profile(message.from_user.id, photo_file_id=None)
        card_changed(message.from_user.id)
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s Аватарка убрана." % EMOJI["ok"])
        return
    file_id = extract_image_file_id(message)
    if not file_id:
        await drop_user_message(message)
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s Нужно фото или картинка файлом." % EMOJI["warn"])
        return
    await drop_user_message(message)
    db.update_profile(message.from_user.id, photo_file_id=file_id)
    card_changed(message.from_user.id)
    await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                      notice="%s Аватарка обновлена." % EMOJI["ok"])


@dp.message(Form.banner, F.photo | F.document | F.text)
async def st_banner(message: Message, state: FSMContext):
    sync_user(message)
    if message.text and is_clear(message.text):
        await drop_user_message(message)
        db.update_profile(message.from_user.id, banner_file_id=None)
        card_changed(message.from_user.id)
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s Баннер убран." % EMOJI["ok"])
        return
    file_id = extract_image_file_id(message)
    if not file_id:
        await drop_user_message(message)
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s Нужна картинка." % EMOJI["warn"])
        return
    await drop_user_message(message)
    db.update_profile(message.from_user.id, banner_file_id=file_id)
    card_changed(message.from_user.id)
    await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                      notice="%s Баннер обновлён." % EMOJI["ok"])


@dp.message(Form.anketa_text, F.text)
async def st_anketa_text(message: Message, state: FSMContext):
    sync_user(message)
    await drop_user_message(message)
    raw = (message.text or "").strip()
    if is_clear(raw):
        db.update_profile(message.from_user.id, anketa_text=None)
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s Текст анкеты очищен." % EMOJI["ok"],
                          target="anketa")
        return
    ok, rule = safety.check_anketa(raw)
    if not ok:
        db.add_strike(message.from_user.id, "anketa", rule, safety.safe_snippet(raw))
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s %s" % (EMOJI["warn"], safety.REJECT_TEXT),
                          target="anketa")
        return
    db.update_profile(message.from_user.id, anketa_text=raw[:ANKETA_TEXT_LIMIT])
    await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                      notice="%s Анкета сохранена." % EMOJI["ok"], target="anketa")


@dp.message(Form.anketa_photo, F.photo | F.document | F.text)
async def st_anketa_photo(message: Message, state: FSMContext):
    sync_user(message)
    file_id = extract_image_file_id(message)
    await drop_user_message(message)
    if not file_id:
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s Нужно фото." % EMOJI["warn"], target="anketa")
        return
    total, added = db.add_anketa_photo(message.from_user.id, file_id)
    notice = ("%s Фото добавлено: %d из %d." % (EMOJI["ok"], total, MAX_ANKETA_PHOTOS)
              if added else
              "%s Больше нельзя или фото уже есть." % EMOJI["warn"])
    await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                      notice=notice, target="anketa")


@dp.message(Form.social, F.text)
async def st_social(message: Message, state: FSMContext):
    sync_user(message)
    await drop_user_message(message)
    data = await state.get_data()
    key = str(data.get("social") or "")
    raw = (message.text or "").strip()
    if key not in SOCIALS:
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          target="socials")
        return
    if is_clear(raw):
        db.set_social(message.from_user.id, key, "")
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s Убрано." % EMOJI["ok"], target="socials")
        return
    ok, rule = safety.check_text(raw, "social")
    if not ok:
        db.add_strike(message.from_user.id, "social", rule, safety.safe_snippet(raw))
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s %s" % (EMOJI["warn"], safety.REJECT_TEXT),
                          target="socials")
        return
    db.set_social(message.from_user.id, key, raw)
    await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                      notice="%s Сохранено." % EMOJI["ok"], target="socials")


@dp.message(Form.like_word, F.text)
async def st_like_word(message: Message, state: FSMContext):
    await save_label(message, state, "like")


@dp.message(Form.dislike_word, F.text)
async def st_dislike_word(message: Message, state: FSMContext):
    await save_label(message, state, "dislike")


async def save_label(message: Message, state: FSMContext, kind: str):
    sync_user(message)
    await drop_user_message(message)
    raw = (message.text or "").strip()
    if is_clear(raw):
        db.set_reaction_label(message.from_user.id, kind, None)
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s Вернул обычное слово." % EMOJI["ok"],
                          target="settings")
        return
    ok, rule = safety.check_text(raw, "label")
    if not ok:
        db.add_strike(message.from_user.id, "label", rule, safety.safe_snippet(raw))
        await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                          notice="%s %s" % (EMOJI["warn"], safety.REJECT_TEXT),
                          target="settings")
        return
    db.set_reaction_label(message.from_user.id, kind, raw[:CUSTOM_LABEL_LIMIT])
    await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                      notice="%s Слово сохранено." % EMOJI["ok"], target="settings")


@dp.message(Form.reaction_word, F.text)
async def st_reaction_word(message: Message, state: FSMContext):
    viewer = sync_user(message)
    data = await state.get_data()
    owner_id = int(data.get("target") or 0)
    raw = (message.text or "").strip()[:REACTION_WORD_LIMIT]
    await state.clear()
    owner = db.get_profile(owner_id)
    if owner is None or not raw:
        await message.answer("%s Не получилось отправить слово." % EMOJI["warn"])
        return
    ok, rule = safety.check_text(raw, "word")
    if not ok:
        db.add_strike(message.from_user.id, "word", rule, safety.safe_snippet(raw))
        await message.answer("%s %s" % (EMOJI["warn"], safety.REJECT_TEXT))
        return
    db.set_reaction(message.from_user.id, owner_id, "word", raw)
    await message.answer("%s Слово отправлено." % EMOJI["ok"],
                         reply_markup=kb([[button("%s Следующая анкета" % EMOJI["dice"],
                                                 "go:random")]]))
    with contextlib.suppress(TelegramAPIError):
        await message.bot.send_message(
            owner_id, "%s О вашей анкете сказали: <b>%s</b>" % (
                EMOJI["word"], esc(raw)),
            reply_markup=kb([[button("%s Кто это" % EMOJI["card"],
                                    "v:profile:%d" % message.from_user.id)]]))


@dp.message(Form.qr_scan, F.photo | F.document | F.text)
async def st_qr_scan(message: Message, state: FSMContext):
    sync_user(message)
    code = ""
    if message.text:
        raw = message.text.strip()
        if is_clear(raw):
            await drop_user_message(message)
            await finish_edit(message.bot, message.from_user.id, message.chat.id,
                              state, target="friends")
            return
        if "start=add_" in raw:
            code = raw.split("start=add_")[-1].strip()
        else:
            code = raw.lstrip("#").strip()
    else:
        file_id = extract_image_file_id(message)
        payload = await download_file(message.bot, file_id) if file_id else None
        if payload and qr.scanner_available():
            code = (await asyncio.to_thread(qr.decode_qr, payload)) or ""
            if "start=add_" in code:
                code = code.split("start=add_")[-1].strip()
    await drop_user_message(message)
    target = db.find_by_qr_code(code) if code else None
    if target is None:
        await finish_edit(
            message.bot, message.from_user.id, message.chat.id, state,
            notice="%s Код не распознан. Можно прислать ссылку-приглашение текстом."
                   % EMOJI["warn"],
            target="friends")
        return
    await state.clear()
    await handle_friend_request(message.bot, message.from_user.id,
                               int(target["user_id"]), message.chat.id)
    await finish_edit(message.bot, message.from_user.id, message.chat.id, state,
                      target="friends")


# --------------------------------------------------------------------------- #
# Обычные сообщения: диалог, хэш или подсказка
# --------------------------------------------------------------------------- #
@dp.message(StateFilter(None), F.text)
async def plain_text(message: Message, state: FSMContext):
    profile = sync_user(message)
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        await message.answer(HELP_HINT, reply_markup=start_keyboard())
        return
    if not _value(profile, "bot_name", ""):
        await state.set_state(Form.name)
        await message.answer("%s\n\n%s" % (WELCOME, NAME_RULES))
        return
    peer_id = db.chat_peer(message.from_user.id)
    if peer_id:
        ok, error = await deliver_dm(message.bot, profile, peer_id, raw)
        if not ok:
            await message.answer("%s %s" % (EMOJI["warn"], esc(error)),
                                 reply_markup=chat_keyboard(peer_id))
        return
    tag = parse_tag(raw)
    if tag:
        await run_search(message.bot, message.from_user.id, message.chat.id, tag)
        return
    await message.answer(HELP_HINT, reply_markup=start_keyboard())


@dp.message(StateFilter(None), F.photo | F.document | F.sticker | F.voice | F.video)
async def plain_media(message: Message):
    sync_user(message)
    peer_id = db.chat_peer(message.from_user.id)
    if peer_id:
        await message.answer(
            "%s В диалоге можно отправлять только текст." % EMOJI["warn"],
            reply_markup=chat_keyboard(peer_id))
        return
    await message.answer(
        "%s Фото принимаю только после нажатия кнопки в профиле." % EMOJI["camera"],
        reply_markup=start_keyboard())


# --------------------------------------------------------------------------- #
# Антиспам: бережёт сервер при быстрых кликах
# --------------------------------------------------------------------------- #
_LAST_ACTION = {}
_MIN_INTERVAL = 0.4


async def throttle(handler, event, data):
    user_id = None
    query = getattr(event, "callback_query", None)
    msg = getattr(event, "message", None)
    if query is not None and query.from_user:
        user_id = query.from_user.id
    elif msg is not None and msg.from_user:
        user_id = msg.from_user.id
    if user_id is not None:
        now = time.monotonic()
        if now - _LAST_ACTION.get(user_id, 0.0) < _MIN_INTERVAL:
            if query is not None:
                with contextlib.suppress(TelegramAPIError):
                    await query.answer()
            return None
        _LAST_ACTION[user_id] = now
        if len(_LAST_ACTION) > 400:
            edge = now - 300
            for key in [k for k, value in _LAST_ACTION.items() if value < edge]:
                _LAST_ACTION.pop(key, None)
    return await handler(event, data)


dp.update.outer_middleware(throttle)


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #
COMMANDS = [
    BotCommand(command="start", description="Начало"),
    BotCommand(command="profile", description="Мой профиль"),
    BotCommand(command="search", description="Поиск по хэшу"),
    BotCommand(command="random", description="Случайные анкеты"),
]


async def run() -> int:
    global db, BOT_USERNAME
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.dispatcher").addFilter(ConflictNoiseFilter())

    token = read_token()
    if not token:
        LOG.error("Нет токена. Добавьте BOT_TOKEN в переменные окружения или в .env")
        return 2

    if not acquire_single_instance_lock(token):
        LOG.error("Рядом уже работает другой экземпляр бота. Выхожу.")
        return 3

    db_path = resolve_db_path()
    db = Database(db_path)
    LOG.info("Сборка %s | база %s | размер карточки %s", BUILD, db_path, CARD_SIZE)

    report = card.font_report()
    regular = report.get("regular") or {}
    LOG.info("Шрифт %s %s [%s] кириллица=%s",
             regular.get("family"), regular.get("style"),
             regular.get("source"), regular.get("cyrillic"))
    LOG.info("Папка шрифтов: %s", card.fonts_dir_report())
    LOG.info("Сканер QR: %s",
             "включён" if qr.scanner_available() else "выключен, код можно ввести текстом")
    LOG.info("В базе: %s", db.stats())

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    watchdog = ConflictWatchdog()
    try:
        me = await preflight(bot)
        if isinstance(me, bool):
            # на сервере остался старый runtime.py - добираем данные сами
            LOG.warning("Файлы разных версий: обновите runtime.py вместе с main.py")
            me = await bot.get_me() if me else None
        if me is None:
            return 3
        BOT_USERNAME = getattr(me, "username", "") or ""
        LOG.info("Запуск @%s (id=%s)", BOT_USERNAME, getattr(me, "id", "?"))
        with contextlib.suppress(TelegramAPIError):
            await bot.set_my_commands(COMMANDS)
        watchdog.bind(asyncio.get_running_loop(), dp)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(),
                              handle_signals=True)
    finally:
        with contextlib.suppress(Exception):
            await bot.session.close()
        with contextlib.suppress(Exception):
            db.close()
        gc.collect()
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        LOG.info("Остановлено вручную")
        return 0


if __name__ == "__main__":
    sys.exit(main())
