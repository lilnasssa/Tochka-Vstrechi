"""QR-коды для бота "Точка встречи".

Генерация написана вручную (только Pillow), чтобы не тащить лишние зависимости:
режим byte, уровень коррекции L, версии 1-6 (до 134 байт полезной нагрузки) -
этого с запасом хватает на ссылку вида https://t.me/bot?start=add_abcdef.

Сканирование (распознавание присланной фотографии) делается через OpenCV, если
он установлен. Если библиотеки нет, бот просто предложит ввести код текстом.
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageDraw

LOG = logging.getLogger("tochka.qr")

# --------------------------------------------------------------------------- #
# Таблицы стандарта (уровень коррекции L, по одному блоку на версию)
# --------------------------------------------------------------------------- #
# версия: (всего кодовых слов, слов данных, слов коррекции)
CAPACITY_L = {
    1: (26, 19, 7),
    2: (44, 34, 10),
    3: (70, 55, 15),
    4: (100, 80, 20),
    5: (134, 108, 26),
}

# Координаты центров выравнивающих узоров
ALIGNMENT = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
}

QUIET_ZONE = 4


# --------------------------------------------------------------------------- #
# Арифметика в поле Галуа GF(256) для кодов Рида-Соломона
# --------------------------------------------------------------------------- #
_EXP = [0] * 512
_LOG = [0] * 256


def _init_tables():
    value = 1
    for i in range(255):
        _EXP[i] = value
        _LOG[value] = i
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_tables()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator_poly(degree: int):
    poly = [1]
    for i in range(degree):
        poly = _poly_mul(poly, [1, _EXP[i]])
    return poly


def _poly_mul(a, b):
    result = [0] * (len(a) + len(b) - 1)
    for i, av in enumerate(a):
        if av == 0:
            continue
        for j, bv in enumerate(b):
            if bv == 0:
                continue
            result[i + j] ^= _mul(av, bv)
    return result


def _ecc_codewords(data: list, count: int) -> list:
    generator = _generator_poly(count)
    remainder = list(data) + [0] * count
    for i in range(len(data)):
        factor = remainder[i]
        if factor == 0:
            continue
        for j, gv in enumerate(generator):
            remainder[i + j] ^= _mul(gv, factor)
    return remainder[len(data):]


# --------------------------------------------------------------------------- #
# Кодирование данных
# --------------------------------------------------------------------------- #
def _pick_version(length: int) -> int:
    for version in sorted(CAPACITY_L):
        # 4 бита режим + 8 бит длина + сами данные + терминатор
        if CAPACITY_L[version][1] >= length + 2:
            return version
    raise ValueError("Слишком длинная строка для QR (максимум 106 байт)")


def _encode_data(payload: bytes, version: int) -> list:
    total, data_words, _ = CAPACITY_L[version]
    bits = []

    def push(value: int, width: int):
        for shift in range(width - 1, -1, -1):
            bits.append((value >> shift) & 1)

    push(0b0100, 4)          # режим byte
    push(len(payload), 8)    # длина (версии 1-9: 8 бит)
    for byte in payload:
        push(byte, 8)

    capacity_bits = data_words * 8
    if len(bits) > capacity_bits:
        raise ValueError("Данные не влезают в выбранную версию QR")

    # терминатор
    for _ in range(min(4, capacity_bits - len(bits))):
        bits.append(0)
    # выравнивание до целого байта
    while len(bits) % 8:
        bits.append(0)

    words = [int("".join(str(bit) for bit in bits[i:i + 8]), 2)
             for i in range(0, len(bits), 8)]
    # добивочные байты
    pad = (0xEC, 0x11)
    index = 0
    while len(words) < data_words:
        words.append(pad[index % 2])
        index += 1

    ecc = _ecc_codewords(words, total - data_words)
    return words + ecc


# --------------------------------------------------------------------------- #
# Построение матрицы
# --------------------------------------------------------------------------- #
class _Matrix:
    def __init__(self, version: int):
        self.version = version
        self.size = 17 + 4 * version
        self.modules = [[None] * self.size for _ in range(self.size)]
        self.reserved = [[False] * self.size for _ in range(self.size)]

    def set(self, row: int, col: int, value: int, reserve: bool = True):
        self.modules[row][col] = value
        if reserve:
            self.reserved[row][col] = True

    def draw_finder(self, row: int, col: int):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = row + r, col + c
                if not (0 <= rr < self.size and 0 <= cc < self.size):
                    continue
                if r in (0, 6) and 0 <= c <= 6:
                    value = 1
                elif c in (0, 6) and 0 <= r <= 6:
                    value = 1
                elif 2 <= r <= 4 and 2 <= c <= 4:
                    value = 1
                else:
                    value = 0
                self.set(rr, cc, value)

    def draw_alignment(self):
        centers = ALIGNMENT[self.version]
        for row in centers:
            for col in centers:
                # углы заняты поисковыми узорами
                if (row < 9 and col < 9) or (row < 9 and col > self.size - 10) \
                        or (row > self.size - 10 and col < 9):
                    continue
                for r in range(-2, 3):
                    for c in range(-2, 3):
                        value = 1 if max(abs(r), abs(c)) != 1 else 0
                        self.set(row + r, col + c, value)

    def draw_timing(self):
        for i in range(8, self.size - 8):
            value = 1 if i % 2 == 0 else 0
            self.set(6, i, value)
            self.set(i, 6, value)

    def reserve_format(self):
        for i in range(9):
            if self.modules[8][i] is None:
                self.set(8, i, 0)
            if self.modules[i][8] is None:
                self.set(i, 8, 0)
        for i in range(8):
            self.set(8, self.size - 1 - i, 0)
            self.set(self.size - 1 - i, 8, 0)
        # тёмный модуль
        self.set(self.size - 8, 8, 1)

    def place_data(self, codewords: list):
        bits = []
        for word in codewords:
            for shift in range(7, -1, -1):
                bits.append((word >> shift) & 1)

        index = 0
        upward = True
        col = self.size - 1
        while col > 0:
            if col == 6:  # столбец синхронизации
                col -= 1
            rows = range(self.size - 1, -1, -1) if upward else range(self.size)
            for row in rows:
                for offset in (0, 1):
                    cc = col - offset
                    if self.reserved[row][cc]:
                        continue
                    bit = bits[index] if index < len(bits) else 0
                    index += 1
                    self.modules[row][cc] = bit
            upward = not upward
            col -= 2

    def apply_mask(self, mask: int):
        for row in range(self.size):
            for col in range(self.size):
                if self.reserved[row][col]:
                    continue
                if _mask_condition(mask, row, col):
                    self.modules[row][col] ^= 1

    def draw_format(self, mask: int):
        bits = _format_bits(mask)
        # первая копия: вокруг левого верхнего поискового узора
        positions = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
                     (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
        for bit, (row, col) in zip(bits, positions):
            self.modules[row][col] = bit
        # вторая копия
        for i in range(7):
            self.modules[self.size - 1 - i][8] = bits[i]
        for i in range(7, 15):
            self.modules[8][self.size - 15 + i] = bits[i]
        self.modules[self.size - 8][8] = 1

    def penalty(self) -> int:
        size = self.size
        score = 0
        # правило 1: серии одинаковых модулей
        for line in list(self.modules) + [list(col) for col in zip(*self.modules)]:
            run_value, run_length = line[0], 1
            for value in line[1:]:
                if value == run_value:
                    run_length += 1
                else:
                    if run_length >= 5:
                        score += 3 + (run_length - 5)
                    run_value, run_length = value, 1
            if run_length >= 5:
                score += 3 + (run_length - 5)
        # правило 2: блоки 2x2
        for row in range(size - 1):
            for col in range(size - 1):
                block = (self.modules[row][col], self.modules[row][col + 1],
                         self.modules[row + 1][col], self.modules[row + 1][col + 1])
                if len(set(block)) == 1:
                    score += 3
        # правило 3: узор 1:1:3:1:1
        pattern = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
        reverse = list(reversed(pattern))
        for line in list(self.modules) + [list(col) for col in zip(*self.modules)]:
            for i in range(size - 10):
                window = line[i:i + 11]
                if window == pattern or window == reverse:
                    score += 40
        # правило 4: перекос баланса тёмного
        dark = sum(sum(row) for row in self.modules)
        percent = dark * 100 // (size * size)
        score += 10 * (abs(percent - 50) // 5)
        return score


def _mask_condition(mask: int, row: int, col: int) -> bool:
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


def _format_bits(mask: int) -> list:
    # уровень коррекции L = 01, дальше номер маски
    data = (0b01 << 3) | mask
    generator = 0b10100110111
    # деление по модулю порождающего полинома
    remainder = data << 10
    while remainder.bit_length() > 10:
        remainder ^= generator << (remainder.bit_length() - 11)
    combined = ((data << 10) | remainder) ^ 0b101010000010010
    return [(combined >> shift) & 1 for shift in range(14, -1, -1)]


def _build_matrix(payload: bytes) -> _Matrix:
    version = _pick_version(len(payload))
    codewords = _encode_data(payload, version)

    best = None
    best_score = None
    for mask in range(8):
        matrix = _Matrix(version)
        matrix.draw_finder(0, 0)
        matrix.draw_finder(0, matrix.size - 7)
        matrix.draw_finder(matrix.size - 7, 0)
        matrix.draw_alignment()
        matrix.draw_timing()
        matrix.reserve_format()
        matrix.place_data(codewords)
        matrix.apply_mask(mask)
        matrix.draw_format(mask)
        score = matrix.penalty()
        if best_score is None or score < best_score:
            best, best_score = matrix, score
    return best


# --------------------------------------------------------------------------- #
# Публичное API
# --------------------------------------------------------------------------- #
def make_qr(text: str, size: int = 1024, dark=(20, 24, 32), light=(255, 255, 255),
            label: str = "") -> bytes:
    """PNG с QR-кодом для строки text."""
    matrix = _build_matrix(str(text).encode("utf-8"))
    modules = matrix.size + QUIET_ZONE * 2
    scale = max(1, size // modules)
    side = modules * scale

    image = Image.new("RGB", (side, side), light)
    draw = ImageDraw.Draw(image)
    for row in range(matrix.size):
        for col in range(matrix.size):
            if not matrix.modules[row][col]:
                continue
            x = (col + QUIET_ZONE) * scale
            y = (row + QUIET_ZONE) * scale
            draw.rectangle((x, y, x + scale - 1, y + scale - 1), fill=dark)

    if image.size[0] != size:
        image = image.resize((size, size), Image.NEAREST)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def scanner_available() -> bool:
    try:
        import cv2  # noqa: F401
    except Exception:
        return False
    return True


def decode_qr(image_bytes: bytes):
    """Читает QR с картинки. Возвращает строку или None."""
    try:
        import cv2
        import numpy
    except Exception:
        LOG.info("OpenCV не установлен - сканер QR недоступен")
        return None
    try:
        array = numpy.frombuffer(image_bytes, dtype=numpy.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if image is None:
            return None
        detector = cv2.QRCodeDetector()
        text, points, _ = detector.detectAndDecode(image)
        if text:
            return text
        # вторая попытка: увеличиваем и повышаем контраст
        bigger = cv2.resize(image, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(bigger, cv2.COLOR_BGR2GRAY)
        gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        text, points, _ = detector.detectAndDecode(gray)
        return text or None
    except Exception as exc:
        LOG.warning("Не смог прочитать QR: %s", exc)
        return None


if __name__ == "__main__":
    sample = "https://t.me/tochkavstrechimytischi_bot?start=add_a1b2c3"
    data = make_qr(sample)
    open("/tmp/qr-test.png", "wb").write(data)
    print("PNG байт:", len(data))
    print("decode:", decode_qr(data))
