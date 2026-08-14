# -*- coding: utf-8 -*-
"""Прогон всех обработчиков бота без телеграма.

Щёлкаем каждую кнопку и вводим текст как живой пользователь,
ловим любое исключение и показываем, какие кнопки вообще не обрабатываются.
"""
import asyncio
import logging
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import harness  # noqa: E402

harness.install()

tmpdir = tempfile.mkdtemp(prefix="tochka-handlers-")
os.environ["DB_PATH"] = os.path.join(tmpdir, "handlers.sqlite3")
os.environ["BOT_TOKEN"] = "8960511254:TESTTESTTESTTESTTESTTESTTESTTESTTES"

logging.disable(logging.CRITICAL)

import main  # noqa: E402
from database import Database  # noqa: E402

ALICE = 1001
BOB = 1002

failures = []
steps = 0
used_callbacks = set()


def fail(title, error):
    lines = traceback.format_exception_only(type(error), error)
    failures.append((title, lines[-1].strip()))
    print("  FAIL %s -> %s" % (title, lines[-1].strip()))


class User:
    def __init__(self, user_id, username):
        self.id = user_id
        self.username = username
        self.state = harness.FakeState()
        self.menu = None


async def send(user, text, title=None):
    """Пользователь пишет текст."""
    global steps
    label = title or ("текст %r" % text)
    message = harness.FakeMessage(chat_id=user.id, user_id=user.id, text=text,
                                 bot=BOT, message_id=BOT.new_id(),
                                 username=user.username,
                                 fsm_state=user.state.current)
    handler = harness.pick(DP.message_handlers, message)
    if handler is None:
        failures.append((label, "нет обработчика"))
        print("  FAIL %s -> нет обработчика" % label)
        return None
    command = None
    if text and text.startswith("/"):
        head, _, tail = text[1:].partition(" ")
        command = harness.CommandObject(command=head, args=tail.strip() or None)
    steps += 1
    try:
        await harness.call_handler(handler, message, user.state, command=command)
    except Exception as error:  # noqa: BLE001
        fail(label, error)
    return message


async def photo(user, title="фото"):
    """Пользователь присылает фото."""
    global steps
    sizes = [type("P", (), {"file_id": "photo-%s" % user.id, "file_size": 4096})()]
    message = harness.FakeMessage(chat_id=user.id, user_id=user.id, text=None,
                                 bot=BOT, message_id=BOT.new_id(),
                                 photo=sizes, username=user.username,
                                 fsm_state=user.state.current)
    handler = harness.pick(DP.message_handlers, message)
    if handler is None:
        failures.append((title, "нет обработчика для фото"))
        print("  FAIL %s -> нет обработчика для фото" % title)
        return
    steps += 1
    try:
        await harness.call_handler(handler, message, user.state)
    except Exception as error:  # noqa: BLE001
        fail(title, error)


async def click(user, data, title=None):
    """Пользователь жмёт кнопку."""
    global steps
    label = title or ("кнопка %s" % data)
    menu = harness.FakeMessage(chat_id=user.id, user_id=user.id, text="меню",
                               bot=BOT, message_id=user.menu or BOT.new_id(),
                               username=user.username)
    callback = harness.FakeCallback(data=data, user_id=user.id, message=menu, bot=BOT,
                                   username=user.username,
                                   fsm_state=user.state.current)
    handler = harness.pick(DP.callback_handlers, callback)
    if handler is None:
        failures.append((label, "нет обработчика"))
        print("  FAIL %s -> нет обработчика" % label)
        return
    used_callbacks.add(handler.__name__)
    steps += 1
    try:
        await harness.call_handler(handler, callback, user.state)
    except Exception as error:  # noqa: BLE001
        fail(label, error)


