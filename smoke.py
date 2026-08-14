# -*- coding: utf-8 -*-
"""Запусковая проверка: подменяем aiogram и реально вызываем run().

Ловит ошибки старта (типа 'bool' object has no attribute 'username')
без установленного aiogram и без живого токена.
"""
import asyncio
import importlib.abc
import importlib.machinery
import os
import sys
import tempfile
import types
from types import SimpleNamespace

BUILD_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BUILD_DIR)

failures = []


def check(title, condition, extra=""):
    if condition:
        print("  ok   %s" % title)
    else:
        print("  FAIL %s %s" % (title, extra))
        failures.append(title)


# --------------------------------------------------------------------------- #
# Универсальная заглушка
# --------------------------------------------------------------------------- #
class _Meta(type):
    def __getattr__(cls, name):
        return cls

    def __and__(cls, other):
        return cls

    def __or__(cls, other):
        return cls

    def __invert__(cls):
        return cls

    def __getitem__(cls, item):
        return cls


class _Stub(metaclass=_Meta):
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        # работаем как декоратор: @dp.message(...) должен вернуть саму функцию
        if (len(args) == 1 and not kwargs
                and isinstance(args[0], (types.FunctionType, types.MethodType))):
            return args[0]
        return _Stub()

    def __getattr__(self, name):
        return _Stub()

    def __await__(self):
        async def result():
            return _Stub()
        return result().__await__()

    def __and__(self, other):
        return self

    def __or__(self, other):
        return self

    def __invert__(self):
        return self

    def __getitem__(self, item):
        return self


STUB_ROOTS = ("aiogram", "dotenv")


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in STUB_ROOTS:
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        module = types.ModuleType(spec.name)
        module.__path__ = []
        module.__getattr__ = lambda name: _Stub
        return module

    def exec_module(self, module):
        pass


sys.meta_path.insert(0, _StubFinder())

# настоящие исключения: их ловят except и contextlib.suppress
import aiogram  # noqa: E402
import aiogram.exceptions as aiogram_exceptions  # noqa: E402


class TelegramAPIError(Exception):
    pass


class TelegramRetryAfter(TelegramAPIError):
    def __init__(self, message="retry", retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class TelegramConflictError(TelegramAPIError):
    pass


aiogram_exceptions.TelegramAPIError = TelegramAPIError
aiogram_exceptions.TelegramRetryAfter = TelegramRetryAfter
aiogram_exceptions.TelegramConflictError = TelegramConflictError
aiogram_exceptions.TelegramBadRequest = type("TelegramBadRequest", (TelegramAPIError,), {})
aiogram_exceptions.TelegramForbiddenError = type(
    "TelegramForbiddenError", (TelegramAPIError,), {})


# --------------------------------------------------------------------------- #
# Поддельные Bot и Dispatcher
# --------------------------------------------------------------------------- #
class FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeBot:
    ME = SimpleNamespace(username="tochkatest_bot", id=8960511254,
                         first_name="Точка", is_bot=True)

    def __init__(self, token, default=None, **kwargs):
        self.token = token
        self.session = FakeSession()
        self.webhook_dropped = False
        self.commands = None
        self.me_error = None
        self.updates_error = None

    async def delete_webhook(self, **kwargs):
        self.webhook_dropped = True

    async def get_me(self):
        if self.me_error:
            raise self.me_error
        return self.ME

    async def get_updates(self, **kwargs):
        if self.updates_error:
            raise self.updates_error
        return []

    async def set_my_commands(self, commands, **kwargs):
        self.commands = list(commands)

    def __getattr__(self, name):
        return _Stub()


MADE_BOTS = []


def bot_factory(token, default=None, **kwargs):
    bot = FakeBot(token, default=default, **kwargs)
    MADE_BOTS.append(bot)
    return bot


class FakeDispatcher:
    def __init__(self, *args, **kwargs):
        self.polled = False
        self.poll_kwargs = None

    def resolve_used_update_types(self):
        return ["message", "callback_query"]

    async def start_polling(self, bot, **kwargs):
        self.polled = True
        self.poll_kwargs = kwargs

    def __getattr__(self, name):
        return _Stub()


aiogram.Bot = bot_factory
aiogram.Dispatcher = FakeDispatcher

# токен и база только для теста
tmpdir = tempfile.mkdtemp(prefix="tochka-smoke-")
os.environ["DB_PATH"] = os.path.join(tmpdir, "smoke.sqlite3")
os.environ.pop("BOT_TOKEN", None)

print("1. Импорт main.py")
import main  # noqa: E402

check("main.py импортируется", True)
check("команды объявлены", len(main.COMMANDS) >= 4, str(main.COMMANDS))

print("2. Запуск без токена")
code = asyncio.run(main.run())
check("без токена код выхода 2", code == 2, "получили %r" % code)

print("3. Нормальный запуск")
os.environ["BOT_TOKEN"] = "8960511254:TESTTESTTESTTESTTESTTESTTESTTESTTES"
code = asyncio.run(main.run())
check("run() завершился без ошибок", code == 0, "код %r" % code)
check("имя бота прочитано",
      main.BOT_USERNAME == "tochkatest_bot", repr(main.BOT_USERNAME))
check("имя бота - строка", isinstance(main.BOT_USERNAME, str),
      type(main.BOT_USERNAME).__name__)
bot = MADE_BOTS[-1]
check("вебхук снят", bot.webhook_dropped)
check("меню команд отправлено", bot.commands is not None and len(bot.commands) >= 4,
      str(bot.commands))
check("сессия закрыта", bot.session.closed)

print("4. Контракт preflight")
import runtime  # noqa: E402

me = asyncio.run(runtime.preflight(FakeBot("t")))
check("preflight возвращает объект бота", not isinstance(me, bool), repr(me))
check("у объекта есть username", getattr(me, "username", None) == "tochkatest_bot",
      repr(me))
check("у объекта есть id", getattr(me, "id", None) == 8960511254, repr(me))

bad = FakeBot("t")
bad.me_error = TelegramAPIError("Unauthorized")
check("плохой токен - None", asyncio.run(runtime.preflight(bad)) is None)

soft = FakeBot("t")
soft.updates_error = TelegramAPIError("Bad Gateway")
soft_me = asyncio.run(runtime.preflight(soft))
check("сетевая ошибка не мешает старту",
      getattr(soft_me, "username", None) == "tochkatest_bot", repr(soft_me))

print("5. Конфликт двух копий")
real_sleep = asyncio.sleep


async def fast_sleep(delay, *args, **kwargs):
    return await real_sleep(0)


asyncio.sleep = fast_sleep
try:
    busy = FakeBot("t")
    busy.updates_error = TelegramConflictError(
        "Conflict: terminated by other getUpdates request")
    check("занятый токен - None", asyncio.run(runtime.preflight(busy)) is None)
finally:
    asyncio.sleep = real_sleep

print()
if failures:
    print("НЕ ПРОШЛО: %d" % len(failures))
    for item in failures:
        print("  - " + item)
    sys.exit(1)
print("ЗАПУСК ПРОВЕРЕН УСПЕШНО")
