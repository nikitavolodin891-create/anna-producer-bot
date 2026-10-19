"""Карусели для Instagram: разбор ответа Claude (JSON со слайдами) и отрисовка PNG 1080×1350.

Слайды рисуем сами через Pillow — без внешних сервисов, быстро и почти без памяти.
Акцентные слова в тексте Claude помечает **так** — они красятся в акцентный цвет.
"""
import io
import json
import os
import re

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1350          # формат 4:5 — максимальная площадь в ленте Instagram
PAD = 96                   # поля
HANDLE = "@anyalakshmi"
MAX_SLIDES = 10            # лимит Instagram и альбома Telegram

_HERE = os.path.dirname(os.path.abspath(__file__))
_FONT_DIRS = [os.path.join(_HERE, "fonts"), _HERE]

# Темы оформления. По умолчанию — светлая «кремовая»: тёплый фон, глубокий сливовый текст,
# золотисто-терракотовый акцент. Обложка и финальный слайд — контрастные.
THEMES = {
    "light": {
        "bg": "#F4EDE4", "text": "#3A1F2D", "muted": "#8A7266", "accent": "#B5714E",
        "cover_bg": "#3A1F2D", "cover_text": "#F4EDE4", "cover_accent": "#E2B48C",
        "cta_bg": "#B5714E", "cta_text": "#FFF8F1", "cta_accent": "#3A1F2D",
    },
    "dark": {
        "bg": "#1E1A1D", "text": "#F2E9E1", "muted": "#9C8E86", "accent": "#E2B48C",
        "cover_bg": "#1E1A1D", "cover_text": "#F2E9E1", "cover_accent": "#E2B48C",
        "cta_bg": "#E2B48C", "cta_text": "#1E1A1D", "cta_accent": "#7A3E2C",
    },
    "rose": {
        "bg": "#F7E8E6", "text": "#4A2330", "muted": "#9B7A7F", "accent": "#C4566E",
        "cover_bg": "#C4566E", "cover_text": "#FFF5F3", "cover_accent": "#4A2330",
        "cta_bg": "#4A2330", "cta_text": "#F7E8E6", "cta_accent": "#F0A8B4",
    },
}


class CarouselError(Exception):
    pass


def pick_theme(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"т[её]мн|черн|чёрн|dark", t):
        return "dark"
    if re.search(r"розов|пудров|нежн|rose|pink", t):
        return "rose"
    return "light"


