# -*- coding: utf-8 -*-
"""Поддельный aiogram: позволяет гонять обработчики бота без телеграма.

Здесь живут мини-версии F-фильтров, Command/StateFilter, Bot, Dispatcher и Message.
Используется только тестами (smoke.py, handlers.py), в бою не нужен.
"""
import asyncio
import importlib.abc
import importlib.machinery
import io
import sys
import types
from types import SimpleNamespace


# --------------------------------------------------------------------------- #
# Фильтры в стиле aiogram F
# --------------------------------------------------------------------------- #
def apply_filter(item, obj):
    if item is None:
        return True
    if callable(item):
        return bool(item(obj))
    return bool(item)


class Pred:
    def __init__(self, fn, label="pred"):
        self.fn = fn
        self.label = label

    def __call__(self, obj):
        return bool(self.fn(obj))

    def __and__(self, other):
        return Pred(lambda o: self(o) and apply_filter(other, o), "and")

    def __or__(self, other):
        return Pred(lambda o: self(o) or apply_filter(other, o), "or")

    def __invert__(self):
        return Pred(lambda o: not self(o), "not")

    def __repr__(self):
        return "Pred(%s)" % self.label


class MagicField:
    """F.data, F.text, F.photo и так далее."""

    def __init__(self, path=()):
        self._path = path

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return MagicField(self._path + (name,))

    def value(self, obj):
        current = obj
        for part in self._path:
            current = getattr(current, part, None)
            if current is None:
                return None
        return current

    def __eq__(self, other):
        return Pred(lambda o: self.value(o) == other, "%s==%r" % (self.label(), other))

    def __ne__(self, other):
        return Pred(lambda o: self.value(o) != other, "%s!=%r" % (self.label(), other))

    def in_(self, values):
        return Pred(lambda o: self.value(o) in values, "%s in %r" % (self.label(), values))

    def startswith(self, prefix):
        def test(obj):
            value = self.value(obj)
            return isinstance(value, str) and value.startswith(prefix)
        return Pred(test, "%s.startswith(%r)" % (self.label(), prefix))

    def contains(self, needle):
        def test(obj):
            value = self.value(obj)
            return isinstance(value, str) and needle in value
        return Pred(test, "contains")

    def func(self, fn):
        return Pred(lambda o: bool(fn(self.value(o))), "func")

    def label(self):
        return "F." + ".".join(self._path)

    def __call__(self, obj):
        return bool(self.value(obj))

    def __and__(self, other):
        return Pred(lambda o: bool(self.value(o)) and apply_filter(other, o), "and")

    def __or__(self, other):
        return Pred(lambda o: bool(self.value(o)) or apply_filter(other, o), "or")

    def __invert__(self):
        return Pred(lambda o: not self.value(o), "not")

    def __hash__(self):
        return hash(self._path)

    def __repr__(self):
        return self.label()


F = MagicField()


class Command:
    def __init__(self, *names, **kwargs):
        self.names = [str(name) for name in names]

    def __call__(self, obj):
        text = getattr(obj, "text", "") or ""
        if not text.startswith("/"):
            return False
        head = text[1:].split(maxsplit=1)[0] if len(text) > 1 else ""
        return head.split("@")[0] in self.names


class CommandStart:
    def __init__(self, deep_link=False, **kwargs):
        self.deep_link = deep_link

    def __call__(self, obj):
        text = getattr(obj, "text", "") or ""
        if not text.startswith("/start"):
            return False
        payload = text[len("/start"):].strip()
        return bool(payload) if self.deep_link else not payload


class CommandObject:
    def __init__(self, command="", args=None):
        self.command = command
        self.args = args


class StateFilter:
    def __init__(self, *states):
        self.states = states

    def __call__(self, obj):
        current = getattr(obj, "fsm_state", None)
        for state in self.states:
            if state is None and current is None:
                return True
            if state is not None and current is not None:
                if state_name(state) == state_name(current):
                    return True
        return False


def state_name(state):
    if state is None:
        return None
    return getattr(state, "state", None) or str(state)


class State:
    def __init__(self, name=None):
        self._name = name

    def __set_name__(self, owner, name):
        self._name = "%s:%s" % (owner.__name__, name)

    @property
    def state(self):
        return self._name

    def __repr__(self):
        return "State(%s)" % self._name


class StatesGroup:
    pass


# --------------------------------------------------------------------------- #
# Типы телеграма
# --------------------------------------------------------------------------- #
class BotCommand:
    def __init__(self, command="", description="", **kwargs):
        self.command = command
        self.description = description


class BufferedInputFile:
    def __init__(self, data, filename="file.bin", **kwargs):
        self.data = data
        self.filename = filename


class InputMediaPhoto:
    def __init__(self, media=None, caption=None, parse_mode=None, **kwargs):
        self.media = media
        self.caption = caption


