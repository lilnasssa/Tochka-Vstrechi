# -*- coding: utf-8 -*-
"""Статическая проверка всех вызовов между файлами бота.

main.py нельзя импортировать (нет aiogram), поэтому разбираем его через ast
и сверяем каждый вызов с настоящей подписью функции или метода.
"""
import ast
import importlib.abc
import importlib.machinery
import inspect
import os
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


# в песочнице нет aiogram и python-dotenv - подставляем пустые заглушки
STUB_ROOTS = ("aiogram", "dotenv")


class _Any:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _Any()

    def __getattr__(self, name):
        return _Any()


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in STUB_ROOTS:
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        module = types.ModuleType(spec.name)
        module.__path__ = []
        module.__getattr__ = lambda name: _Any()
        return module

    def exec_module(self, module):
        pass


sys.meta_path.insert(0, _StubFinder())

import card
import database
import qr
import runtime
import safety

MODULES = {
    "card": card,
    "qr": qr,
    "safety": safety,
    "runtime": runtime,
    "database": database,
}

SOURCE = "/data/build/main.py"
with open(SOURCE, encoding="utf-8") as handle:
    text = handle.read()
tree = ast.parse(text)

# 1. что main.py импортирует из наших модулей
imported = {}
for node in ast.walk(tree):
    if isinstance(node, ast.ImportFrom) and node.module in MODULES:
        module = MODULES[node.module]
        for alias in node.names:
            local = alias.asname or alias.name
            if not hasattr(module, alias.name):
                print("НЕТ ИМЕНИ: from %s import %s" % (node.module, alias.name))
                continue
            imported[local] = (node.module + "." + alias.name,
                               getattr(module, alias.name))

print("импортировано имён из наших модулей: %d" % len(imported))


def signature_of(obj):
    try:
        return inspect.signature(obj)
    except (TypeError, ValueError):
        return None


problems = []


def check_call(node, label, obj, drop_self=False):
    if not callable(obj):
        return
    signature = signature_of(obj)
    if signature is None:
        return
    args = []
    for argument in node.args:
        if isinstance(argument, ast.Starred):
            return  # распаковку не проверяем
        args.append(object())
    kwargs = {}
    for keyword in node.keywords:
        if keyword.arg is None:
            return
        kwargs[keyword.arg] = object()
    if drop_self:
        args.insert(0, object())
    try:
        signature.bind(*args, **kwargs)
    except TypeError as error:
        problems.append("строка %d: %s(...) -> %s | подпись %s%s" % (
            node.lineno, label, error, label, signature))


# методы Database вызываются как db.<name>(...)
DB_METHODS = {
    name: value for name, value in inspect.getmembers(database.Database)
    if not name.startswith("__")
}

calls = 0
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    target = node.func
    if isinstance(target, ast.Name) and target.id in imported:
        label, obj = imported[target.id]
        calls += 1
        check_call(node, label, obj)
    elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        owner = target.value.id
        name = target.attr
        if owner in MODULES:
            module = MODULES[owner]
            if not hasattr(module, name):
                problems.append("строка %d: %s.%s не существует" % (node.lineno, owner, name))
                continue
            calls += 1
            check_call(node, "%s.%s" % (owner, name), getattr(module, name))
        elif owner == "db":
            if name not in DB_METHODS:
                problems.append("строка %d: у Database нет метода %s" % (node.lineno, name))
                continue
            calls += 1
            member = DB_METHODS[name]
            drop_self = isinstance(member, type(check_call))
            check_call(node, "db." + name, member, drop_self=drop_self)

print("проверено вызовов: %d" % calls)

# 2. обращения к константам наших модулей (card.X, runtime.X и т.п.)
for node in ast.walk(tree):
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        owner = node.value.id
        if owner in MODULES and not hasattr(MODULES[owner], node.attr):
            problems.append("строка %d: %s.%s не существует" % (node.lineno, owner, node.attr))

# 3. словари EMOJI и SOCIAL_EMOJI: ключи, которых нет
for node in ast.walk(tree):
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
            and node.value.id in ("EMOJI", "SOCIAL_EMOJI")
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)):
        table = getattr(runtime, node.value.id, {})
        if node.slice.value not in table:
            problems.append("строка %d: %s[%r] отсутствует" % (
                node.lineno, node.value.id, node.slice.value))

# 4. SOCIALS: форма значений (используется как SOCIALS[key][0]/[1])
for key, value in database.SOCIALS.items():
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        problems.append("SOCIALS[%r] = %r - ожидалась пара (название, шаблон)" % (key, value))

seen = set()
unique = []
for item in problems:
    if item not in seen:
        seen.add(item)
        unique.append(item)

if unique:
    print("\nПРОБЛЕМЫ: %d" % len(unique))
    for item in unique:
        print("  - " + item)
    sys.exit(1)

print("\nВСЕ ВЫЗОВЫ СОГЛАСОВАНЫ")
