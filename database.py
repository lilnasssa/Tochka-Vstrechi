"""Слой данных бота "Точка встречи" (версия 2).

Главные отличия от версии 1:
  * имя в боте (bot_name) вместо телеграм-юзернейма, уникальное, смена раз в 48 часов;
  * анкета (текст + до 5 фото) отдельно от профиля (карточки);
  * соцсети со ссылками;
  * свободные реакции словами вместо лайка/дизлайка;
  * друзья и заявки в друзья, в том числе через QR;
  * история просмотров для аккуратного /random.

Старая база подхватывается автоматически: новые столбцы добавляются на месте,
а старые display_name/username переносятся в bot_name, где это возможно.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

LOG = logging.getLogger("tochka.db")

# --------------------------------------------------------------------------- #
# Правила имени в боте
# --------------------------------------------------------------------------- #
NAME_MIN = 3
NAME_MAX = 20
NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
NAME_COOLDOWN = 48 * 3600  # секунды

RESERVED_NAMES = {
    "admin", "administrator", "root", "bot", "support", "help", "tochka",
    "moderator", "mod", "official", "telegram", "start", "profile", "search",
    "random", "settings", "null", "none", "undefined", "me", "you",
}

# Соцсети: ключ -> (название, шаблон ссылки по нику)
SOCIALS = {
    "tiktok": ("TikTok", "https://www.tiktok.com/@%s"),
    "blink": ("Blink", ""),
    "telegram": ("Telegram", "https://t.me/%s"),
    "discord": ("Discord", ""),
    "instagram": ("Instagram", "https://instagram.com/%s"),
    "vk": ("VK", "https://vk.com/%s"),
    "youtube": ("YouTube", "https://youtube.com/@%s"),
    "pinterest": ("Pinterest", "https://pinterest.com/%s"),
}

SOCIAL_ORDER = ("tiktok", "blink", "telegram", "discord",
                "instagram", "vk", "youtube", "pinterest")

REACTION_MODES = ("buttons", "words", "both")
REACTION_MODE_TITLES = {
    "buttons": "Только лайк и дизлайк",
    "words": "Только свои слова",
    "both": "И кнопки, и свои слова",
}

MAX_ANKETA_PHOTOS = 5
ANKETA_TEXT_LIMIT = 600
REACTION_WORD_LIMIT = 40

# Одну и ту же анкету можно увидеть снова через 5 часов.
VIEW_TTL = 5 * 3600
# Сколько надо общаться, чтобы открылись соцсети и телеграм.
CHAT_UNLOCK_SECONDS = 10 * 60
CHAT_UNLOCK_MESSAGES = 6

DEFAULT_LIKE_WORD = "Нравится"
DEFAULT_DISLIKE_WORD = "Не очень"
CUSTOM_LABEL_LIMIT = 18

# Возрастные границы и порог совершеннолетия (разные группы не пересекаются).
MIN_AGE = 12
MAX_AGE = 99
ADULT_AGE = 18
STRIKES_TO_HIDE = 3
REPORTS_TO_HIDE = 3

EDITABLE = (
    "tg_username", "bot_name", "bot_name_lower", "name_changed_at",
    "age", "gender", "bio", "theme", "photo_file_id", "banner_file_id",
    "anketa_text", "anketa_photos", "socials", "reaction_mode",
    "anketa_active", "qr_code",
    "like_word", "dislike_word", "rules_accepted_at",
    "strikes", "hidden", "banned_until", "last_seen",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    user_id         INTEGER PRIMARY KEY,
    tg_username     TEXT,
    bot_name        TEXT,
    bot_name_lower  TEXT,
    name_changed_at INTEGER,
    tag             TEXT,
    age             INTEGER,
    gender          TEXT,
    bio             TEXT,
    theme           TEXT DEFAULT 'blue',
    photo_file_id   TEXT,
    banner_file_id  TEXT,
    anketa_text     TEXT,
    anketa_photos   TEXT DEFAULT '[]',
    socials         TEXT DEFAULT '{}',
    reaction_mode   TEXT DEFAULT 'buttons',
    anketa_active   INTEGER DEFAULT 1,
    qr_code         TEXT,
    created_at      INTEGER,
    updated_at      INTEGER
);

CREATE INDEX IF NOT EXISTS profiles_tag_idx ON profiles(tag);

CREATE TABLE IF NOT EXISTS reactions (
    from_user_id INTEGER NOT NULL,
    to_user_id   INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    word         TEXT,
    created_at   INTEGER,
    PRIMARY KEY (from_user_id, to_user_id, kind, word)
);
CREATE INDEX IF NOT EXISTS reactions_to_idx ON reactions(to_user_id);

CREATE TABLE IF NOT EXISTS friends (
    user_id    INTEGER NOT NULL,
    friend_id  INTEGER NOT NULL,
    created_at INTEGER,
    PRIMARY KEY (user_id, friend_id)
);

CREATE TABLE IF NOT EXISTS friend_requests (
    from_user_id INTEGER NOT NULL,
    to_user_id   INTEGER NOT NULL,
    status       TEXT DEFAULT 'pending',
    source       TEXT,
    created_at   INTEGER,
    PRIMARY KEY (from_user_id, to_user_id)
);
CREATE INDEX IF NOT EXISTS requests_to_idx ON friend_requests(to_user_id, status);

CREATE TABLE IF NOT EXISTS views (
    viewer_id  INTEGER NOT NULL,
    target_id  INTEGER NOT NULL,
    created_at INTEGER,
    PRIMARY KEY (viewer_id, target_id)
);
"""

