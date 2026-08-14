# -*- coding: utf-8 -*-
"""Проверки версии 3: анкеты раз в 5 часов, диалоги, кэш карточек, защита."""
import os
import sys
import time
import tempfile

sys.path.insert(0, "/data/build")

import card
import safety
import database as dbm
from database import Database

FAILS = []


def check(name, condition, extra=""):
    if condition:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, extra))
        FAILS.append(name)


def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "test.sqlite3")
    return Database(path)


def make_user(db, user_id, name, age=None, gender=None, anketa=True):
    db.ensure_profile(user_id, "u%d" % user_id)
    ok, err = db.claim_name(user_id, name, first_time=True)
    assert ok, err
    fields = {}
    if age is not None:
        fields["age"] = age
    if gender is not None:
        fields["gender"] = gender
    if anketa:
        fields["anketa_text"] = "Люблю музыку и прогулки, ищу друзей рядом"
        fields["rules_accepted_at"] = int(time.time())
    if fields:
        db.update_profile(user_id, **fields)
    return db.get_profile(user_id)


print("1. Анкеты и пауза 5 часов")
db = fresh_db()
make_user(db, 1, "viewer", age=20)
make_user(db, 2, "target", age=21)
row = db.next_anketa(1)
check("анкета нашлась", row is not None and int(row["user_id"]) == 2)
db.mark_view(1, 2)
check("повтора сразу нет", db.next_anketa(1) is None)
wait = db.view_wait_seconds(1)
check("ждать около 5 часов", 4 * 3600 < wait <= 5 * 3600, "wait=%s" % wait)
db._execute("UPDATE views SET created_at=? WHERE viewer_id=1",
            (int(time.time()) - dbm.VIEW_TTL - 60,))
check("после 5 часов анкета вернулась", db.next_anketa(1) is not None)

print("2. Взрослые и подростки не смешиваются")
db2 = fresh_db()
teen = make_user(db2, 10, "teen", age=14)
adult = make_user(db2, 11, "adult", age=30)
unknown = make_user(db2, 12, "nobody")
check("подросток не видит взрослого", not db2.contact_allowed(teen, adult))
check("взрослый не видит подростка", not db2.contact_allowed(adult, teen))
check("без возраста контакт есть", db2.contact_allowed(adult, unknown))
feed = db2.next_anketa(10)
check("в ленте подростка нет взрослого", feed is None or int(feed["user_id"]) != 11)

print("3. Соцсети: 10 минут общения или дружба")
db3 = fresh_db()
make_user(db3, 20, "anna", age=22)
make_user(db3, 21, "boris", age=23)
db3.open_chat(20, 21)
check("соцсети закрыты сразу", not db3.socials_unlocked(20, 21))
check("до открытия 10 минут", db3.unlock_left(20, 21) == dbm.CHAT_UNLOCK_SECONDS)
low, high = (20, 21)
db3._execute("UPDATE chats SET talk_seconds=?, msgs_a=?, msgs_b=?, last_at=? "
             "WHERE a=? AND b=?", (595, 6, 6, int(time.time()) - 10, low, high))
result = db3.register_message(20, 21)
check("открылись после 10 минут", result.get("just_unlocked") is True, str(result))
check("теперь видны обоим",
      db3.socials_unlocked(20, 21) and db3.socials_unlocked(21, 20))
db3.close_chat(20)
check("диалог закрыт", db3.chat_peer(20) is None and db3.chat_peer(21) is None)
make_user(db3, 22, "clara", age=24)
check("без дружбы закрыто", not db3.socials_unlocked(20, 22))
db3.add_friend_request(20, 22)
db3.accept_friend_request(20, 22)
check("друзьям сразу открыто",
      db3.socials_unlocked(20, 22) and db3.socials_unlocked(22, 20))

print("4. Поиск по хэшу")
db4 = fresh_db()
target = make_user(db4, 30, "hashman", age=25)
tag = target["tag"]
check("хэш из 5 цифр", len(str(tag)) == 5 and str(tag).isdigit(), str(tag))
check("нашли по хэшу", db4.find_by_tag(tag) is not None)
check("нашли с решёткой", db4.find_by_tag("#" + str(tag)) is not None)
check("мусор не находится", db4.find_by_tag("99999") is None
      or int(db4.find_by_tag("99999")["user_id"]) == 30)

print("5. Кэш карточки")
db5 = fresh_db()
make_user(db5, 40, "cachy", age=19)
db5.set_card_file_id(40, "2k", "abc123", "file-1")
check("кэш вернул file_id", db5.get_card_file_id(40, "2k", "abc123") == "file-1")
check("другой хеш - нет кэша", db5.get_card_file_id(40, "2k", "zzz") is None)
db5.drop_card_cache(40)
check("кэш сброшен", db5.get_card_file_id(40, "2k", "abc123") is None)

print("6. Свои слова вместо лайка")
profile = db5.get_profile(40)
like, dislike = Database.reaction_labels(profile)
check("по умолчанию стандартные",
      like == dbm.DEFAULT_LIKE_WORD and dislike == dbm.DEFAULT_DISLIKE_WORD)