class InlineKeyboardButton:
    def __init__(self, text="", callback_data=None, url=None, **kwargs):
        self.text = text
        self.callback_data = callback_data
        self.url = url


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard=None, **kwargs):
        self.inline_keyboard = inline_keyboard or []

    def datas(self):
        out = []
        for row in self.inline_keyboard:
            for button in row:
                if button.callback_data:
                    out.append(button.callback_data)
        return out


PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeBot:
    ME = SimpleNamespace(username="tochkatest_bot", id=8960511254,
                         first_name="Точка", is_bot=True)

    def __init__(self, token="test", default=None, **kwargs):
        self.token = token
        self.session = SimpleNamespace(close=self._close)
        self.sent = []
        self.edits = []
        self.deleted = []
        self.commands = None
        self.webhook_dropped = False
        self.me_error = None
        self.updates_error = None
        self.closed = False
        self._next_id = 1000

    async def _close(self):
        self.closed = True

    def new_id(self):
        self._next_id += 1
        return self._next_id

    # --- старт ---
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

    # --- сообщения ---
    async def send_message(self, chat_id, text="", reply_markup=None, **kwargs):
        record = SimpleNamespace(kind="text", chat_id=chat_id, text=text,
                                 markup=reply_markup, message_id=self.new_id())
        self.sent.append(record)
        return FakeMessage(chat_id=chat_id, user_id=chat_id, text=text, bot=self,
                           message_id=record.message_id, markup=reply_markup)

    async def send_photo(self, chat_id, photo=None, caption="", reply_markup=None,
                         **kwargs):
        record = SimpleNamespace(kind="photo", chat_id=chat_id, text=caption,
                                 markup=reply_markup, message_id=self.new_id(),
                                 photo=photo)
        self.sent.append(record)
        return FakeMessage(chat_id=chat_id, user_id=chat_id, text=caption, bot=self,
                           message_id=record.message_id, markup=reply_markup,
                           photo=[SimpleNamespace(file_id="card-%d" % record.message_id,
                                                  file_size=1024)])

    async def edit_message_caption(self, chat_id=None, message_id=None, caption="",
                                   reply_markup=None, **kwargs):
        self.edits.append(SimpleNamespace(kind="caption", chat_id=chat_id,
                                          message_id=message_id, text=caption,
                                          markup=reply_markup))
        return True

    async def edit_message_media(self, chat_id=None, message_id=None, media=None,
                                 reply_markup=None, **kwargs):
        self.edits.append(SimpleNamespace(kind="media", chat_id=chat_id,
                                          message_id=message_id,
                                          text=getattr(media, "caption", ""),
                                          markup=reply_markup))
        return True

    async def delete_message(self, chat_id=None, message_id=None, **kwargs):
        self.deleted.append((chat_id, message_id))
        return True

    # --- файлы ---
    async def get_file(self, file_id, **kwargs):
        return SimpleNamespace(file_id=file_id, file_path="photos/%s.jpg" % file_id,
                               file_size=2048)

    async def download_file(self, file_path, **kwargs):
        return io.BytesIO(PNG_1PX)

    def __getattr__(self, name):
        async def anything(*args, **kwargs):
            return True
        return anything


class FakeMessage:
    def __init__(self, chat_id=1, user_id=1, text=None, bot=None, message_id=1,
                 photo=None, username="tester", markup=None, document=None,
                 fsm_state=None):
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.from_user = SimpleNamespace(id=user_id, username=username,
                                        first_name="User%s" % user_id,
                                        is_bot=False)
        self.text = text
        self.caption = None
        self.photo = photo
        self.document = document
        self.message_id = message_id
        self.bot = bot or FakeBot()
        self.reply_markup = markup
        self.fsm_state = fsm_state
        self.answers = []
        self.deleted = False

    async def answer(self, text="", reply_markup=None, **kwargs):
        self.answers.append(text)
        return await self.bot.send_message(self.chat.id, text,
                                           reply_markup=reply_markup)

    async def answer_photo(self, photo=None, caption="", reply_markup=None, **kwargs):
        return await self.bot.send_photo(self.chat.id, photo, caption=caption,
                                        reply_markup=reply_markup)

    async def delete(self):
        self.deleted = True
        self.bot.deleted.append((self.chat.id, self.message_id))

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        self.bot.edits.append(SimpleNamespace(kind="markup", chat_id=self.chat.id,
                                              message_id=self.message_id, text="",
                                              markup=reply_markup))
        return True


class FakeCallback:
    def __init__(self, data="", user_id=1, message=None, bot=None, username="tester",
                 fsm_state=None):
        self.id = "cb-%s" % user_id
        self.data = data
        self.bot = bot or (message.bot if message else FakeBot())
        self.from_user = SimpleNamespace(id=user_id, username=username,
                                        first_name="User%s" % user_id, is_bot=False)
        self.message = message
        self.fsm_state = fsm_state
        self.alerts = []

    async def answer(self, text="", show_alert=False, **kwargs):
        self.alerts.append(text)
        return True