# ---------------------------------------------------------------------------
# Разбор ответа модели
# ---------------------------------------------------------------------------
def parse(reply: str) -> dict:
    """Достаёт JSON из ответа Claude: {"slides": [{"title", "text"}...], "caption": "..."}."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", reply, re.DOTALL) or re.search(r"(\{.*\})", reply, re.DOTALL)
    if not m:
        raise CarouselError("модель не вернула слайды")
    raw = m.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # частая мелочь — «умные» кавычки или висящая запятая
        fixed = re.sub(r",\s*([\]}])", r"\1", raw.replace("“", '"').replace("”", '"'))
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError as e:
            raise CarouselError(f"не разобрала слайды ({e.msg})") from e
    slides = []
    for s in data.get("slides") or []:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()
        text = str(s.get("text") or "").strip()
        if title or text:
            slides.append({"title": title, "text": text})
    if len(slides) < 2:
        raise CarouselError("слишком мало слайдов")
    return {"slides": slides[:MAX_SLIDES], "caption": str(data.get("caption") or "").strip()}


def as_text(data: dict) -> str:
    """Человекочитаемая версия карусели — для истории диалога и чтобы Анна могла скопировать текст."""
    parts = []
    for i, s in enumerate(data["slides"], 1):
        line = f"Слайд {i}: {s['title']}"
        if s["text"]:
            line += f"\n{s['text']}"
        parts.append(line)
    if data.get("caption"):
        parts.append("Подпись к посту:\n" + data["caption"])
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Отрисовка
# ---------------------------------------------------------------------------
_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _font_cache:
        for d in _FONT_DIRS:
            path = os.path.join(d, name)
            if os.path.exists(path):
                _font_cache[key] = ImageFont.truetype(path, size)
                break
        else:
            raise CarouselError(f"нет шрифта {name}")
    return _font_cache[key]


TITLE_FONT = "PlayfairDisplay-Bold.ttf"
BODY_FONT = "Montserrat-Medium.ttf"
LABEL_FONT = "Montserrat-SemiBold.ttf"


def _tokens(text: str) -> list[tuple[str, bool]]:
    """Разбивает текст на слова с пометкой «акцент» (внутри **…**). Переносы строк — отдельный токен.
    Слово, «прилипшее» к акценту без пробела (знак препинания, окончание), помечается приставкой \x00."""
    out: list[tuple[str, bool]] = []
    accent = False
    prev_end_glued = False
    for part in re.split(r"(\*\*)", text):
        if part == "**":
            accent = not accent
            continue
        glue_start = bool(out) and part[:1] not in ("", " ", "\n", "\t") and prev_end_glued
        for line_i, line in enumerate(part.split("\n")):
            if line_i:
                out.append(("\n", False))
            for wi, w in enumerate(line.split()):
                if line_i == 0 and wi == 0 and glue_start:
                    w = "\x00" + w
                if w in ("—", "–") and out and out[-1][0] != "\n":
                    # тире не переносим на новую строку — держим его с предыдущим словом
                    out[-1] = (out[-1][0] + " " + w, out[-1][1])
                    continue
                out.append((w, accent))
        prev_end_glued = part[-1:] not in ("", " ", "\n", "\t")
    return out


def _wrap(tokens: list[tuple[str, bool]], font: ImageFont.FreeTypeFont, width: int) -> list[list[tuple[str, bool]]]:
    lines: list[list[tuple[str, bool]]] = [[]]
    space = font.getlength(" ")
    cur = 0.0
    for word, acc in tokens:
        if word == "\n":
            lines.append([])
            cur = 0.0
            continue
        glued = word.startswith("\x00")
        w = font.getlength(word.lstrip("\x00"))
        if glued and lines[-1]:
            cur += w
            lines[-1].append((word, acc))
            continue
        if lines[-1] and cur + space + w > width:
            lines.append([])
            cur = 0.0
        cur += (space if lines[-1] else 0) + w
        lines[-1].append((word, acc))
    return lines


def _fit(text: str, font_name: str, width: int, max_h: int, start: int, minimum: int, leading: float):
    """Подбирает самый крупный кегль, при котором текст влезает в блок."""
    tokens = _tokens(text)
    size = start
    while True:
        font = _font(font_name, size)
        lines = _wrap(tokens, font, width)
        line_h = int(size * leading)
        if len(lines) * line_h <= max_h or size <= minimum:
            return font, lines, line_h
        size -= 2


def _draw_lines(draw, lines, font, line_h, x, y, color, accent, align="left", width=0):
    space = font.getlength(" ")
    for line in lines:
        line_w = sum(font.getlength(w.lstrip("\x00")) + (0 if w.startswith("\x00") else space) for w, _ in line) - space
        cx = x + (width - line_w) / 2 if align == "center" else x
        for i, (w, acc) in enumerate(line):
            if i and not w.startswith("\x00"):
                cx += space
            w = w.lstrip("\x00")
            draw.text((cx, y), w, font=font, fill=accent if acc else color)
            cx += font.getlength(w)
        y += line_h
    return y


def _footer(draw, theme_text, theme_muted, idx, total, show_arrow=True):
    f = _font(LABEL_FONT, 30)
    y = H - PAD + 10
    draw.text((PAD, y), HANDLE, font=f, fill=theme_muted, anchor="ls")
    if total:
        label = f"{idx:02d} / {total:02d}"
        draw.text((W - PAD, y), label + ("   →" if show_arrow else ""), font=f, fill=theme_muted, anchor="rs")


def render_slide(slide: dict, idx: int, total: int, theme_name: str = "light") -> bytes:
    th = THEMES.get(theme_name, THEMES["light"])
    is_cover, is_last = idx == 1, idx == total and total > 2
    if is_cover:
        bg, color, accent, muted = th["cover_bg"], th["cover_text"], th["cover_accent"], th["cover_accent"]
    elif is_last:
        bg, color, accent, muted = th["cta_bg"], th["cta_text"], th["cta_accent"], th["cta_text"]
    else:
        bg, color, accent, muted = th["bg"], th["text"], th["accent"], th["muted"]

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    inner_w = W - 2 * PAD
    title, text = slide.get("title", ""), slide.get("text", "")

    if is_cover:
        # Обложка: крупный заголовок по центру по вертикали, подзаголовок ниже, тонкая линия-акцент
        tf, tl, th_ = _fit(title, TITLE_FONT, inner_w, 640, 112, 64, 1.12)
        sub_font, sub_lines, sub_h = _fit(text, BODY_FONT, inner_w, 260, 42, 30, 1.4) if text else (None, [], 0)
        block = len(tl) * th_ + (60 + len(sub_lines) * sub_h if sub_lines else 0)
        y = max(PAD + 80, (H - block) // 2 - 40)
        d.rectangle([PAD, y - 70, PAD + 120, y - 64], fill=accent)
        y = _draw_lines(d, tl, tf, th_, PAD, y, color, accent)
        if sub_lines:
            _draw_lines(d, sub_lines, sub_font, sub_h, PAD, y + 50, color, accent)
        f = _font(LABEL_FONT, 30)
        d.text((PAD, H - PAD + 10), HANDLE, font=f, fill=muted, anchor="ls")
        d.text((W - PAD, H - PAD + 10), "листай  →", font=f, fill=muted, anchor="rs")
    else:
        # Обычный слайд: большой номер-акцент, заголовок, текст
        num_font = _font(TITLE_FONT, 120)
        top, bottom = PAD + 20, H - PAD - 90
        num_h = 0 if is_last else 170
        avail = bottom - top - num_h
        tf, tl, th_ = _fit(title, TITLE_FONT, inner_w, int(avail * 0.42), 84, 52, 1.15) if title else (None, [], 0)
        title_h = len(tl) * th_ + (44 if tl and text else 0)
        bf, bl, bh = _fit(text, BODY_FONT, inner_w, avail - title_h, 50, 30, 1.45) if text else (None, [], 0)
        block = num_h + title_h + len(bl) * bh
        # блок контента — по центру по вертикали, но не выше верхнего поля
        y = max(top, top + (bottom - top - block) // 2 - 30)
        if not is_last:
            d.text((PAD, y - 20), f"{idx - 1:02d}", font=num_font, fill=accent)
            y += num_h
        if tl:
            y = _draw_lines(d, tl, tf, th_, PAD, y, color, accent) + 44
        if bl:
            _draw_lines(d, bl, bf, bh, PAD, y, color, accent)
        _footer(d, color, muted, idx, total, show_arrow=not is_last)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render(data: dict, theme_name: str = "light") -> list[bytes]:
    total = len(data["slides"])
    return [render_slide(s, i, total, theme_name) for i, s in enumerate(data["slides"], 1)]
