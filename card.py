"""Рендер карточки профиля «Точка встречи».

Геометрия задана в единицах макета 300×400 и масштабируется в любое разрешение:

    sd  ->  900 × 1200
    hd  -> 1536 × 2048
    2k  -> 2304 × 3072
    4k  -> 3072 × 4096   (по умолчанию, меняется переменной CARD_SIZE)

Сглаживание делается локальными масками (уголок и круг рисуются с запасом и
уменьшаются), а не огромным холстом целиком, — поэтому 4K живёт в памяти дешёво
и работает даже на слабом хостинге.

Публичный API:
    render_card(profile, avatar=None, banner=None, size="4k") -> bytes (PNG)
    make_card(profile, avatar_path=None, output_path=None) -> bytes | путь
    font_report() -> диагностика шрифтов
"""

from __future__ import annotations

import io
import logging
import os
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LOG = logging.getLogger("tochka.card")

ROOT = Path(__file__).resolve().parent
FONT_DIR = ROOT / "fonts"
ASSET_DIR = ROOT / "assets"
LOGO_FILE = ASSET_DIR / "logo.png"
MEDIA_DIR = ROOT / "data" / "media"

# --------------------------------------------------------------------------- #
# Размеры вывода
# --------------------------------------------------------------------------- #
DESIGN_W = 300.0
DESIGN_H = 400.0

CARD_SIZES = {"sd": 3.0, "hd": 5.12, "2k": 7.68, "4k": 10.24}
DEFAULT_CARD_SIZE = (os.getenv("CARD_SIZE") or "4k").strip().lower()
if DEFAULT_CARD_SIZE not in CARD_SIZES:
    DEFAULT_CARD_SIZE = "4k"

# --------------------------------------------------------------------------- #
# Цвета
# --------------------------------------------------------------------------- #
THEMES = {
    "blue": (35, 84, 159),
    "sky": (32, 124, 202),
    "indigo": (68, 62, 168),
    "violet": (114, 62, 168),
    "berry": (176, 52, 118),
    "peach": (199, 104, 74),
    "sun": (196, 146, 38),
    "mint": (22, 122, 104),
    "forest": (38, 106, 62),
    "graphite": (72, 78, 88),
    "night": (32, 36, 46),
    "wine": (128, 42, 58),
}
THEME_ALIASES = {
    "white": "night", "dark": "night", "black": "night",
    "green": "mint", "orange": "peach", "red": "wine",
    "purple": "violet", "pink": "berry", "yellow": "sun",
    "gray": "graphite", "grey": "graphite", "cyan": "sky",
}
THEME_TITLES = {
    "blue": "Синяя",
    "sky": "Небесная",
    "indigo": "Индиго",
    "violet": "Фиолетовая",
    "berry": "Ягодная",
    "peach": "Персиковая",
    "sun": "Солнечная",
    "mint": "Мятная",
    "forest": "Лесная",
    "graphite": "Графит",
    "night": "Ночная",
    "wine": "Винная",
}
THEME_NAMES = THEME_TITLES  # совместимость со старым кодом

GENDER_TITLES = {"male": "Парень", "female": "Девушка", "other": "Не указан"}
GENDER_GLYPHS = {"male": "\u2642", "female": "\u2640"}

SURFACE = (255, 255, 255)
INK_TITLE = (26, 26, 26)
INK_BODY = (48, 48, 48)
INK_TAG = (128, 128, 128)
INK_HINT = (163, 163, 163)

# --------------------------------------------------------------------------- #
# Геометрия макета (300×400)
# --------------------------------------------------------------------------- #
LOGO_RADIUS = 21.0
LOGO_TOP_CENTER = (38.0, 31.0)
LOGO_BOTTOM_CENTER = (38.0, 41.0)
LOGO_TEXT = "Точка встречи"
LOGO_TEXT_LEFT = 10.7
LOGO_TEXT_BASELINE = 73.3

AVATAR_CENTER = (52.7, 222.3)
AVATAR_RADIUS = 49.6   # внешний радиус вместе с белым кольцом
AVATAR_RING = 5.2      # толщина кольца, рисуется внутрь

CARD_BOX = (3.0, 217.0, 297.0, 397.0)
CARD_RADIUS = 6.0

# Баннер: широкий прямоугольник внизу карточки.
PANEL_BOX = (6.0, 302.0, 294.0, 394.0)
PANEL_RADIUS = 9.0
BANNER_SIZE = (int(PANEL_BOX[2] - PANEL_BOX[0]), int(PANEL_BOX[3] - PANEL_BOX[1]))

TEXT_LEFT = 103.4
TEXT_RIGHT = 282.0
NAME_BASELINE = 232.5
TAG_GAP = 3.0
BIO_BASELINES = (245.8, 257.9, 270.0)
META_LEFT = 29.5
META_BASELINE = 278.8
META_GLYPH_GAP = 0.2
GLYPH_INK_HEIGHT = 15.0   # высота чернил ♂ / ♀ по макету
GLYPH_INK_BOTTOM = 279.0  # нижняя кромка знака по макету

# Кегли в единицах макета (умножаются на масштаб)
FONT_LOGO = 23.0 / 3.0
FONT_NAME = 52.0 / 3.0
FONT_TAG = 39.0 / 3.0
FONT_BIO = 33.0 / 3.0
FONT_META = 33.0 / 3.0
FONT_GLYPH = 40.0 / 3.0

BIO_LIMIT = 240
EMPTY_BIO_HINT = "Описание пока не заполнено — /edit"
NO_AGE_HINT = "Возраст не указан"

# --------------------------------------------------------------------------- #
# Шрифты
# --------------------------------------------------------------------------- #
# Сначала ищем настоящий Montserrat (если его положили в fonts/), потом
# метрически совпадающие с макетом Liberation Sans / Arial, затем всё остальное.
FONT_CANDIDATES = {
    False: (
        "Montserrat-Regular.ttf", "Montserrat-Medium.ttf", "Montserrat[wght].ttf",
        "Montserrat-VariableFont_wght.ttf", "LiberationSans-Regular.ttf",
        "Arial.ttf", "arial.ttf", "Helvetica.ttc", "DejaVuSans.ttf",
        "NotoSans-Regular.ttf", "NotoSans[wght].ttf", "FreeSans.ttf",
        "Roboto-Regular.ttf", "segoeui.ttf", "Verdana.ttf", "verdana.ttf",
    ),
    True: (
        "Montserrat-Bold.ttf", "Montserrat-SemiBold.ttf", "Montserrat[wght].ttf",
        "Montserrat-VariableFont_wght.ttf", "LiberationSans-Bold.ttf",
        "Arial_Bold.ttf", "Arial-Bold.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf",
        "NotoSans-Bold.ttf", "FreeSansBold.ttf", "Roboto-Bold.ttf",
        "segoeuib.ttf", "Verdana_Bold.ttf", "verdanab.ttf",
    ),
}