# Таблицы версии 3: личные сообщения, кэш карточек и модерация.
# Создаются отдельно, чтобы старая база тоже их получила.
SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS chats (
    a            INTEGER NOT NULL,
    b            INTEGER NOT NULL,
    started_at   INTEGER,
    last_at      INTEGER,
    talk_seconds INTEGER DEFAULT 0,
    msgs_a       INTEGER DEFAULT 0,
    msgs_b       INTEGER DEFAULT 0,
    unlocked_at  INTEGER,
    PRIMARY KEY (a, b)
);

CREATE TABLE IF NOT EXISTS chat_state (
    user_id    INTEGER PRIMARY KEY,
    peer_id    INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS card_cache (
    user_id    INTEGER NOT NULL,
    size       TEXT NOT NULL,
    hash       TEXT,
    file_id    TEXT,
    updated_at INTEGER,
    PRIMARY KEY (user_id, size)
);

CREATE TABLE IF NOT EXISTS reports (
    from_user_id INTEGER NOT NULL,
    to_user_id   INTEGER NOT NULL,
    created_at   INTEGER,
    PRIMARY KEY (from_user_id, to_user_id)
);
CREATE INDEX IF NOT EXISTS reports_to_idx ON reports(to_user_id);

CREATE TABLE IF NOT EXISTS mod_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    field      TEXT,
    rule       TEXT,
    snippet    TEXT,
    created_at INTEGER
);

