import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

checks = ["smoke.py", "sigcheck.py", "tests.py", "handlers.py"]
fails = []
for name in checks:
    code = os.system("%s %s" % (sys.executable, os.path.join(HERE, name)))
    print("%s -> %s" % (name, "OK" if code == 0 else "\u041E\u0428\u0418\u0411\u041A\u0410"))
    if code != 0:
        fails.append(name)
print()
if fails:
    print("\u041D\u0415 \u041F\u0420\u041E\u0428\u041B\u041E: %s" % ", ".join(fails))
    sys.exit(1)
print("\u0412\u0421\u0415 \u041F\u0420\u041E\u0412\u0415\u0420\u041A\u0418 \u041F\u0420\u041E\u0428\u041B\u0418 - \u043C\u043E\u0436\u043D\u043E \u0437\u0430\u043B\u0438\u0432\u0430\u0442\u044C")