SYSTEM_FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/share/fonts/truetype",
    "/usr/local/share/fonts",
    "/usr/share/fonts/liberation-sans",
    "/usr/share/fonts/liberation",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu-sans-fonts",
    "/usr/share/fonts/msttcore",
    "/usr/share/fonts/truetype/msttcorefonts",
    "~/.fonts",
    "~/.local/share/fonts",
    "/Library/Fonts",
    "/System/Library/Fonts",
    "C:/Windows/Fonts",
)

# Символ из области для частного использования: его нет ни в одном нормальном
# шрифте, так что он даёт эталон «квадратика» — так ловим недостающие глифы.
TOFU_PROBE = "\ue05f"
CYRILLIC_PROBE = "Фяй"

_font_cache: dict[tuple, ImageFont.FreeTypeFont] = {}
_source_cache: dict[tuple, tuple] = {}
_glyph_support: dict[tuple, bool] = {}


def _candidate_paths(bold: bool, font_dir=None):
    dirs = []
    if font_dir:
        dirs.extend([Path(font_dir), Path(font_dir) / "fonts",
                     Path(font_dir) / "static", Path(font_dir) / "fonts" / "static"])
    dirs.extend([FONT_DIR, FONT_DIR / "static", ROOT,
                 Path.cwd() / "fonts", Path.cwd() / "fonts" / "static", Path.cwd()])
    for raw in SYSTEM_FONT_DIRS:
        dirs.append(Path(os.path.expanduser(raw)))

    seen = set()
    for directory in dirs:
        try:
            if not directory.is_dir():
                continue
        except OSError:
            continue
        for name in FONT_CANDIDATES[bold]:
            path = directory / name
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if path.is_file():
                yield path

    # Montserrat из Google Fonts приезжает под разными именами
    # (Montserrat-VariableFont_wght.ttf, static/Montserrat-Regular.ttf и т.д.),
    # поэтому дополнительно ищем любой файл с Montserrat в имени.
    wanted = ("bold", "semibold", "variablefont", "wght") if bold \
        else ("regular", "medium", "variablefont", "wght")
    for directory in (FONT_DIR, FONT_DIR / "static",
                      Path.cwd() / "fonts", Path.cwd() / "fonts" / "static"):
        try:
            if not directory.is_dir():
                continue
            found = sorted(directory.glob("*ontserrat*"))
        except OSError:
            continue
        for path in found:
            if path.suffix.lower() not in (".ttf", ".otf"):
                continue
            if str(path) in seen or not path.is_file():
                continue
            lowered = path.name.lower()
            if not any(mark in lowered for mark in wanted):
                continue
            seen.add(str(path))
            yield path

    # Последняя попытка: поиск по дереву системных шрифтов.
    for raw in ("/usr/share/fonts", "/usr/local/share/fonts"):
        root = Path(raw)
        if not root.is_dir():
            continue
        for name in FONT_CANDIDATES[bold]:
            try:
                for path in root.glob("*/%s" % name):
                    if str(path) not in seen:
                        seen.add(str(path))
                        yield path
            except OSError:
                continue


def _probe(font: ImageFont.FreeTypeFont, text: str) -> bytes:
    image = Image.new("L", (96, 72), 0)
    ImageDraw.Draw(image).text((4, 4), text, font=font, fill=255)
    return image.tobytes()


def _supports(font: ImageFont.FreeTypeFont, text: str) -> bool:
    """Есть ли в шрифте реальные глифы (а не квадратики) для всего текста."""
    key = (getattr(font, "path", ""), getattr(font, "size", 0), text)
    cached = _glyph_support.get(key)
    if cached is not None:
        return cached
    blank = _probe(font, "")
    tofu = _probe(font, TOFU_PROBE)
    for char in text:
        shot = _probe(font, char)
        if shot == blank or shot == tofu:
            _glyph_support[key] = False
            return False
    _glyph_support[key] = True
    return True


def _embedded_source():
    try:
        import fontdata
    except Exception:  # noqa: BLE001
        return None
    return fontdata


def _resolve_source(bold: bool, font_dir=None):
    """Подбирает шрифт, в котором точно есть кириллица."""
    key = (bold, str(font_dir))
    cached = _source_cache.get(key)
    if cached is not None:
        return cached

    fallbacks = []
    for path in _candidate_paths(bold, font_dir):
        try:
            probe_font = ImageFont.truetype(str(path), 42)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("шрифт %s не открылся: %s", path, exc)
            continue
        if _supports(probe_font, CYRILLIC_PROBE):
            result = (str(path), "file")
            _source_cache[key] = result
            return result
        fallbacks.append(path)

    embedded = _embedded_source()
    if embedded is not None:
        try:
            probe_font = ImageFont.truetype(embedded.font_stream(bold), 42)
            if _supports(probe_font, CYRILLIC_PROBE):
                LOG.warning(
                    "Шрифты из fonts/ не найдены или без кириллицы — беру встроенный резервный шрифт."
                )
                result = ("embedded", "embedded")
                _source_cache[key] = result
                return result
        except Exception as exc:  # noqa: BLE001
            LOG.debug("встроенный шрифт не открылся: %s", exc)

    if fallbacks:
        LOG.warning("Ни один шрифт не поддерживает кириллицу, использую %s", fallbacks[0])
        result = (str(fallbacks[0]), "file")
    else:
        LOG.error("Шрифты вообще не найдены — текст будет рисоваться встроенным шрифтом Pillow.")
        result = (None, "default")
    _source_cache[key] = result
    return result


def _font(size: float, bold: bool = False, font_dir=None) -> ImageFont.FreeTypeFont:
    pixels = max(1, int(round(size)))
    key = (pixels, bold, str(font_dir))
    font = _font_cache.get(key)
    if font is not None:
        return font

    source, kind = _resolve_source(bold, font_dir)
    try:
        if kind == "embedded":
            font = ImageFont.truetype(_embedded_source().font_stream(bold), pixels)
        elif kind == "file":
            font = ImageFont.truetype(source, pixels)
        else:
            raise OSError("no font files")
    except Exception as exc:  # noqa: BLE001
        LOG.error("Не удалось открыть шрифт (%s): %s", source, exc)
        try:
            font = ImageFont.load_default(pixels)
        except TypeError:
            font = ImageFont.load_default()

    if bold and getattr(font, "set_variation_by_name", None):
        try:  # вариативный Montserrat[wght].ttf — включаем Bold
            font.set_variation_by_name("Bold")
        except Exception:  # noqa: BLE001
            pass

    _font_cache[key] = font
    return font