CREATE INDEX IF NOT EXISTS views_viewer_idx ON views(viewer_id, created_at);
"""


def make_tag(user_id: int) -> str:
    return "%05d" % (int(user_id) % 100000)


def make_qr_code() -> str:
    return secrets.token_hex(4)


def normalize_name(value: str) -> str:
    return (value or "").strip().lstrip("@")


def validate_name(value: str):
    """Возвращает (имя, None) или (None, причина отказа)."""
    name = normalize_name(value)
    if not name:
        return None, "Пустое имя."
    if len(name) < NAME_MIN:
        return None, "Слишком короткое: минимум %d символа." % NAME_MIN
    if len(name) > NAME_MAX:
        return None, "Слишком длинное: максимум %d символов." % NAME_MAX
    if not NAME_RE.match(name):
        return None, "Можно только латинские буквы, цифры и подчёркивание."
    if name.lower() in RESERVED_NAMES:
        return None, "Это имя зарезервировано."
    return name, None


def social_link(key: str, value: str) -> str:
    """Собирает ссылку: если человек ввёл урл - оставляем, иначе подставляем шаблон."""
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("t.me/") or value.startswith("www."):
        return "https://" + value
    template = SOCIALS.get(key, ("", ""))[1]
    if template:
        return template % value.lstrip("@")
    return ""


class Database:
    def __init__(self, path: str = "data/bot.sqlite3"):
        self.path = path
        self._lock = threading.Lock()
        self._conn = None
        self.init()

    # ------------------------------------------------------------------ #
    # Служебное
    # ------------------------------------------------------------------ #
    def init(self):
        target = self.path
        if target != ":memory:":
            try:
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                LOG.error("Не создать папку для базы (%s) - работаю в памяти", exc)
                target = ":memory:"
        try:
            self._conn = sqlite3.connect(target, check_same_thread=False)
        except sqlite3.Error as exc:
            LOG.error("База недоступна (%s) - работаю в памяти", exc)
            target = ":memory:"
            self._conn = sqlite3.connect(target, check_same_thread=False)
        self.path = target
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if target != ":memory:":
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.Error:
                    pass
            self._conn.execute("PRAGMA busy_timeout=5000")
            # быстрее запись и меньше обращений к диску при десятках людей
            for pragma in ("PRAGMA synchronous=NORMAL",
                           "PRAGMA temp_store=MEMORY",
                           "PRAGMA cache_size=-4000"):
                try:
                    self._conn.execute(pragma)
                except sqlite3.Error:
                    pass
            self._conn.executescript(SCHEMA)
            self._conn.executescript(SCHEMA_V3)
            self._conn.commit()
        self._migrate()
        self._create_indexes()

    # Индексы по новым столбцам создаём только после миграции, иначе на базе
    # версии 1 столбцов ещё нет и sqlite падает.
    INDEXES = (
        "CREATE UNIQUE INDEX IF NOT EXISTS profiles_name_idx"
        " ON profiles(bot_name_lower) WHERE bot_name_lower IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS profiles_qr_idx"
        " ON profiles(qr_code) WHERE qr_code IS NOT NULL",
    )

    def _create_indexes(self):
        with self._lock:
            for statement in self.INDEXES:
                try:
                    self._conn.execute(statement)
                except sqlite3.Error as exc:
                    LOG.warning("Индекс не создан: %s", exc)
            self._conn.commit()

    def _migrate(self):
        """Добавляет новые столбцы в базу версии 1 и переносит имена."""
        with self._lock:
            columns = {row["name"] for row in
                       self._conn.execute("PRAGMA table_info(profiles)")}
            additions = {
                "banner_file_id": "TEXT",
                "photo_file_id": "TEXT",
                "theme": "TEXT DEFAULT 'blue'",
                "bio": "TEXT",
                "age": "INTEGER",
                "gender": "TEXT DEFAULT 'other'",
                "tag": "TEXT",
                "created_at": "INTEGER",
                "updated_at": "INTEGER",
                "tg_username": "TEXT",
                "bot_name": "TEXT",
                "bot_name_lower": "TEXT",
                "name_changed_at": "INTEGER",
                "anketa_text": "TEXT",
                "anketa_photos": "TEXT DEFAULT '[]'",
                "socials": "TEXT DEFAULT '{}'",
                "reaction_mode": "TEXT DEFAULT 'buttons'",
                "anketa_active": "INTEGER DEFAULT 1",
                "qr_code": "TEXT",
                "like_word": "TEXT",
                "dislike_word": "TEXT",
                "rules_accepted_at": "INTEGER",
                "strikes": "INTEGER DEFAULT 0",
                "hidden": "INTEGER DEFAULT 0",
                "banned_until": "INTEGER",
                "last_seen": "INTEGER",
            }
            for name, decl in additions.items():
                if name not in columns:
                    try:
                        self._conn.execute(
                            "ALTER TABLE profiles ADD COLUMN %s %s" % (name, decl))
                        LOG.info("База: добавлен столбец %s", name)
                    except sqlite3.Error as exc:
                        LOG.warning("Не добавить столбец %s: %s", name, exc)

            # старые имена в новое поле, если они годятся и свободны
            if "username" in columns or "display_name" in columns:
                rows = self._conn.execute(
                    "SELECT user_id, %s%s FROM profiles WHERE bot_name IS NULL" % (
                        "username" if "username" in columns else "NULL AS username",
                        ", display_name" if "display_name" in columns else "",
                    )).fetchall()
                taken = set()
                for row in self._conn.execute(
                        "SELECT bot_name_lower FROM profiles WHERE bot_name_lower IS NOT NULL"):
                    taken.add(row[0])
                for row in rows:
                    keys = row.keys()
                    candidates = [row[key] for key in ("username", "display_name")
                                  if key in keys and row[key]]
                    for candidate in candidates:
                        name, error = validate_name(candidate)
                        if error or name.lower() in taken:
                            continue
                        try:
                            self._conn.execute(
                                "UPDATE profiles SET bot_name=?, bot_name_lower=? "
                                "WHERE user_id=?", (name, name.lower(), row["user_id"]))
                            taken.add(name.lower())
                        except sqlite3.Error:
                            pass
                        break

            # коды QR тем, у кого их нет
            missing = self._conn.execute(
                "SELECT user_id FROM profiles WHERE qr_code IS NULL").fetchall()
            for row in missing:
                for _ in range(5):
                    code = make_qr_code()
                    try:
                        self._conn.execute("UPDATE profiles SET qr_code=? WHERE user_id=?",
                                           (code, row["user_id"]))
                        break
                    except sqlite3.IntegrityError:
                        continue
            self._conn.commit()

    def _execute(self, query: str, params=()):
        with self._lock:
            cursor = self._conn.execute(query, params)
            self._conn.commit()
            return cursor

    def _fetchone(self, query: str, params=()):
        with self._lock:
            return self._conn.execute(query, params).fetchone()

    def _fetchall(self, query: str, params=()):
        with self._lock:
            return self._conn.execute(query, params).fetchall()

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------ #
    # Профили
    # ------------------------------------------------------------------ #
    def ensure_profile(self, user_id: int, tg_username: str = None) -> sqlite3.Row:
        now = int(time.time())
        row = self.get_profile(user_id)
        if row is None:
            code = make_qr_code()
            self._execute(
                "INSERT OR IGNORE INTO profiles "
                "(user_id, tg_username, tag, theme, anketa_photos, socials, "
                " reaction_mode, anketa_active, qr_code, created_at, updated_at) "
                "VALUES (?, ?, ?, 'blue', '[]', '{}', 'buttons', 1, ?, ?, ?)",
                (user_id, tg_username, make_tag(user_id), code, now, now))
        elif tg_username and row["tg_username"] != tg_username:
            self.update_profile(user_id, tg_username=tg_username)
        return self.get_profile(user_id)

    def update_profile(self, user_id: int, **fields):
        allowed = {key: value for key, value in fields.items() if key in EDITABLE}
        if not allowed:
            return
        allowed["updated_at"] = int(time.time())
        assignments = ", ".join("%s=?" % key for key in allowed)
        params = list(allowed.values()) + [user_id]
        self._execute("UPDATE profiles SET %s WHERE user_id=?" % assignments, params)

    def get_profile(self, user_id: int):
        return self._fetchone("SELECT * FROM profiles WHERE user_id=?", (user_id,))

    def find_by_name(self, name: str):
        name = normalize_name(name).lower()
        if not name:
            return None
        return self._fetchone("SELECT * FROM profiles WHERE bot_name_lower=?", (name,))

    def find_by_tag(self, tag: str):
        tag = (tag or "").strip().lstrip("#")
        if not tag:
            return None
        return self._fetchone("SELECT * FROM profiles WHERE tag=?", (tag,))

    def find_by_qr_code(self, code: str):
        code = (code or "").strip().lower()
        if not code:
            return None
        return self._fetchone("SELECT * FROM profiles WHERE qr_code=?", (code,))

    def name_is_free(self, name: str, exclude_user_id: int = None) -> bool:
        row = self.find_by_name(name)
        if row is None:
            return True
        return exclude_user_id is not None and row["user_id"] == exclude_user_id

    def name_cooldown_left(self, user_id: int) -> int:
        """Сколько секунд осталось до следующей смены имени (0 - можно сейчас)."""
        row = self.get_profile(user_id)
        if row is None or not row["name_changed_at"]:
            return 0
        left = NAME_COOLDOWN - (int(time.time()) - int(row["name_changed_at"]))
        return max(0, left)

    def claim_name(self, user_id: int, name: str, first_time: bool = False):
        """Занимает имя. Возвращает (True, None) или (False, причина)."""
        clean, error = validate_name(name)
        if error:
            return False, error
        if not self.name_is_free(clean, exclude_user_id=user_id):
            return False, "Имя уже занято, придумайте другое."
        if not first_time:
            left = self.name_cooldown_left(user_id)
            if left > 0:
                return False, "Менять имя можно раз в 48 часов."
        try:
            self._execute(
                "UPDATE profiles SET bot_name=?, bot_name_lower=?, name_changed_at=?, "
                "updated_at=? WHERE user_id=?",
                (clean, clean.lower(), int(time.time()), int(time.time()), user_id))
        except sqlite3.IntegrityError:
            return False, "Имя уже занято, придумайте другое."
        return True, None

    def count_profiles(self) -> int:
        row = self._fetchone("SELECT COUNT(*) AS total FROM profiles")
        return int(row["total"]) if row else 0

    # ------------------------------------------------------------------ #
    # Соцсети
    # ------------------------------------------------------------------ #
    def get_socials(self, user_id: int) -> dict:
        row = self.get_profile(user_id)
        if row is None:
            return {}
        try:
            data = json.loads(row["socials"] or "{}")
        except (ValueError, TypeError):
            return {}
        return {key: value for key, value in data.items()
                if key in SOCIALS and value}

    def set_social(self, user_id: int, key: str, value: str):
        if key not in SOCIALS:
            return
        data = self.get_socials(user_id)
        value = (value or "").strip()
        if value:
            data[key] = value[:200]
        else:
            data.pop(key, None)
        self.update_profile(user_id, socials=json.dumps(data, ensure_ascii=False))

    # ------------------------------------------------------------------ #
    # Анкета
    # ------------------------------------------------------------------ #
    def get_anketa_photos(self, user_id: int) -> list:
        row = self.get_profile(user_id)
        if row is None:
            return []
        try:
            data = json.loads(row["anketa_photos"] or "[]")
        except (ValueError, TypeError):
            return []
        return [item for item in data if isinstance(item, str)][:MAX_ANKETA_PHOTOS]

    def add_anketa_photo(self, user_id: int, file_id: str):
        photos = self.get_anketa_photos(user_id)
        if file_id in photos:
            return len(photos), False
        if len(photos) >= MAX_ANKETA_PHOTOS:
            return len(photos), False
        photos.append(file_id)
        self.update_profile(user_id, anketa_photos=json.dumps(photos))
        return len(photos), True

    def clear_anketa_photos(self, user_id: int):
        self.update_profile(user_id, anketa_photos="[]")

    def anketa_is_ready(self, row) -> bool:
        if row is None:
            return False
        keys = row.keys()
        has_text = bool("anketa_text" in keys and (row["anketa_text"] or "").strip())
        has_photo = bool(self.get_anketa_photos(row["user_id"]))
        active = not ("anketa_active" in keys) or bool(row["anketa_active"])
        return active and bool(row["bot_name"]) and (has_text or has_photo)

    # ------------------------------------------------------------------ #
    # Поиск анкет для /random
    # ------------------------------------------------------------------ #
    def next_anketa(self, viewer_id: int, fresh_only: bool = True):
        """Следующая анкета.

        Учитывает три правила: увиденное возвращается только через 5 часов,
        скрытые и заблокированные не показываются, а возрастные группы
        (до 18 и от 18) между собой не пересекаются.
        """
        now = int(time.time())
        viewer = self.get_profile(viewer_id)
        bracket = self.age_bracket(viewer)
        params = [viewer_id, now]
        base = (
            "SELECT p.* FROM profiles p WHERE p.user_id != ? "
            "AND p.bot_name IS NOT NULL AND COALESCE(p.anketa_active, 1) = 1 "
            "AND COALESCE(p.hidden, 0) = 0 "
            "AND COALESCE(p.banned_until, 0) < ? "
            "AND (COALESCE(TRIM(p.anketa_text), '') != '' "
            "     OR COALESCE(p.anketa_photos, '[]') NOT IN ('[]', '')) "
        )
        if bracket == "minor":
            base += "AND p.age IS NOT NULL AND p.age < ? "
            params.append(ADULT_AGE)
        elif bracket == "adult":
            base += "AND (p.age IS NULL OR p.age >= ?) "
            params.append(ADULT_AGE)
        if fresh_only:
            base += ("AND p.user_id NOT IN (SELECT target_id FROM views "
                     "WHERE viewer_id = ? AND COALESCE(created_at, 0) > ?) ")
            params.extend([viewer_id, now - VIEW_TTL])
        row = self._fetchone(base + "ORDER BY RANDOM() LIMIT 1", tuple(params))
        if row is None and fresh_only:
            # свежих нет: чистим только старые отметки, свежие бережём
            self.prune_views(viewer_id)
            return None
        return row

    def mark_view(self, viewer_id: int, target_id: int):
        self._execute(
            "INSERT OR REPLACE INTO views (viewer_id, target_id, created_at) "
            "VALUES (?, ?, ?)", (viewer_id, target_id, int(time.time())))

    def prune_views(self, viewer_id: int = None):
        """Убирает отметки просмотра старше 5 часов."""
        edge = int(time.time()) - VIEW_TTL
        if viewer_id is None:
            self._execute("DELETE FROM views WHERE COALESCE(created_at, 0) <= ?", (edge,))
        else:
            self._execute(
                "DELETE FROM views WHERE viewer_id=? AND COALESCE(created_at, 0) <= ?",
                (viewer_id, edge))

    def view_wait_seconds(self, viewer_id: int) -> int:
        """Сколько ждать до ближайшей повторной анкеты."""
        row = self._fetchone(
            "SELECT MIN(created_at) AS oldest FROM views WHERE viewer_id=?", (viewer_id,))
        if not row or row["oldest"] in (None, ""):
            return 0
        left = int(row["oldest"]) + VIEW_TTL - int(time.time())
        return max(0, left)

    def reset_views(self, viewer_id: int):
        self._execute("DELETE FROM views WHERE viewer_id=?", (viewer_id,))

    # ------------------------------------------------------------------ #
    # Реакции
    # ------------------------------------------------------------------ #
    def set_reaction(self, from_user_id: int, to_user_id: int,
                     kind: str, word: str = None):
        if kind in ("like", "dislike"):
            self._execute(
                "DELETE FROM reactions WHERE from_user_id=? AND to_user_id=? "
                "AND kind IN ('like','dislike')", (from_user_id, to_user_id))
        self._execute(
            "INSERT OR REPLACE INTO reactions "
            "(from_user_id, to_user_id, kind, word, created_at) VALUES (?, ?, ?, ?, ?)",
            (from_user_id, to_user_id, kind, (word or "")[:REACTION_WORD_LIMIT],
             int(time.time())))

    def get_reaction(self, from_user_id: int, to_user_id: int):
        return self._fetchone(
            "SELECT * FROM reactions WHERE from_user_id=? AND to_user_id=? "
            "AND kind IN ('like','dislike') LIMIT 1", (from_user_id, to_user_id))

    def has_mutual_like(self, a: int, b: int) -> bool:
        row = self._fetchone(
            "SELECT COUNT(*) AS total FROM reactions "
            "WHERE kind='like' AND ((from_user_id=? AND to_user_id=?) "
            "OR (from_user_id=? AND to_user_id=?))", (a, b, b, a))
        return bool(row and int(row["total"]) >= 2)

    def reaction_stats(self, user_id: int) -> dict:
        rows = self._fetchall(
            "SELECT kind, COUNT(*) AS total FROM reactions WHERE to_user_id=? "
            "GROUP BY kind", (user_id,))
        stats = {"like": 0, "dislike": 0, "word": 0}
        for row in rows:
            stats[row["kind"]] = int(row["total"])
        return stats

    def recent_words(self, user_id: int, limit: int = 10) -> list:
        rows = self._fetchall(
            "SELECT word, created_at FROM reactions WHERE to_user_id=? AND kind='word' "
            "AND word IS NOT NULL AND word != '' ORDER BY created_at DESC LIMIT ?",
            (user_id, limit))
        return [row["word"] for row in rows]

    # ------------------------------------------------------------------ #
    # Друзья и заявки
    # ------------------------------------------------------------------ #
    def are_friends(self, a: int, b: int) -> bool:
        row = self._fetchone(
            "SELECT 1 FROM friends WHERE user_id=? AND friend_id=?", (a, b))
        return row is not None

    def add_friend_request(self, from_user_id: int, to_user_id: int,
                           source: str = "button"):
        """Возвращает статус: friends / already / created / accepted."""
        if from_user_id == to_user_id:
            return "self"
        if self.are_friends(from_user_id, to_user_id):
            return "friends"
        # если есть встречная заявка - сразу друзья
        incoming = self._fetchone(
            "SELECT * FROM friend_requests WHERE from_user_id=? AND to_user_id=? "
            "AND status='pending'", (to_user_id, from_user_id))
        if incoming is not None:
            self.accept_friend_request(to_user_id, from_user_id)
            return "accepted"
        existing = self._fetchone(
            "SELECT * FROM friend_requests WHERE from_user_id=? AND to_user_id=? "
            "AND status='pending'", (from_user_id, to_user_id))
        if existing is not None:
            return "already"
        self._execute(
            "INSERT OR REPLACE INTO friend_requests "
            "(from_user_id, to_user_id, status, source, created_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (from_user_id, to_user_id, source, int(time.time())))
        return "created"

    def accept_friend_request(self, from_user_id: int, to_user_id: int) -> bool:
        row = self._fetchone(
            "SELECT * FROM friend_requests WHERE from_user_id=? AND to_user_id=? "
            "AND status='pending'", (from_user_id, to_user_id))
        if row is None:
            return False
        now = int(time.time())
        self._execute(
            "UPDATE friend_requests SET status='accepted' "
            "WHERE from_user_id=? AND to_user_id=?", (from_user_id, to_user_id))
        self._execute(
            "INSERT OR IGNORE INTO friends (user_id, friend_id, created_at) "
            "VALUES (?, ?, ?)", (from_user_id, to_user_id, now))
        self._execute(
            "INSERT OR IGNORE INTO friends (user_id, friend_id, created_at) "
            "VALUES (?, ?, ?)", (to_user_id, from_user_id, now))
        return True

    def decline_friend_request(self, from_user_id: int, to_user_id: int) -> bool:
        cursor = self._execute(
            "UPDATE friend_requests SET status='declined' "
            "WHERE from_user_id=? AND to_user_id=? AND status='pending'",
            (from_user_id, to_user_id))
        return cursor.rowcount > 0

    def remove_friend(self, user_id: int, friend_id: int):
        self._execute("DELETE FROM friends WHERE user_id=? AND friend_id=?",
                      (user_id, friend_id))
        self._execute("DELETE FROM friends WHERE user_id=? AND friend_id=?",
                      (friend_id, user_id))
        self._execute("DELETE FROM friend_requests WHERE "
                      "(from_user_id=? AND to_user_id=?) OR "
                      "(from_user_id=? AND to_user_id=?)",
                      (user_id, friend_id, friend_id, user_id))

    def get_friends(self, user_id: int, limit: int = 50) -> list:
        return self._fetchall(
            "SELECT p.* FROM friends f JOIN profiles p ON p.user_id = f.friend_id "
            "WHERE f.user_id=? ORDER BY f.created_at DESC LIMIT ?", (user_id, limit))

    def incoming_requests(self, user_id: int, limit: int = 30) -> list:
        return self._fetchall(
            "SELECT p.*, r.created_at AS requested_at, r.source AS request_source "
            "FROM friend_requests r JOIN profiles p ON p.user_id = r.from_user_id "
            "WHERE r.to_user_id=? AND r.status='pending' "
            "ORDER BY r.created_at DESC LIMIT ?", (user_id, limit))

    def count_incoming_requests(self, user_id: int) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) AS total FROM friend_requests "
            "WHERE to_user_id=? AND status='pending'", (user_id,))
        return int(row["total"]) if row else 0

    def count_friends(self, user_id: int) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) AS total FROM friends WHERE user_id=?", (user_id,))
        return int(row["total"]) if row else 0

    # ------------------------------------------------------------------ #
    # Возрастные группы и доступ
    # ------------------------------------------------------------------ #
    @staticmethod
    def age_of(row):
        if row is None:
            return None
        try:
            if "age" not in row.keys():
                return None
        except AttributeError:
            pass
        try:
            value = int(row["age"])
        except (TypeError, ValueError, KeyError, IndexError):
            return None
        if MIN_AGE <= value <= MAX_AGE:
            return value
        return None

    @classmethod
    def age_bracket(cls, row):
        """minor - до 18, adult - от 18, unknown - возраст не указан."""
        age = cls.age_of(row)
        if age is None:
            return "unknown"
        return "minor" if age < ADULT_AGE else "adult"

    def contact_allowed(self, a_row, b_row) -> bool:
        """Можно ли этим двоим видеть друг друга и переписываться."""
        if a_row is None or b_row is None:
            return False
        if self.is_blocked(b_row) or self.is_blocked(a_row):
            return False
        first = self.age_bracket(a_row)
        second = self.age_bracket(b_row)
        if "minor" in (first, second) and first != second:
            return False
        return True

    def is_blocked(self, row) -> bool:
        if row is None:
            return True
        keys = row.keys()
        if "hidden" in keys and row["hidden"]:
            return True
        if "banned_until" in keys and row["banned_until"]:
            try:
                return int(row["banned_until"]) > int(time.time())
            except (TypeError, ValueError):
                return False
        return False

    def touch_seen(self, user_id: int):
        self._execute("UPDATE profiles SET last_seen=? WHERE user_id=?",
                      (int(time.time()), user_id))

    # ------------------------------------------------------------------ #
    # Свои слова вместо лайка и дизлайка
    # ------------------------------------------------------------------ #
    @staticmethod
    def reaction_labels(row) -> tuple:
        like, dislike = DEFAULT_LIKE_WORD, DEFAULT_DISLIKE_WORD
        if row is not None:
            keys = row.keys()
            if "like_word" in keys and (row["like_word"] or "").strip():
                like = row["like_word"].strip()
            if "dislike_word" in keys and (row["dislike_word"] or "").strip():
                dislike = row["dislike_word"].strip()
        return like, dislike

    def set_reaction_label(self, user_id: int, kind: str, value):
        field = "like_word" if kind == "like" else "dislike_word"
        text = (value or "").strip()[:CUSTOM_LABEL_LIMIT] or None
        self._execute("UPDATE profiles SET %s=?, updated_at=? WHERE user_id=?" % field,
                      (text, int(time.time()), user_id))

    # ------------------------------------------------------------------ #
    # Правила публикации анкет
    # ------------------------------------------------------------------ #
    @staticmethod
    def rules_accepted(row) -> bool:
        if row is None:
            return False
        keys = row.keys()
        return bool("rules_accepted_at" in keys and row["rules_accepted_at"])

    def accept_rules(self, user_id: int):
        self._execute("UPDATE profiles SET rules_accepted_at=? WHERE user_id=?",
                      (int(time.time()), user_id))

    # ------------------------------------------------------------------ #
    # Кэш готовых карточек (file_id из телеграма)
    # ------------------------------------------------------------------ #
    def get_card_file_id(self, user_id: int, size: str, digest: str):
        row = self._fetchone(
            "SELECT file_id FROM card_cache WHERE user_id=? AND size=? AND hash=?",
            (user_id, str(size), str(digest)))
        return row["file_id"] if row and row["file_id"] else None

    def set_card_file_id(self, user_id: int, size: str, digest: str, file_id: str):
        self._execute(
            "INSERT OR REPLACE INTO card_cache (user_id, size, hash, file_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, str(size), str(digest), file_id, int(time.time())))

    def drop_card_cache(self, user_id: int):
        self._execute("DELETE FROM card_cache WHERE user_id=?", (user_id,))

    # ------------------------------------------------------------------ #
    # Личные сообщения через бота
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pair(a: int, b: int) -> tuple:
        return (a, b) if int(a) <= int(b) else (b, a)

    def chat_peer(self, user_id: int):
        row = self._fetchone("SELECT peer_id FROM chat_state WHERE user_id=?", (user_id,))
        if row and row["peer_id"]:
            return int(row["peer_id"])
        return None

    def open_chat(self, user_id: int, peer_id: int):
        now = int(time.time())
        low, high = self._pair(user_id, peer_id)
        self._execute(
            "INSERT OR IGNORE INTO chats (a, b, started_at, last_at, talk_seconds) "
            "VALUES (?, ?, ?, ?, 0)", (low, high, now, now))
        for me, other in ((user_id, peer_id), (peer_id, user_id)):
            self._execute(
                "INSERT OR REPLACE INTO chat_state (user_id, peer_id, updated_at) "
                "VALUES (?, ?, ?)", (me, other, now))

    def close_chat(self, user_id: int):
        peer = self.chat_peer(user_id)
        self._execute("DELETE FROM chat_state WHERE user_id=?", (user_id,))
        if peer:
            self._execute("DELETE FROM chat_state WHERE user_id=? AND peer_id=?",
                          (peer, user_id))
        return peer

    def chat_row(self, a: int, b: int):
        low, high = self._pair(a, b)
        return self._fetchone("SELECT * FROM chats WHERE a=? AND b=?", (low, high))

    def register_message(self, sender_id: int, peer_id: int) -> dict:
        """Считает время и сообщения в диалоге, открывает соцсети после 10 минут."""
        now = int(time.time())
        low, high = self._pair(sender_id, peer_id)
        row = self.chat_row(sender_id, peer_id)
        if row is None:
            self.open_chat(sender_id, peer_id)
            row = self.chat_row(sender_id, peer_id)
        last = int(row["last_at"] or now)
        # пауза больше 15 минут не считается общением
        delta = now - last
        gain = delta if 0 < delta <= 900 else 0
        seconds = int(row["talk_seconds"] or 0) + gain
        column = "msgs_a" if int(sender_id) == int(low) else "msgs_b"
        self._execute(
            "UPDATE chats SET last_at=?, talk_seconds=?, %s=COALESCE(%s, 0) + 1 "
            "WHERE a=? AND b=?" % (column, column), (now, seconds, low, high))
        row = self.chat_row(sender_id, peer_id)
        unlocked_at = row["unlocked_at"]
        both = min(int(row["msgs_a"] or 0), int(row["msgs_b"] or 0))
        if not unlocked_at and seconds >= CHAT_UNLOCK_SECONDS and both >= CHAT_UNLOCK_MESSAGES:
            self._execute("UPDATE chats SET unlocked_at=? WHERE a=? AND b=?",
                          (now, low, high))
            return {"seconds": seconds, "unlocked": True, "just_unlocked": True}
        return {"seconds": seconds, "unlocked": bool(unlocked_at), "just_unlocked": False}

    def chat_seconds(self, a: int, b: int) -> int:
        row = self.chat_row(a, b)
        return int(row["talk_seconds"] or 0) if row else 0

    def socials_unlocked(self, viewer_id: int, owner_id: int) -> bool:
        """Соцсети открыты друзьям сразу, остальным - после 10 минут общения."""
        if int(viewer_id) == int(owner_id):
            return True
        if self.are_friends(viewer_id, owner_id):
            return True
        row = self.chat_row(viewer_id, owner_id)
        return bool(row and row["unlocked_at"])

    def unlock_left(self, viewer_id: int, owner_id: int) -> int:
        """Сколько секунд общения осталось до открытия соцсетей."""
        row = self.chat_row(viewer_id, owner_id)
        seconds = int(row["talk_seconds"] or 0) if row else 0
        return max(0, CHAT_UNLOCK_SECONDS - seconds)

    def active_chats(self, user_id: int, limit: int = 20) -> list:
        return self._fetchall(
            "SELECT p.* FROM chat_state s JOIN profiles p ON p.user_id = s.peer_id "
            "WHERE s.user_id=? ORDER BY s.updated_at DESC LIMIT ?", (user_id, limit))

    # ------------------------------------------------------------------ #
    # Жалобы и тихая модерация
    # ------------------------------------------------------------------ #
    def add_report(self, from_user_id: int, to_user_id: int) -> str:
        if int(from_user_id) == int(to_user_id):
            return "self"
        existing = self._fetchone(
            "SELECT 1 FROM reports WHERE from_user_id=? AND to_user_id=?",
            (from_user_id, to_user_id))
        if existing:
            return "already"
        self._execute(
            "INSERT OR REPLACE INTO reports (from_user_id, to_user_id, created_at) "
            "VALUES (?, ?, ?)", (from_user_id, to_user_id, int(time.time())))
        if self.count_reports(to_user_id) >= REPORTS_TO_HIDE:
            self.hide_profile(to_user_id)
            return "hidden"
        return "created"

    def count_reports(self, user_id: int) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) AS total FROM reports WHERE to_user_id=?", (user_id,))
        return int(row["total"]) if row else 0

    def hide_profile(self, user_id: int, hidden: bool = True):
        self._execute("UPDATE profiles SET hidden=?, anketa_active=? WHERE user_id=?",
                      (1 if hidden else 0, 0 if hidden else 1, user_id))

    def add_strike(self, user_id: int, field: str, rule: str, snippet: str) -> int:
        """Тихо запоминает нарушение и прячет анкету после трёх раз."""
        now = int(time.time())
        self._execute(
            "INSERT INTO mod_log (user_id, field, rule, snippet, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, str(field)[:40], str(rule)[:40], str(snippet)[:180], now))
        self._execute(
            "UPDATE profiles SET strikes = COALESCE(strikes, 0) + 1 WHERE user_id=?",
            (user_id,))
        row = self._fetchone("SELECT strikes FROM profiles WHERE user_id=?", (user_id,))
        strikes = int(row["strikes"] or 0) if row else 0
        if strikes >= STRIKES_TO_HIDE:
            self.hide_profile(user_id)
        return strikes

    def stats(self) -> dict:
        """Короткая сводка для логов."""
        def one(query, params=()):
            row = self._fetchone(query, params)
            return int(row[0]) if row else 0
        return {
            "profiles": one("SELECT COUNT(*) FROM profiles"),
            "anketas": one("SELECT COUNT(*) FROM profiles WHERE COALESCE(anketa_active,1)=1 "
                           "AND (COALESCE(TRIM(anketa_text),'') != '' "
                           "OR COALESCE(anketa_photos,'[]') NOT IN ('[]',''))"),
            "chats": one("SELECT COUNT(*) FROM chats"),
            "friends": one("SELECT COUNT(*) FROM friends"),
        }