db5.set_reaction_label(40, "like", "Огонь")
db5.set_reaction_label(40, "dislike", "Не моё")
like, dislike = Database.reaction_labels(db5.get_profile(40))
check("свои слова сохранились", like == "Огонь" and dislike == "Не моё")
db5.set_reaction_label(40, "like", None)
like, _ = Database.reaction_labels(db5.get_profile(40))
check("сброс работает", like == dbm.DEFAULT_LIKE_WORD)

print("7. Правила и жалобы")
db6 = fresh_db()
make_user(db6, 50, "rulesman", age=26, anketa=False)
check("сначала правила не приняты", not Database.rules_accepted(db6.get_profile(50)))
db6.accept_rules(50)
check("правила приняты", Database.rules_accepted(db6.get_profile(50)))
for reporter in (51, 52, 53):
    db6.ensure_profile(reporter, "r%d" % reporter)
check("первая жалоба", db6.add_report(51, 50) == "created")
check("повторная жалоба", db6.add_report(51, 50) == "already")
db6.add_report(52, 50)
check("после третьей скрыт", db6.add_report(53, 50) == "hidden")
check("скрытый недоступен", db6.is_blocked(db6.get_profile(50)))

print("8. Тихая модерация: три нарушения")
db7 = fresh_db()
make_user(db7, 60, "striker", age=28)
for index in range(2):
    db7.add_strike(60, "bio", "m1", "snippet %d" % index)
check("два нарушения - ещё виден", not db7.is_blocked(db7.get_profile(60)))
db7.add_strike(60, "bio", "m1", "snippet 3")
check("три нарушения - скрыт", db7.is_blocked(db7.get_profile(60)))

print("9. Защита текстов")
bad = [
    "ищу малолетку для общения",
    "скинь нюдсы пожалуйста",
    "не говори родителям про нас",
    "приглашаем в ТЦК на службу",
    "набор в ЧВК, зову воевать",
    "передай координаты объекта",
    "м а л о л е т к а ищу",
    "куплю мефедрон закладки работают",
    "заработок без вложений, криптосигналы",
]
for text in bad:
    ok, rule = safety.check_text(text, "bio")
    check("блок: %s" % text[:28], not ok, "rule=%s" % rule)

good = [
    "Люблю музыку, футбол и гулять с друзьями",
    "Учусь в школе, играю в волейбол, ищу компанию",
    "Рисую аниме и слушаю рок, давай общаться",
    "Мне 17 лет, люблю скейт и кофе",
    "Играю в команде по баскетболу, хочу найти друзей",
]
for text in good:
    ok, rule = safety.check_text(text, "bio")
    check("пропуск: %s" % text[:28], ok, "rule=%s" % rule)

ok, rule = safety.check_anketa("Мой канал https://t.me/spamchannel заходите")
check("ссылка в анкете не проходит", not ok, "rule=%s" % rule)
ok, rule = safety.check_anketa("Люблю гитару, ищу группу для репетиций")
check("обычная анкета проходит", ok, "rule=%s" % rule)

print("10. Карточка: возраст и пол")
sizes = {}
for name, profile in (
    ("оба", {"bot_name": "Crackford", "tag": "00000", "age": 17, "gender": "male",
              "bio": "Люблю музыку и спорт", "theme": "blue"}),
    ("только возраст", {"bot_name": "Anna", "tag": "00001", "age": 20, "theme": "rose"}),
    ("только пол", {"bot_name": "Boris", "tag": "00002", "gender": "female",
                    "theme": "mint"}),
    ("ничего", {"bot_name": "Ghost", "tag": "00003", "theme": "violet"}),
):
    payload = card.make_card(profile, size="hd")
    sizes[name] = len(payload)
    check("карточка нарисована (%s)" % name, len(payload) > 10000)
print("    размеры: %s" % ", ".join("%s=%dКБ" % (k, v // 1024) for k, v in sizes.items()))

report = card.font_report()
regular = report.get("regular") or {}
bold = report.get("bold") or {}
check("шрифт Montserrat", "Montserrat" in str(regular.get("family")), str(regular))
check("жирный Montserrat", "Montserrat" in str(bold.get("family")), str(bold))
check("кириллица есть", bool(regular.get("cyrillic")) and bool(bold.get("cyrillic")),
      str(report))
check("шрифты из папки fonts", regular.get("kind") == "file", str(regular))

print("11. Скорость рендера")
sample = {"bot_name": "Speed", "tag": "00009", "age": 22, "gender": "male",
          "bio": "Тест скорости рендера карточки", "theme": "blue"}
for size in ("hd", "2k", "4k"):
    start = time.time()
    payload = card.make_card(sample, size=size)
    spent = time.time() - start
    print("    %s: %dКБ за %.2f с" % (size, len(payload) // 1024, spent))
    check("рендер %s быстрее 2 секунд" % size, spent < 2.0, "%.2f" % spent)

print()
if FAILS:
    print("НЕ ПРОШЛО: %d" % len(FAILS))
    for name in FAILS:
        print("  - %s" % name)
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОШЛИ")