def font_report(font_dir=None) -> dict:
    """Чем реально рисуется текст — для логов и check_fonts.py."""
    report = {}
    for label, bold in (("regular", False), ("bold", True)):
        source, kind = _resolve_source(bold, font_dir)
        font = _font(40, bold, font_dir)
        try:
            family, style = font.getname()
        except Exception:  # noqa: BLE001
            family, style = ("?", "?")
        report[label] = {
            "source": source or "Pillow default",
            "kind": kind,
            "family": family,
            "style": style,
            "cyrillic": _supports(font, CYRILLIC_PROBE),
            "gender_glyphs": _supports(font, "".join(GENDER_GLYPHS.values())),
        }
    return report


# --------------------------------------------------------------------------- #
# Мелкие хелперы
# --------------------------------------------------------------------------- #
def resolve_theme(name) -> str:
    key = str(name or "blue").strip().lower()
    key = THEME_ALIASES.get(key, key)
    return key if key in THEMES else "blue"


def years_label(age: int) -> str:
    age = abs(int(age))
    if 11 <= age % 100 <= 14:
        return "лет"
    last = age % 10
    if last == 1:
        return "год"
    if last in (2, 3, 4):
        return "года"
    return "лет"


def _field(profile, key, default=None):
    if profile is None:
        return default
    if isinstance(profile, dict):
        return profile.get(key, default)
    try:  # sqlite3.Row
        value = profile[key]
        return default if value is None else value
    except (KeyError, IndexError, TypeError):
        pass
    return getattr(profile, key, default)


def _resolve_scale(size=None, scale=None) -> float:
    if scale:
        value = float(scale)
    elif isinstance(size, (int, float)) and not isinstance(size, bool):
        value = float(size)
    elif size:
        value = CARD_SIZES.get(str(size).strip().lower(), CARD_SIZES[DEFAULT_CARD_SIZE])
    else:
        value = CARD_SIZES[DEFAULT_CARD_SIZE]
    return max(1.0, min(16.0, value))


def _supersample(width: int, height: int) -> int:
    area = max(1, width * height)
    if area <= 400_000:
        return 4
    if area <= 4_000_000:
        return 3
    return 2


