"""Быстрая проверка шрифтов: запустите на хостинге, если текст стал квадратиками.

    python check_fonts.py

Скрипт показывает, какой файл шрифта реально взят, есть ли в нем кириллица и знаки
пола, и собирает тестовую карточку font-check.png.
"""

import sys
from pathlib import Path

import card

OK = chr(0x2705)
BAD = chr(0x274C)


def main():
    print("Папка со шрифтами:", card.FONT_DIR)
    if card.FONT_DIR.is_dir():
        files = sorted(p.name for p in card.FONT_DIR.iterdir() if p.suffix.lower() in (".ttf", ".otf"))
        print("Файлы:", ", ".join(files) if files else "(пусто)")
    else:
        print(BAD, "Папки fonts/ нет - будет использован встроенный резерв")

    problems = 0
    for role, info in card.font_report().items():
        mark = OK if info["cyrillic"] and info["gender_glyphs"] else BAD
        problems += 0 if mark == OK else 1
        print("%s %-8s %s (%s)" % (mark, role, info["family"], info["style"]))
        print("     файл: %s" % info["source"])
        print("     кириллица: %s | знаки пола: %s" % (info["cyrillic"], info["gender_glyphs"]))

    demo = {
        "display_name": "Проверка",
        "tag": "12345",
        "age": 17,
        "gender": "male",
        "bio": "Если вы читаете эту строку на картинке, шрифты работают правильно.",
        "theme": "blue",
    }
    output = Path("font-check.png")
    output.write_bytes(card.render_card(demo, size="sd"))
    print("\nТестовая карточка: %s" % output.resolve())

    if problems:
        print(BAD, "Есть проблемы со шрифтами - см. README, раздел про Montserrat")
    else:
        print(OK, "Шрифты в порядке")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