class FakeState:
    """Мини-FSMContext."""

    def __init__(self):
        self.current = None
        self.data = {}

    async def set_state(self, state=None):
        self.current = state

    async def get_state(self):
        return state_name(self.current)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)
        return dict(self.data)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.current = None
        self.data = {}


class RecordingDispatcher:
    def __init__(self, *args, **kwargs):
        self.message_handlers = []
        self.callback_handlers = []
        self.middlewares = []
        self.polled = False
        self.update = SimpleNamespace(outer_middleware=self._add_middleware,
                                      middleware=self._add_middleware)

    def _add_middleware(self, fn):
        self.middlewares.append(fn)
        return fn

    def message(self, *filters, **kwargs):
        def decorator(fn):
            self.message_handlers.append((filters, fn))
            return fn
        return decorator

    def callback_query(self, *filters, **kwargs):
        def decorator(fn):
            self.callback_handlers.append((filters, fn))
            return fn
        return decorator

    def edited_message(self, *filters, **kwargs):
        return self.message(*filters, **kwargs)

    def resolve_used_update_types(self):
        return ["message", "callback_query"]

    async def start_polling(self, bot, **kwargs):
        self.polled = True

    def __getattr__(self, name):
        def decorator_factory(*args, **kwargs):
            def decorator(fn):
                return fn
            return decorator
        return decorator_factory


# --------------------------------------------------------------------------- #
# Установка заглушек в sys.modules
# --------------------------------------------------------------------------- #
class TelegramAPIError(Exception):
    pass


class TelegramRetryAfter(TelegramAPIError):
    def __init__(self, message="retry", retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class TelegramConflictError(TelegramAPIError):
    pass


class TelegramBadRequest(TelegramAPIError):
    pass


class TelegramForbiddenError(TelegramAPIError):
    pass


class _StubMeta(type):
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


class _Stub(metaclass=_StubMeta):
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
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


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    ROOTS = ("aiogram", "dotenv")

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.ROOTS:
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        module = types.ModuleType(spec.name)
        module.__path__ = []
        module.__getattr__ = lambda name: _Stub
        return module

    def exec_module(self, module):
        pass


def install():
    """Подменяет aiogram заглушками. Вызывать до import main."""
    if not any(isinstance(item, _StubFinder) for item in sys.meta_path):
        sys.meta_path.insert(0, _StubFinder())

    import aiogram
    import aiogram.client.default
    import aiogram.enums
    import aiogram.exceptions
    import aiogram.filters
    import aiogram.fsm.context
    import aiogram.fsm.state
    import aiogram.fsm.storage.memory
    import aiogram.types

    aiogram.Bot = FakeBot
    aiogram.Dispatcher = RecordingDispatcher
    aiogram.F = F
    aiogram.BaseMiddleware = _Stub

    aiogram.exceptions.TelegramAPIError = TelegramAPIError
    aiogram.exceptions.TelegramRetryAfter = TelegramRetryAfter
    aiogram.exceptions.TelegramConflictError = TelegramConflictError
    aiogram.exceptions.TelegramBadRequest = TelegramBadRequest
    aiogram.exceptions.TelegramForbiddenError = TelegramForbiddenError

    aiogram.filters.Command = Command
    aiogram.filters.CommandStart = CommandStart
    aiogram.filters.CommandObject = CommandObject
    aiogram.filters.StateFilter = StateFilter

    aiogram.fsm.state.State = State
    aiogram.fsm.state.StatesGroup = StatesGroup
    aiogram.fsm.context.FSMContext = FakeState

    aiogram.types.BotCommand = BotCommand
    aiogram.types.BufferedInputFile = BufferedInputFile
    aiogram.types.InlineKeyboardButton = InlineKeyboardButton
    aiogram.types.InlineKeyboardMarkup = InlineKeyboardMarkup
    aiogram.types.InputMediaPhoto = InputMediaPhoto
    aiogram.types.Message = FakeMessage
    aiogram.types.CallbackQuery = FakeCallback
    return aiogram


# --------------------------------------------------------------------------- #
# Диспетчеризация как в aiogram: первый подходящий обработчик
# --------------------------------------------------------------------------- #
import inspect  # noqa: E402


def pick(handlers, event):
    for filters, handler in handlers:
        if all(apply_filter(item, event) for item in filters):
            return handler
    return None


async def call_handler(handler, event, state, command=None):
    signature = inspect.signature(handler)
    kwargs = {}
    for name, parameter in list(signature.parameters.items())[1:]:
        if name == "state":
            kwargs[name] = state
        elif name == "command":
            kwargs[name] = command or CommandObject()
        elif parameter.default is inspect.Parameter.empty:
            kwargs[name] = None
    return await handler(event, **kwargs)


async def sleep_zero(delay, *args, **kwargs):
    return None


def disable_throttle(main_module):
    """В тестах антиспам мешает: чистим его память."""
    store = getattr(main_module, "_LAST_ACTION", None)
    if isinstance(store, dict):
        store.clear()