async def scenario():
    alice = User(ALICE, "alice_tg")
    bob = User(BOB, "bob_tg")

    print("1. Первый запуск и имя")
    await send(alice, "/start")
    await send(bob, "/start")
    await send(alice, "alisa", "имя alisa")
    await send(bob, "bobby", "имя bobby")

    print("2. Заполнение профиля")
    for user, age, bio in ((alice, "17", "Люблю музыку и скейт"),
                           (bob, "16", "Играю в баскетбол")):
        await click(user, "p:edit")
        await click(user, "e:age")
        await send(user, age, "возраст %s" % age)
        await click(user, "e:bio")
        await send(user, bio, "описание")
        await click(user, "e:gender")
        await click(user, "e:g:male")
        await click(user, "e:themes")
        await click(user, "e:th:blue")
        await click(user, "e:avatar")
        await photo(user, "аватарка")
        await click(user, "e:banner")
        await photo(user, "баннер")

    print("3. Карточка и меню")
    await send(alice, "/profile")
    for data in ("p:root", "p:refresh", "p:edit", "p:share", "p:qr", "p:friends",
                 "p:reactions", "p:search", "p:anketa", "p:socials", "p:settings",
                 "go:profile", "noop"):
        await click(alice, data)

    print("4. Анкета и правила")
    for user, text in ((alice, "Ищу друзей для прогулок и музыки"),
                      (bob, "Играю в баскетбол, ищу команду")):
        await click(user, "p:anketa")
        await click(user, "a:rules")
        await click(user, "a:accept")
        await click(user, "a:text")
        await send(user, text, "текст анкеты")
        await click(user, "a:photo")
        await photo(user, "фото анкеты")
        await click(user, "a:toggle")
        await click(user, "a:preview")
        await click(user, "a:clear")

    print("5. Запрещённые тексты")
    await click(alice, "a:text")
    await send(alice, "ищу малолетку для интима", "запрещённый текст анкеты")
    await click(alice, "a:text")
    await send(alice, "мой канал https://t.me/spam", "ссылка в анкете")
    await click(alice, "a:text")
    await send(alice, "Ищу друзей для прогулок и музыки", "возврат нормального текста")

    print("6. Соцсети и настройки")
    await click(alice, "p:socials")
    await click(alice, "e:soc:telegram")
    await send(alice, "alice_tg", "логин telegram")
    await click(alice, "e:soc:tiktok")
    await send(alice, "-", "очистка tiktok")
    await click(alice, "p:settings")
    await click(alice, "s:modes")
    await click(alice, "s:mode:words")
    await click(alice, "s:like")
    await send(alice, "Класс", "своё слово лайка")
    await click(alice, "s:dislike")
    await send(alice, "Мимо", "своё слово дизлайка")
    await click(alice, "s:reset")
    await click(alice, "e:name")
    await send(alice, "alisa2", "переименование раньше 48 часов")

    print("7. Поиск анкет и реакции")
    bob_tag = main.db.ensure_profile(BOB)["tag"]
    await send(alice, "/random")
    await click(alice, "go:random")
    await send(alice, "/search #%s" % bob_tag, "/search по хэшу")
    await send(alice, "/search", "/search без аргумента")
    await send(alice, "/search вася", "/search мусором")
    await send(alice, "#%s" % bob_tag, "хэш сообщением")
    await click(alice, "v:profile:%s" % BOB)
    await click(alice, "v:anketa:%s" % BOB)
    await click(alice, "r:like:%s" % BOB)
    await click(alice, "r:dis:%s" % BOB)
    await click(alice, "r:word:%s" % BOB)
    await send(alice, "Крутое фото", "слово-реакция")
    await click(alice, "p:reactions")

    print("8. Дружба и QR")
    await click(alice, "fr:add:%s" % BOB)
    await click(bob, "p:friends")
    await click(bob, "p:requests")
    await click(bob, "fr:acc:%s" % ALICE)
    await click(alice, "p:friendlist")
    await click(alice, "p:scan")
    code = main.db.ensure_profile(BOB)["qr_code"]
    await send(alice, "https://t.me/tochkatest_bot?start=add_%s" % code, "ссылка QR")
    await send(bob, "/start add_%s" % main.db.ensure_profile(ALICE)["qr_code"],
               "deep link дружбы")
    await click(bob, "fr:dec:%s" % ALICE)

    print("9. Личные сообщения")
    await click(alice, "c:open:%s" % BOB)
    await send(alice, "Привет, как дела?", "лс первое сообщение")
    await send(alice, "Скинь нюдсы", "лс запрещённый текст")
    await click(alice, "c:stop:%s" % BOB)
    await click(alice, "rp:%s" % BOB)

    print("10. Повторные клики по всем кнопкам профиля")
    for data in ("p:root", "p:edit", "p:share", "p:qr", "p:friends", "p:reactions",
                 "p:search", "p:anketa", "p:socials", "p:settings", "p:refresh"):
        await click(alice, data, "повтор %s" % data)


async def unknown_buttons():
    """Каждая кнопка из нарисованных клавиатур должна иметь обработчик."""
    print("11. Все кнопки из отправленных клавиатур")
    datas = set()
    for record in BOT.sent + BOT.edits:
        markup = getattr(record, "markup", None)
        if markup is None:
            continue
        for row in getattr(markup, "inline_keyboard", []):
            for button in row:
                if getattr(button, "callback_data", None):
                    datas.add(button.callback_data)
    print("    всего разных кнопок: %d" % len(datas))
    orphans = []
    for data in sorted(datas):
        probe = harness.FakeCallback(data=data, user_id=ALICE,
                                     message=harness.FakeMessage(chat_id=ALICE,
                                                                 user_id=ALICE,
                                                                 bot=BOT),
                                     bot=BOT)
        if harness.pick(DP.callback_handlers, probe) is None:
            orphans.append(data)
    if orphans:
        for data in orphans:
            failures.append(("кнопка %s" % data, "нет обработчика"))
            print("  FAIL кнопка %s ничего не делает" % data)
    else:
        print("  ok   у всех кнопок есть обработчик")


async def coverage():
    print("12. Обработчики без проверки")
    names = [handler.__name__ for _, handler in DP.callback_handlers]
    missed = [name for name in names if name not in used_callbacks]
    print("    обработчиков кнопок: %d, проверено: %d"
          % (len(names), len(names) - len(missed)))
    if missed:
        print("    не проверены: %s" % ", ".join(sorted(set(missed))))


async def amain():
    global BOT, DP
    BOT = harness.FakeBot()
    DP = main.dp
    main.db = Database(os.environ["DB_PATH"])
    main.BOT_USERNAME = "tochkatest_bot"
    harness.disable_throttle(main)

    print("Обработчиков сообщений: %d, кнопок: %d"
          % (len(DP.message_handlers), len(DP.callback_handlers)))
    await scenario()
    await unknown_buttons()
    await coverage()
    main.db.close()


BOT = None
DP = None
asyncio.run(amain())

print()
print("шагов выполнено: %d" % steps)
if failures:
    print("НЕ ПРОШЛО: %d" % len(failures))
    for title, reason in failures:
        print("  - %s: %s" % (title, reason))
    sys.exit(1)
print("ВСЕ ОБРАБО��ЧИКИ РАБОТАЮТ")