def _round_rect_mask(width: int, height: int, radius: int) -> Image.Image:
    """Маска скруглённого прямоугольника: углы сглаживаются точечно."""
    width = max(1, width)
    height = max(1, height)
    radius = max(0, min(radius, width // 2, height // 2))
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    if radius == 0:
        draw.rectangle((0, 0, width - 1, height - 1), fill=255)
        return mask
    draw.rectangle((radius, 0, width - 1 - radius, height - 1), fill=255)
    draw.rectangle((0, radius, width - 1, height - 1 - radius), fill=255)

    ss = 4
    corner = Image.new("L", (radius * ss, radius * ss), 0)
    ImageDraw.Draw(corner).pieslice(
        (0, 0, radius * 2 * ss - 1, radius * 2 * ss - 1), 180, 270, fill=255
    )
    corner = corner.resize((radius, radius), Image.BOX)
    mask.paste(corner, (0, 0))
    mask.paste(corner.transpose(Image.FLIP_LEFT_RIGHT), (width - radius, 0))
    mask.paste(corner.transpose(Image.FLIP_TOP_BOTTOM), (0, height - radius))
    mask.paste(corner.transpose(Image.ROTATE_180), (width - radius, height - radius))
    return mask


def _circle_mask(diameter: int) -> Image.Image:
    diameter = max(1, diameter)
    ss = _supersample(diameter, diameter)
    big = Image.new("L", (diameter * ss, diameter * ss), 0)
    ImageDraw.Draw(big).ellipse((0, 0, diameter * ss - 1, diameter * ss - 1), fill=255)
    return big.resize((diameter, diameter), Image.BOX)


def _open_image(source):
    """Картинка из bytes / пути / файловог�� объекта / PIL.Image."""
    if source is None:
        return None
    try:
        if isinstance(source, Image.Image):
            return source.convert("RGB")
        if isinstance(source, (bytes, bytearray, memoryview)):
            return Image.open(io.BytesIO(bytes(source))).convert("RGB")
        if hasattr(source, "read"):
            return Image.open(io.BytesIO(source.read())).convert("RGB")
        path = Path(str(source))
        if path.is_file():
            return Image.open(path).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Не удалось открыть картинку: %s", exc)
    return None


def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """Заполняет область без искажений (как background-size: cover)."""
    width = max(1, width)
    height = max(1, height)
    ratio = max(width / image.width, height / image.height)
    new_size = (max(width, int(round(image.width * ratio))),
                max(height, int(round(image.height * ratio))))
    resized = image.resize(new_size, Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _text_width(font, text: str) -> float:
    if not text:
        return 0.0
    try:
        return float(font.getlength(text))
    except Exception:  # noqa: BLE001
        box = font.getbbox(text)
        return float(box[2] - box[0])


def _fit(text: str, base_size: float, limit: float, bold=False, font_dir=None):
    """Уменьшает кегль, пока текст не влезет в ширину."""
    size = base_size
    font = _font(size, bold, font_dir)
    while size > base_size * 0.45 and _text_width(font, text) > limit:
        size -= max(0.5, base_size * 0.03)
        font = _font(size, bold, font_dir)
    return font


def _wrap(font, text: str, limit: float, max_lines: int):
    words = str(text).split()
    lines, current = [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        if _text_width(font, candidate) <= limit or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)

    if len(lines) == max_lines:
        used = len(" ".join(lines).split())
        if used < len(words):
            tail = lines[-1]
            while tail and _text_width(font, tail + "…") > limit:
                tail = tail[:-1]
            lines[-1] = (tail.rstrip() + "…") if tail else "…"
    return lines or [""]


# --------------------------------------------------------------------------- #
# Знаки пола
# --------------------------------------------------------------------------- #
def _vector_glyph_mask(glyph: str, height: float) -> Image.Image:
    """Векторный ♂ / ♀ на случай, если в шрифте нет таких символов."""
    unit = max(24.0, height * 2.4)
    radius = unit * 0.30
    stroke = max(1, int(round(unit * 0.11)))
    box = int(round(unit * 1.8))
    layer = Image.new("L", (box, box), 0)
    draw = ImageDraw.Draw(layer)

    if glyph == GENDER_GLYPHS["female"]:
        cx = box / 2.0
        cy = box / 2.0 - unit * 0.14
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     outline=255, width=stroke)
        draw.line((cx, cy + radius, cx, cy + radius + unit * 0.36), fill=255, width=stroke)
        bar = radius * 0.70
        bar_y = cy + radius + unit * 0.20
        draw.line((cx - bar, bar_y, cx + bar, bar_y), fill=255, width=stroke)
    else:
        cx = box / 2.0 - unit * 0.10
        cy = box / 2.0 + unit * 0.14
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     outline=255, width=stroke)
        start_x = cx + radius * 0.71
        start_y = cy - radius * 0.71
        end_x = start_x + unit * 0.32
        end_y = start_y - unit * 0.32
        draw.line((start_x, start_y, end_x, end_y), fill=255, width=stroke)
        head = unit * 0.18
        draw.line((end_x, end_y, end_x - head, end_y), fill=255, width=stroke)
        draw.line((end_x, end_y, end_x, end_y + head), fill=255, width=stroke)

    bbox = layer.getbbox()
    return layer.crop(bbox) if bbox else layer


def _glyph_mask(glyph: str, height: float, font_dir=None) -> Image.Image:
    """Маска знака пола, обрезанная ровно по чернилам."""
    render_size = max(12.0, height * 2.4)
    font = _font(render_size, False, font_dir)
    if _supports(font, glyph):
        box = int(round(render_size * 2.4))
        layer = Image.new("L", (box, box), 0)
        ImageDraw.Draw(layer).text((box * 0.3, box * 0.7), glyph, font=font, fill=255,
                                  anchor="ls")
        bbox = layer.getbbox()
        if bbox:
            return layer.crop(bbox)
    return _vector_glyph_mask(glyph, height)


def _draw_gender(canvas, glyph: str, ink_left: float, ink_bottom: float, scale: float,
                 color, font_dir=None) -> None:
    """Знак пола всегда занимает ровно такой же размер, как в макете."""
    target = max(2.0, GLYPH_INK_HEIGHT * scale)
    mask = _glyph_mask(glyph, target, font_dir)
    if mask is None or mask.width < 1 or mask.height < 1:
        return
    width = max(1, int(round(mask.width * target / mask.height)))
    height = max(1, int(round(target)))
    mask = mask.resize((width, height), Image.LANCZOS)
    canvas.paste(color, (int(round(ink_left)), int(round(ink_bottom - height))), mask)


# --------------------------------------------------------------------------- #
# Логотип
# --------------------------------------------------------------------------- #
def _draw_logo(canvas, scale: float, logo_file=None) -> None:
    left = (LOGO_TOP_CENTER[0] - LOGO_RADIUS) * scale
    top = (LOGO_TOP_CENTER[1] - LOGO_RADIUS) * scale
    width = int(round(LOGO_RADIUS * 2 * scale))
    height = int(round((LOGO_BOTTOM_CENTER[1] + LOGO_RADIUS - LOGO_TOP_CENTER[1] + LOGO_RADIUS) * scale))

    custom = _open_image(logo_file) if logo_file else None
    if custom is None and LOGO_FILE.is_file():
        try:
            custom = Image.open(LOGO_FILE).convert("RGBA")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Логотип %s не открылся: %s", LOGO_FILE, exc)
            custom = None
    elif custom is not None:
        custom = custom.convert("RGBA")

    if custom is not None:
        ratio = min(width / custom.width, height / custom.height)
        new_size = (max(1, int(round(custom.width * ratio))),
                    max(1, int(round(custom.height * ratio))))
        logo = custom.resize(new_size, Image.LANCZOS)
        offset = (int(round(left + (width - logo.width) / 2)),
                  int(round(top + (height - logo.height) / 2)))
        canvas.paste(logo, offset, logo.split()[-1])
        return

    # Векторный знак: две половинки круга со сдвигом по вертикали.
    ss = _supersample(width, height)
    layer = Image.new("L", (width * ss, height * ss), 0)
    draw = ImageDraw.Draw(layer)
    radius = LOGO_RADIUS * scale * ss
    for center, start, end in (
        (LOGO_TOP_CENTER, 90, 270),
        (LOGO_BOTTOM_CENTER, -90, 90),
    ):
        cx = (center[0] * scale - left) * ss
        cy = (center[1] * scale - top) * ss
        draw.pieslice((cx - radius, cy - radius, cx + radius, cy + radius), start, end, fill=255)
    mask = layer.resize((width, height), Image.BOX)
    canvas.paste(SURFACE, (int(round(left)), int(round(top))), mask)


# --------------------------------------------------------------------------- #
# Главный рендер
# --------------------------------------------------------------------------- #
def render_card(profile, avatar=None, banner=None, *, size=None, scale=None,
                font_dir=None, logo=None, fmt="PNG", quality=92) -> bytes:
    """Собирает карточку и возвращает готовые байты картинки."""
    factor = _resolve_scale(size, scale)
    width = int(round(DESIGN_W * factor))
    height = int(round(DESIGN_H * factor))

    theme = resolve_theme(_field(profile, "theme", "blue"))
    background = THEMES[theme]

    canvas = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(canvas)

    # 1. логотип и подпись
    _draw_logo(canvas, factor, logo)
    draw.text(
        (LOGO_TEXT_LEFT * factor, LOGO_TEXT_BASELINE * factor),
        LOGO_TEXT,
        font=_font(FONT_LOGO * factor, True, font_dir),
        fill=SURFACE,
        anchor="ls",
    )

    # 2. белая плашка
    card_left = int(round(CARD_BOX[0] * factor))
    card_top = int(round(CARD_BOX[1] * factor))
    card_w = int(round(CARD_BOX[2] * factor)) - card_left
    card_h = int(round(CARD_BOX[3] * factor)) - card_top
    canvas.paste(SURFACE, (card_left, card_top),
                 _round_rect_mask(card_w, card_h, int(round(CARD_RADIUS * factor))))

    # 3. баннер (нижний блок 288×92)
    panel_left = int(round(PANEL_BOX[0] * factor))
    panel_top = int(round(PANEL_BOX[1] * factor))
    panel_w = int(round(PANEL_BOX[2] * factor)) - panel_left
    panel_h = int(round(PANEL_BOX[3] * factor)) - panel_top
    panel_mask = _round_rect_mask(panel_w, panel_h, int(round(PANEL_RADIUS * factor)))
    banner_image = _open_image(banner)
    if banner_image is not None:
        canvas.paste(_cover(banner_image, panel_w, panel_h), (panel_left, panel_top), panel_mask)
    else:
        canvas.paste(background, (panel_left, panel_top), panel_mask)

    # 4. аватарка в белом кольце
    outer = int(round(AVATAR_RADIUS * 2 * factor))
    outer_left = int(round((AVATAR_CENTER[0] - AVATAR_RADIUS) * factor))
    outer_top = int(round((AVATAR_CENTER[1] - AVATAR_RADIUS) * factor))
    canvas.paste(SURFACE, (outer_left, outer_top), _circle_mask(outer))

    photo_radius = max(1.0, AVATAR_RADIUS - AVATAR_RING)
    inner = int(round(photo_radius * 2 * factor))
    inner_left = int(round((AVATAR_CENTER[0] - photo_radius) * factor))
    inner_top = int(round((AVATAR_CENTER[1] - photo_radius) * factor))
    inner_mask = _circle_mask(inner)
    photo = _open_image(avatar)
    if photo is not None:
        canvas.paste(_cover(photo, inner, inner), (inner_left, inner_top), inner_mask)
    else:
        canvas.paste(background, (inner_left, inner_top), inner_mask)

    # 5. имя и тег
    text_limit = (TEXT_RIGHT - TEXT_LEFT) * factor
    name = str(_field(profile, "display_name", "") or _field(profile, "username", "") or "Без имени")
    tag_value = _field(profile, "tag", None)
    tag = ("#%s" % str(tag_value).lstrip("#")) if tag_value else ""

    tag_font = _font(FONT_TAG * factor, False, font_dir)
    tag_width = _text_width(tag_font, tag) + (TAG_GAP * factor if tag else 0)
    name_font = _fit(name, FONT_NAME * factor, max(text_limit * 0.4, text_limit - tag_width),
                     False, font_dir)

    name_x = TEXT_LEFT * factor
    name_baseline = NAME_BASELINE * factor
    draw.text((name_x, name_baseline), name, font=name_font, fill=INK_TITLE, anchor="ls")
    if tag:
        draw.text(
            (name_x + _text_width(name_font, name) + TAG_GAP * factor, name_baseline),
            tag, font=tag_font, fill=INK_TAG, anchor="ls",
        )

    # 6. описание
    bio_font = _font(FONT_BIO * factor, False, font_dir)
    bio = _sanitize_text(_field(profile, "bio", ""), bio_font)
    if bio:
        lines = _wrap(bio_font, bio[:BIO_LIMIT], text_limit, len(BIO_BASELINES))
        bio_color = INK_BODY
    else:
        lines = _wrap(bio_font, EMPTY_BIO_HINT, text_limit, 2)
        bio_color = INK_HINT
    for line, baseline in zip(lines, BIO_BASELINES):
        draw.text((name_x, baseline * factor), line, font=bio_font, fill=bio_color, anchor="ls")

    # 7. возраст и знак пола: только цифра и значок, всегда по центру.
    # Ничего не указано - строки нет совсем.
    age_raw = _field(profile, "age", None)
    try:
        age = int(age_raw) if age_raw not in (None, "") else None
    except (TypeError, ValueError):
        age = None
    if age is not None and not (5 <= age <= 100):
        age = None
    glyph = GENDER_GLYPHS.get(str(_field(profile, "gender", "") or "").lower())

    if age or glyph:
        meta_baseline = META_BASELINE * factor
        meta_font = _font(FONT_META * factor, False, font_dir)
        label = str(age) if age else ""
        text_width = _text_width(meta_font, label) if label else 0.0
        glyph_height = max(2.0, GLYPH_INK_HEIGHT * factor)
        glyph_width = _gender_ink_width(glyph, glyph_height, font_dir) if glyph else 0.0
        gap = (META_GLYPH_GAP + 2.2) * factor if (label and glyph) else 0.0
        total = text_width + gap + glyph_width
        start = AVATAR_CENTER[0] * factor - total / 2.0
        if label:
            draw.text((start, meta_baseline), label,
                      font=meta_font, fill=INK_TITLE, anchor="ls")
        if glyph:
            _draw_gender(
                canvas, glyph, start + text_width + gap,
                GLYPH_INK_BOTTOM * factor, factor, background, font_dir,
            )

    buffer = io.BytesIO()
    if str(fmt).upper() in ("JPG", "JPEG"):
        canvas.save(buffer, "JPEG", quality=int(quality), optimize=True, progressive=True)
    else:
        canvas.save(buffer, "PNG", compress_level=6)
    return buffer.getvalue()


def make_card(profile, avatar_path=None, output_path=None, font_dir=None,
              banner_path=None, size=None):
    """Совместимая обёртка: пишет файл, если задан output_path."""
    payload = render_card(profile, avatar_path, banner_path, size=size, font_dir=font_dir)
    if not output_path:
        return payload
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return str(path)


if __name__ == "__main__":
    demo = {
        "display_name": "Crackford",
        "tag": "00000",
        "age": 17,
        "gender": "male",
        "bio": "меня зовут ибрагим, я люблю еду всякую есть, а также булочки",
        "theme": "blue",
    }
    for label, name in (("sd", "preview.png"), (DEFAULT_CARD_SIZE, "preview_4k.png")):
        data = render_card(demo, size=label)
        Path(name).write_bytes(data)
        print("%s -> %s (%d КБ)" % (label, name, len(data) // 1024))
    for role, info in font_report().items():
        print("%-8s %s [%s] кириллица=%s знаки пола=%s" % (
            role, info["family"], info["source"], info["cyrillic"], info["gender_glyphs"]))


# --------------------------------------------------------------------------- #
# Мелкие хелперы
# --------------------------------------------------------------------------- #
def resolve_theme(name):
    key = str(name or "blue").strip().lower()
    key = THEME_ALIASES.get(key, key)
    return key if key in THEMES else "blue"


def years_label(age):
    age = abs(int(age))
    if 11 <= age % 100 <= 14:
        return "лет"
    last = age % 10
    if last == 1:
        return "год"
    if last in (2, 3, 4):
        return "года"
    return "лет"


def _field(profile, key, default=None):
    if profile is None:
        return default
    if isinstance(profile, dict):
        value = profile.get(key, default)
        return default if value is None else value
    try:  # sqlite3.Row
        value = profile[key]
        return default if value is None else value
    except (KeyError, IndexError, TypeError):
        pass
    value = getattr(profile, key, default)
    return default if value is None else value


def _resolve_scale(size=None, scale=None):
    if scale:
        value = float(scale)
    elif isinstance(size, (int, float)) and not isinstance(size, bool):
        value = float(size)
    elif size:
        value = CARD_SIZES.get(str(size).strip().lower(), CARD_SIZES[DEFAULT_CARD_SIZE])
    else:
        value = CARD_SIZES[DEFAULT_CARD_SIZE]
    return max(1.0, min(16.0, value))


def _supersample(width, height):
    """Сглаживание через перерасчёт. Чем больше картинка, тем меньше запас:
    на больших размерах лишний слой съедает десятки мегабайт оперативной памяти,
    а разницы на глаз уже не видно."""
    area = max(1, width * height)
    if area <= 250000:
        return 4
    if area <= 1500000:
        return 3
    if area <= 6000000:
        return 2
    return 1


def _round_rect_mask(width, height, radius):
    """Маска скругленного прямоугольника: углы сглаживаются точечно."""
    width = max(1, int(width))
    height = max(1, int(height))
    radius = max(0, min(int(radius), width // 2, height // 2))
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    if radius == 0:
        draw.rectangle((0, 0, width - 1, height - 1), fill=255)
        return mask
    draw.rectangle((radius, 0, width - 1 - radius, height - 1), fill=255)
    draw.rectangle((0, radius, width - 1, height - 1 - radius), fill=255)

    ss = 4
    corner = Image.new("L", (radius * ss, radius * ss), 0)
    ImageDraw.Draw(corner).pieslice(
        (0, 0, radius * 2 * ss - 1, radius * 2 * ss - 1), 180, 270, fill=255
    )
    corner = corner.resize((radius, radius), Image.BOX)
    mask.paste(corner, (0, 0))
    mask.paste(corner.transpose(Image.FLIP_LEFT_RIGHT), (width - radius, 0))
    mask.paste(corner.transpose(Image.FLIP_TOP_BOTTOM), (0, height - radius))
    mask.paste(corner.transpose(Image.ROTATE_180), (width - radius, height - radius))
    return mask


def _circle_mask(diameter):
    diameter = max(1, int(diameter))
    ss = _supersample(diameter, diameter)
    big = Image.new("L", (diameter * ss, diameter * ss), 0)
    ImageDraw.Draw(big).ellipse((0, 0, diameter * ss - 1, diameter * ss - 1), fill=255)
    return big.resize((diameter, diameter), Image.BOX)


def _open_image(source):
    """Картинка из bytes / пути / файлового объекта / PIL.Image."""
    if source is None:
        return None
    try:
        if isinstance(source, Image.Image):
            return source.convert("RGB")
        if isinstance(source, (bytes, bytearray, memoryview)):
            return Image.open(io.BytesIO(bytes(source))).convert("RGB")
        if hasattr(source, "read"):
            return Image.open(io.BytesIO(source.read())).convert("RGB")
        path = Path(str(source))
        if path.is_file():
            return Image.open(path).convert("RGB")
    except Exception as exc:
        LOG.warning("Не удалось открыть картинку: %s", exc)
    return None


def _cover(image, width, height):
    """Заполняет область без искажений (как background-size: cover)."""
    width = max(1, int(width))
    height = max(1, int(height))
    # Грубое уменьшение целым делителем перед LANCZOS: фото с телефона
    # на 4000x3000 иначе гоняется целиком и ест память впустую.
    step = min(image.width // max(1, width), image.height // max(1, height))
    if step >= 2:
        try:
            reduced = image.reduce(min(step, 8))
            if image is not reduced:
                image = reduced
        except Exception:
            pass
    ratio = max(width / image.width, height / image.height)
    new_size = (max(width, int(round(image.width * ratio))),
                max(height, int(round(image.height * ratio))))
    resized = image.resize(new_size, Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _text_width(font, text):
    if not text:
        return 0.0
    try:
        return float(font.getlength(text))
    except Exception:
        box = font.getbbox(text)
        return float(box[2] - box[0])


def fonts_dir_report():
    """Список файлов в fonts/ - чтобы сразу видеть в логе, что залито."""
    lines = []
    for directory in (FONT_DIR, FONT_DIR / "static"):
        try:
            if not directory.is_dir():
                lines.append("%s - папки нет" % directory)
                continue
            names = sorted(item.name for item in directory.iterdir() if item.is_file())
        except OSError as exc:
            lines.append("%s - ошибка чтения (%s)" % (directory, exc))
            continue
        lines.append("%s - %s" % (directory, ", ".join(names) if names else "пусто"))
    return lines


_PLAIN_CACHE = {}
_DIGIT_WORDS = {
    "ZERO": "0", "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4",
    "FIVE": "5", "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9",
}


def _plain_char(ch):
    """Стилизованная Unicode-буква -> обычная.

    В никах Telegram часто встречаются декоративные алфавиты (double-struck,
    fraktur, monospace, кружочки, fullwidth). В обычных шрифтах таких глифов нет,
    и вместо них рисуются квадратики. Здесь приводим их к латинице или цифре.
    """
    if ch in _PLAIN_CACHE:
        return _PLAIN_CACHE[ch]
    plain = unicodedata.normalize("NFKC", ch)
    if plain == ch:
        stripped = "".join(
            part for part in unicodedata.normalize("NFKD", ch)
            if not unicodedata.combining(part)
        )
        if stripped:
            plain = stripped
    if plain == ch:
        title = unicodedata.name(ch, "")
        for marker in (" SMALL LETTER ", " CAPITAL LETTER ", " LETTER ", " DIGIT "):
            if marker not in title:
                continue
            tail = title.split(marker)[-1].strip()
            if marker == " DIGIT ":
                plain = _DIGIT_WORDS.get(tail, ch)
            elif len(tail) == 1:
                plain = tail.lower() if "SMALL" in marker else tail
            break
    _PLAIN_CACHE[ch] = plain
    return plain


def _sanitize_text(value, font=None, fallback=""):
    """Готовит пользовательский текст к рисованию без квадратиков."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    result = []
    for ch in text:
        if ch in "\n\r\t":
            result.append(" ")
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        if font is None:
            result.append(ch)
            continue
        if _supports(font, ch):
            result.append(ch)
            continue
        for piece in _plain_char(ch):
            if _supports(font, piece):
                result.append(piece)
    cleaned = " ".join("".join(result).split())
    return cleaned or fallback


def _fit(text, base_size, limit, bold=False, font_dir=None):
    """Уменьшает кегль, пока текст не влезет в ширину."""
    size = base_size
    font = _font(size, bold, font_dir)
    while size > base_size * 0.45 and _text_width(font, text) > limit:
        size -= max(0.5, base_size * 0.03)
        font = _font(size, bold, font_dir)
    return font


def _wrap(font, text, limit, max_lines):
    words = str(text).split()
    lines, current = [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        if _text_width(font, candidate) <= limit or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)

    if len(lines) == max_lines:
        used = len(" ".join(lines).split())
        if used < len(words):
            tail = lines[-1]
            ellipsis = chr(0x2026)
            while tail and _text_width(font, tail + ellipsis) > limit:
                tail = tail[:-1]
            lines[-1] = (tail.rstrip() + ellipsis) if tail else ellipsis
    return lines or [""]


# --------------------------------------------------------------------------- #
# Знаки пола
# --------------------------------------------------------------------------- #
def _vector_glyph_mask(glyph, height):
    """Векторный знак пола на случай, если в шрифте его нет."""
    unit = max(24.0, height * 2.4)
    radius = unit * 0.30
    stroke = max(1, int(round(unit * 0.11)))
    box = int(round(unit * 1.8))
    layer = Image.new("L", (box, box), 0)
    draw = ImageDraw.Draw(layer)

    if glyph == GENDER_GLYPHS["female"]:
        cx = box / 2.0
        cy = box / 2.0 - unit * 0.14
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     outline=255, width=stroke)
        draw.line((cx, cy + radius, cx, cy + radius + unit * 0.36), fill=255, width=stroke)
        bar = radius * 0.70
        bar_y = cy + radius + unit * 0.20
        draw.line((cx - bar, bar_y, cx + bar, bar_y), fill=255, width=stroke)
    else:
        cx = box / 2.0 - unit * 0.10
        cy = box / 2.0 + unit * 0.14
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     outline=255, width=stroke)
        start_x = cx + radius * 0.71
        start_y = cy - radius * 0.71
        end_x = start_x + unit * 0.32
        end_y = start_y - unit * 0.32
        draw.line((start_x, start_y, end_x, end_y), fill=255, width=stroke)
        head = unit * 0.18
        draw.line((end_x, end_y, end_x - head, end_y), fill=255, width=stroke)
        draw.line((end_x, end_y, end_x, end_y + head), fill=255, width=stroke)

    bbox = layer.getbbox()
    return layer.crop(bbox) if bbox else layer


# Готовые значки пола из assets/. Если файл есть - берём его форму,
# цвет всё равно ставим как в макете (значок используется как маска).
GENDER_ICON_FILES = {
    chr(0x2642): ("gender-male.png", "gender_male.png", "male.png"),
    chr(0x2640): ("gender-female.png", "gender_female.png", "female.png"),
}
_icon_mask_cache = {}


def _icon_mask(glyph):
    """Силуэт значка из assets/ в полном разрешении (кэшируется один раз)."""
    if glyph in _icon_mask_cache:
        return _icon_mask_cache[glyph]
    result = None
    for name in GENDER_ICON_FILES.get(glyph, ()):
        path = ASSET_DIR / name
        if not path.is_file():
            continue
        try:
            with Image.open(path) as source:
                image = source.convert("RGBA")
            # чтобы не держать в памяти картинку 1254x1254, сразу сжимаем
            if max(image.size) > 256:
                ratio = 256.0 / max(image.size)
                image = image.resize((max(1, int(image.width * ratio)),
                                      max(1, int(image.height * ratio))), Image.LANCZOS)
            alpha = image.split()[-1]
            if alpha.getextrema()[0] > 250:
                # фон не прозрачный (белый) - делаем маску из яркости
                gray = image.convert("L")
                alpha = gray.point(lambda value: 255 if value < 235 else 0)
            bbox = alpha.getbbox()
            result = alpha.crop(bbox) if bbox else None
            image.close()
            if result is not None:
                LOG.info("Значок пола: %s", path)
                break
        except Exception as exc:
            LOG.warning("Значок %s не открылся: %s", path, exc)
    _icon_mask_cache[glyph] = result
    return result


def _glyph_mask(glyph, height, font_dir=None):
    """Маска знака пола, обрезанная ровно по чернилам."""
    icon = _icon_mask(glyph)
    if icon is not None:
        return icon
    render_size = max(12.0, height * 2.4)
    font = _font(render_size, False, font_dir)
    if _supports(font, glyph):
        box = int(round(render_size * 2.4))
        layer = Image.new("L", (box, box), 0)
        ImageDraw.Draw(layer).text((box * 0.3, box * 0.7), glyph, font=font,
                                   fill=255, anchor="ls")
        bbox = layer.getbbox()
        if bbox:
            return layer.crop(bbox)
    return _vector_glyph_mask(glyph, height)


def _gender_ink_width(glyph, target, font_dir=None):
    """Ширина значка пола при заданной высоте (нужна для центровки строки)."""
    mask = _glyph_mask(glyph, target, font_dir)
    if mask is None or mask.height < 1:
        return 0.0
    return float(mask.width) * float(target) / float(mask.height)


def _draw_gender(canvas, glyph, ink_left, ink_bottom, scale, color, font_dir=None):
    """Знак пола всегда занимает такой же размер, как в макете."""
    target = max(2.0, GLYPH_INK_HEIGHT * scale)
    mask = _glyph_mask(glyph, target, font_dir)
    if mask is None or mask.width < 1 or mask.height < 1:
        return
    width = max(1, int(round(mask.width * target / mask.height)))
    height = max(1, int(round(target)))
    mask = mask.resize((width, height), Image.LANCZOS)
    canvas.paste(color, (int(round(ink_left)), int(round(ink_bottom - height))), mask)


# --------------------------------------------------------------------------- #
# Логотип
# --------------------------------------------------------------------------- #
def _draw_logo(canvas, scale, logo_file=None):
    left = (LOGO_TOP_CENTER[0] - LOGO_RADIUS) * scale
    top = (LOGO_TOP_CENTER[1] - LOGO_RADIUS) * scale
    width = int(round(LOGO_RADIUS * 2 * scale))
    height = int(round((LOGO_BOTTOM_CENTER[1] - LOGO_TOP_CENTER[1] + LOGO_RADIUS * 2) * scale))

    custom = None
    if logo_file is not None:
        custom = _open_image(logo_file)
    if custom is None and LOGO_FILE.is_file():
        try:
            custom = Image.open(LOGO_FILE)
        except Exception as exc:
            LOG.warning("Логотип %s не открылся: %s", LOGO_FILE, exc)
            custom = None

    if custom is not None:
        custom = custom.convert("RGBA")
        ratio = min(width / custom.width, height / custom.height)
        new_size = (max(1, int(round(custom.width * ratio))),
                    max(1, int(round(custom.height * ratio))))
        logo = custom.resize(new_size, Image.LANCZOS)
        offset = (int(round(left + (width - logo.width) / 2)),
                  int(round(top + (height - logo.height) / 2)))
        canvas.paste(logo, offset, logo.split()[-1])
        return

    # Векторный знак: две половинки круга со сдвигом по вертикали.
    ss = _supersample(width, height)
    layer = Image.new("L", (width * ss, height * ss), 0)
    draw = ImageDraw.Draw(layer)
    radius = LOGO_RADIUS * scale * ss
    for center, start, end in ((LOGO_TOP_CENTER, 90, 270), (LOGO_BOTTOM_CENTER, -90, 90)):
        cx = (center[0] * scale - left) * ss
        cy = (center[1] * scale - top) * ss
        draw.pieslice((cx - radius, cy - radius, cx + radius, cy + radius),
                      start, end, fill=255)
    mask = layer.resize((width, height), Image.BOX)
    canvas.paste(SURFACE, (int(round(left)), int(round(top))), mask)


# --------------------------------------------------------------------------- #
# Главный рендер
# --------------------------------------------------------------------------- #
def render_card(profile, avatar=None, banner=None, size=None, scale=None,
                font_dir=None, logo=None, fmt="PNG", quality=92):
    """Собирает карточку и возвращает готовые байты картинки."""
    factor = _resolve_scale(size, scale)
    width = int(round(DESIGN_W * factor))
    height = int(round(DESIGN_H * factor))

    theme = resolve_theme(_field(profile, "theme", "blue"))
    background = THEMES[theme]

    canvas = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(canvas)

    # 1. логотип и подпись
    _draw_logo(canvas, factor, logo)
    draw.text(
        (LOGO_TEXT_LEFT * factor, LOGO_TEXT_BASELINE * factor),
        LOGO_TEXT,
        font=_font(FONT_LOGO * factor, True, font_dir),
        fill=SURFACE,
        anchor="ls",
    )

    # 2. белая плашка
    card_left = int(round(CARD_BOX[0] * factor))
    card_top = int(round(CARD_BOX[1] * factor))
    card_w = int(round(CARD_BOX[2] * factor)) - card_left
    card_h = int(round(CARD_BOX[3] * factor)) - card_top
    canvas.paste(SURFACE, (card_left, card_top),
                 _round_rect_mask(card_w, card_h, round(CARD_RADIUS * factor)))

    # 3. баннер (нижний блок 288x92)
    panel_left = int(round(PANEL_BOX[0] * factor))
    panel_top = int(round(PANEL_BOX[1] * factor))
    panel_w = int(round(PANEL_BOX[2] * factor)) - panel_left
    panel_h = int(round(PANEL_BOX[3] * factor)) - panel_top
    panel_mask = _round_rect_mask(panel_w, panel_h, round(PANEL_RADIUS * factor))
    banner_image = _open_image(banner)
    if banner_image is not None:
        canvas.paste(_cover(banner_image, panel_w, panel_h), (panel_left, panel_top), panel_mask)
    else:
        canvas.paste(background, (panel_left, panel_top), panel_mask)

    # 4. ава��арка в белом кольце
    outer = int(round(AVATAR_RADIUS * 2 * factor))
    outer_left = int(round((AVATAR_CENTER[0] - AVATAR_RADIUS) * factor))
    outer_top = int(round((AVATAR_CENTER[1] - AVATAR_RADIUS) * factor))
    canvas.paste(SURFACE, (outer_left, outer_top), _circle_mask(outer))

    photo_radius = max(1.0, AVATAR_RADIUS - AVATAR_RING)
    inner = int(round(photo_radius * 2 * factor))
    inner_left = int(round((AVATAR_CENTER[0] - photo_radius) * factor))
    inner_top = int(round((AVATAR_CENTER[1] - photo_radius) * factor))
    inner_mask = _circle_mask(inner)
    photo = _open_image(avatar)
    if photo is not None:
        canvas.paste(_cover(photo, inner, inner), (inner_left, inner_top), inner_mask)
    else:
        canvas.paste(background, (inner_left, inner_top), inner_mask)

    # 5. имя и тег
    text_limit = (TEXT_RIGHT - TEXT_LEFT) * factor
    name = _sanitize_text(
        _field(profile, "display_name", "") or _field(profile, "username", ""),
        _font(FONT_NAME * factor, False, font_dir),
        "Без имени",
    )
    tag_value = _field(profile, "tag", None)
    tag = ("#" + str(tag_value).lstrip("#")) if tag_value else ""

    tag_font = _font(FONT_TAG * factor, False, font_dir)
    tag_width = _text_width(tag_font, tag) + (TAG_GAP * factor if tag else 0)
    name_font = _fit(name, FONT_NAME * factor,
                     max(text_limit * 0.4, text_limit - tag_width), False, font_dir)

    name_x = TEXT_LEFT * factor
    name_baseline = NAME_BASELINE * factor
    draw.text((name_x, name_baseline), name, font=name_font, fill=INK_TITLE, anchor="ls")
    if tag:
        draw.text(
            (name_x + _text_width(name_font, name) + TAG_GAP * factor, name_baseline),
            tag, font=tag_font, fill=INK_TAG, anchor="ls",
        )

    # 6. описание
    bio_font = _font(FONT_BIO * factor, False, font_dir)
    bio = _sanitize_text(_field(profile, "bio", ""), bio_font)
    if bio:
        lines = _wrap(bio_font, bio[:BIO_LIMIT], text_limit, len(BIO_BASELINES))
        bio_color = INK_BODY
    else:
        lines = _wrap(bio_font, EMPTY_BIO_HINT, text_limit, 2)
        bio_color = INK_HINT
    for line, baseline in zip(lines, BIO_BASELINES):
        draw.text((name_x, baseline * factor), line, font=bio_font, fill=bio_color, anchor="ls")

    # 7. возраст и знак пола: только цифра и значок, всегда по центру.
    # Ничего не указано - строки нет совсем.
    age_raw = _field(profile, "age", None)
    try:
        age = int(age_raw) if age_raw not in (None, "") else None
    except (TypeError, ValueError):
        age = None
    if age is not None and not (5 <= age <= 100):
        age = None
    glyph = GENDER_GLYPHS.get(str(_field(profile, "gender", "") or "").lower())

    if age or glyph:
        meta_baseline = META_BASELINE * factor
        meta_font = _font(FONT_META * factor, False, font_dir)
        label = str(age) if age else ""
        text_width = _text_width(meta_font, label) if label else 0.0
        glyph_height = max(2.0, GLYPH_INK_HEIGHT * factor)
        glyph_width = _gender_ink_width(glyph, glyph_height, font_dir) if glyph else 0.0
        gap = (META_GLYPH_GAP + 2.2) * factor if (label and glyph) else 0.0
        total = text_width + gap + glyph_width
        start = AVATAR_CENTER[0] * factor - total / 2.0
        if label:
            draw.text((start, meta_baseline), label,
                      font=meta_font, fill=INK_TITLE, anchor="ls")
        if glyph:
            _draw_gender(
                canvas, glyph, start + text_width + gap,
                GLYPH_INK_BOTTOM * factor, factor, background, font_dir,
            )

    buffer = io.BytesIO()
    if str(fmt).upper() in ("JPG", "JPEG"):
        canvas.save(buffer, "JPEG", quality=int(quality), optimize=True, progressive=True)
    else:
        canvas.save(buffer, "PNG", compress_level=6)
    return buffer.getvalue()


def make_card(profile, avatar_path=None, output_path=None, font_dir=None,
              banner_path=None, size=None):
    """Совместимая обертка: пишет файл, если задан output_path."""
    payload = render_card(profile, avatar_path, banner_path, size=size, font_dir=font_dir)
    if not output_path:
        return payload
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return str(path)


if __name__ == "__main__":
    demo = {
        "display_name": "Crackford",
        "tag": "00000",
        "age": 17,
        "gender": "male",
        "bio": "меня зовут ибрагим, я люблю еду всякую есть, а также булочки",
        "theme": "blue",
    }
    for label, name in (("sd", "preview.png"), ("4k", "preview_4k.png")):
        data = render_card(demo, size=label)
        Path(name).write_bytes(data)
        print("%s -> %s (%d KB)" % (label, name, len(data) // 1024))
    for role, info in font_report().items():
        print("%-8s %s [%s] cyrillic=%s gender=%s" % (
            role, info["family"], info["source"], info["cyrillic"], info["gender_glyphs"]))
