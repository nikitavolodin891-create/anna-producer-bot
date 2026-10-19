"""Карусели для Instagram: конструктор дизайнов и отрисовка PNG 1080×1350.

Claude пишет JSON: слайды + подпись + «design» — описание оформления. Дизайн собирается из деталей:
фон (градиент, вода, шёлк, лён, бумага, кружево, свечи, звёзды, закат, фото, нейрофон), эффекты
(зерно, пальмы, тени пальм, боке, искры, жемчуг), украшения (рамки, монограмма, сургуч, карточка),
шрифты и цвета (включая «золото» — тиснение градиентом). Есть готовые пресеты, любые поля можно
переопределить. Акцентные слова в тексте помечаются **так**.
"""
import io
import json
import math
import os
import random
import re

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1080, 1350          # 4:5 — максимальная площадь в ленте Instagram
HANDLE = "@anyalakshmi"
SIGNATURE = "Анна"
MONOGRAM = "АВ"
MAX_SLIDES = 10

_HERE = os.path.dirname(os.path.abspath(__file__))
_FONT_DIRS = [os.path.join(_HERE, "fonts"), _HERE]


class CarouselError(Exception):
    pass


# ===========================================================================
# Разбор ответа модели
# ===========================================================================
def _json_from(reply: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", reply, re.DOTALL) or re.search(r"(\{.*\})", reply, re.DOTALL)
    if not m:
        raise CarouselError("модель не вернула слайды")
    raw = m.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        fixed = re.sub(r",\s*([\]}])", r"\1", raw.replace("“", '"').replace("”", '"'))
        try:
            return json.loads(fixed)
        except json.JSONDecodeError as e:
            raise CarouselError(f"не разобрала слайды ({e.msg})") from e


def parse(reply: str) -> dict:
    """{"slides": [{"title","text","kicker"?}], "caption": str, "design": dict}."""
    data = _json_from(reply)
    slides = []
    for s in data.get("slides") or []:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()
        text = str(s.get("text") or "").strip()
        if title or text:
            item = {"title": title, "text": text}
            if s.get("kicker"):
                item["kicker"] = str(s["kicker"]).strip()[:40]
            slides.append(item)
    if len(slides) < 2:
        raise CarouselError("слишком мало слайдов")
    design = data.get("design") if isinstance(data.get("design"), dict) else {}
    return {"slides": slides[:MAX_SLIDES], "caption": str(data.get("caption") or "").strip(), "design": design}


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


def design_note(design: dict) -> str:
    """Короткая запись дизайна для истории диалога (чтобы правки «сделай темнее» работали)."""
    d = {k: v for k, v in (design or {}).items() if k not in ("_photo",)}
    return "Дизайн: " + json.dumps(d, ensure_ascii=False)


# ===========================================================================
# Шрифты
# ===========================================================================
_fc: dict = {}


def _font(name: str, size: int, wght=None) -> ImageFont.FreeTypeFont:
    key = (name, size, wght)
    if key not in _fc:
        for d in _FONT_DIRS:
            path = os.path.join(d, name)
            if os.path.exists(path):
                f = ImageFont.truetype(path, size)
                break
        else:
            raise CarouselError(f"нет шрифта {name}")
        if wght is not None:
            try:
                vals = []
                for a in f.get_variation_axes():
                    n = a.get("name", b"")
                    n = n.decode() if isinstance(n, bytes) else str(n)
                    vals.append(max(a["minimum"], min(a["maximum"], wght)) if n.lower().startswith("weight") else a["default"])
                f.set_variation_by_axes(vals)
            except Exception:
                pass
        _fc[key] = f
    return _fc[key]


class Face:
    def __init__(self, file, wght=None, scale=1.0, upper=False, track=0):
        self.file, self.wght, self.scale, self.upper, self.track = file, wght, scale, upper, track

    def at(self, size):
        return _font(self.file, max(8, int(size * self.scale)), self.wght)


# Библиотека шрифтов: имя → начертание. Все с кириллицей.
FACES = {
    "playfair": Face("PlayfairDisplay-VF.ttf", 600),
    "playfair_bold": Face("PlayfairDisplay-VF.ttf", 800),
    "playfair_italic": Face("PlayfairDisplay-Italic-VF.ttf", 500, 1.05),
    "cormorant": Face("CormorantGaramond-VF.ttf", 600),
    "cormorant_light": Face("CormorantGaramond-VF.ttf", 400),
    "cormorant_italic": Face("CormorantGaramond-Italic-VF.ttf", 500, 1.08),
    "cormorant_caps": Face("CormorantGaramond-VF.ttf", 600, 1.0, True, 1),
    "forum_caps": Face("Forum-Regular.ttf", None, 1.0, True, 2),
    "tenor_caps": Face("TenorSans-Regular.ttf", None, 0.92, True, 3),
    "tenor": Face("TenorSans-Regular.ttf"),
    "manrope": Face("Manrope-VF.ttf", 700),
    "manrope_bold": Face("Manrope-VF.ttf", 800),
    "montserrat": Face("Montserrat-SemiBold.ttf"),
    "script": Face("GreatVibes-Regular.ttf", None, 1.42),
}
BODY_FACES = {
    "manrope": Face("Manrope-VF.ttf", 400),
    "cormorant": Face("CormorantGaramond-VF.ttf", 500, 1.08),
    "montserrat": Face("Montserrat-Medium.ttf"),
    "tenor": Face("TenorSans-Regular.ttf"),
    "playfair": Face("PlayfairDisplay-VF.ttf", 400),
}
SMALL_FONT = ("TenorSans-Regular.ttf", 24)


# ===========================================================================
# Текст: разметка **акцент**, перенос, подбор кегля, отрисовка (в т.ч. золотом и свечением)
# ===========================================================================
def _tokens(text):
    out, acc, prev = [], False, False
    for part in re.split(r"(\*\*)", text):
        if part == "**":
            acc = not acc
            continue
        glue = bool(out) and part[:1] not in ("", " ", "\n") and prev
        prev = part[-1:] not in ("", " ", "\n")
        for li, line in enumerate(part.split("\n")):
            if li:
                out.append(("\n", False))
            for i, w in enumerate(line.split()):
                if li == 0 and i == 0 and glue:
                    out.append(("\x00" + w, acc))
                    continue
                if w in ("—", "–") and out and out[-1][0] != "\n":
                    out[-1] = (out[-1][0] + " " + w, out[-1][1])
                    continue
                out.append((w, acc))
    return out


def _word(w, face):
    w = w.lstrip("\x00")
    return w.upper() if face.upper else w


def _wlen(w, face, size):
    s = _word(w, face)
    return face.at(size).getlength(s) + face.track * max(0, len(s) - 1)


def _layout(text, fn, fa, size, width):
    lines, cur = [[]], 0.0
    sp = fn.at(size).getlength(" ")
    for w, a in _tokens(text):
        if w == "\n":
            lines.append([])
            cur = 0.0
            continue
        face = fa if a else fn
        ww = _wlen(w, face, size)
        if w.startswith("\x00") and lines[-1]:
            lines[-1].append((w, a))
            cur += ww
            continue
        if lines[-1] and cur + sp + ww > width:
            lines.append([])
            cur = 0.0
        cur += (sp if lines[-1] else 0) + ww
        lines[-1].append((w, a))
    return [l for l in lines if l] or [[]]


def _line_w(line, fn, fa, size):
    sp = fn.at(size).getlength(" ")
    t = 0.0
    for i, (w, a) in enumerate(line):
        if i and not w.startswith("\x00"):
            t += sp
        t += _wlen(w, fa if a else fn, size)
    return t


def _fit(text, fn, fa, width, maxh, start, mn, lead):
    s = start
    while True:
        ls = _layout(text, fn, fa, s, width)
        ok = len(ls) * s * lead <= maxh and max(_line_w(l, fn, fa, s) for l in ls) <= width + 1
        if ok or s <= mn:
            return s, ls
        s -= 2


_TEX: dict = {}


def _gold_tex(tone):
    y = np.linspace(0, 1, H, dtype=np.float32)[:, None]
    x = np.linspace(0, 1, W, dtype=np.float32)[None, :]
    t = ((y * 0.8 + x * 0.35) * 1.6) % 1.0
    if tone == "gold":
        stops = [(0, (122, 88, 38)), (.22, (226, 196, 128)), (.38, (176, 132, 60)), (.55, (247, 226, 168)),
                 (.72, (166, 122, 52)), (.88, (232, 204, 140)), (1, (130, 94, 40))]
    elif tone == "rose_gold":
        stops = [(0, (150, 92, 86)), (.3, (236, 190, 176)), (.5, (176, 110, 100)), (.7, (246, 210, 196)), (1, (150, 92, 86))]
    else:  # pearl / silver
        stops = [(0, (170, 170, 178)), (.35, (250, 250, 252)), (.6, (196, 194, 202)), (1, (240, 238, 244))]
    a = np.zeros((H, W, 3), np.float32)
    for c in range(3):
        a[..., c] = np.interp(t, [s for s, _ in stops], [col[c] for _, col in stops])
    a += np.random.default_rng(3).normal(0, 5, (H, W, 1)).astype(np.float32)
    return Image.fromarray(np.clip(a, 0, 255).astype("uint8"))


def _paint(spec):
    if isinstance(spec, str) and spec.startswith("@"):
        tone = spec[1:]
        if tone not in _TEX:
            _TEX[tone] = _gold_tex(tone)
        return _TEX[tone]
    return Image.new("RGB", (W, H), spec)


def _draw_block(img, lines, fn, fa, x, y, width, size, lead, align, ink, acc, shadow=None, glow=None, acc_glow=None):
    mn, ma = Image.new("L", (W, H), 0), Image.new("L", (W, H), 0)
    dn, da = ImageDraw.Draw(mn), ImageDraw.Draw(ma)
    base = max(fn.at(size).getmetrics()[0], fa.at(size).getmetrics()[0])
    sp = fn.at(size).getlength(" ")
    cy = y
    for line in lines:
        lw = _line_w(line, fn, fa, size)
        cx = x + (width - lw) / 2 if align == "center" else x
        for i, (w, a) in enumerate(line):
            if i and not w.startswith("\x00"):
                cx += sp
            face = fa if a else fn
            f = face.at(size)
            s = _word(w, face)
            dd = da if a else dn
            by = cy + base - f.getmetrics()[0]
            if face.track:
                for ch in s:
                    dd.text((cx, by), ch, font=f, fill=255)
                    cx += f.getlength(ch) + face.track
                cx -= face.track
            else:
                dd.text((cx, by), s, font=f, fill=255)
                cx += f.getlength(s)
        cy += size * lead
    both = ImageChops.lighter(mn, ma)
    if shadow:
        col, r, op = shadow
        img.paste(Image.new("RGB", (W, H), col), (0, 4), both.filter(ImageFilter.GaussianBlur(r)).point(lambda v: int(v * op)))
    if glow:
        img.paste(Image.new("RGB", (W, H), glow), (0, 0), both.filter(ImageFilter.GaussianBlur(18)).point(lambda v: min(255, int(v * 0.9))))
    if acc_glow:
        img.paste(Image.new("RGB", (W, H), acc_glow), (0, 0), ma.filter(ImageFilter.GaussianBlur(24)).point(lambda v: min(255, int(v * 1.2))))
    img.paste(_paint(ink), (0, 0), mn)
    img.paste(_paint(acc), (0, 0), ma)


def _simple(img, xy, s, font, fill, anchor="la", track=0):
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    if not track:
        d.text(xy, s, font=font, fill=255, anchor=anchor)
    else:
        tw = sum(font.getlength(c) for c in s) + track * (len(s) - 1)
        x, y = xy
        if anchor[0] == "m":
            x -= tw / 2
        elif anchor[0] == "r":
            x -= tw
        for c in s:
            d.text((x, y), c, font=font, fill=255, anchor="l" + anchor[1])
            x += font.getlength(c) + track
    img.paste(_paint(fill), (0, 0), m)


# ===========================================================================
# Фоны и фактуры
# ===========================================================================
_YY, _XX = np.mgrid[0:H, 0:W].astype(np.float32)


def _hex(h):
    h = str(h).strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32)


def _img(a):
    return Image.fromarray(np.clip(a, 0, 255).astype("uint8"))


def _blob(a, x, y, r, col, s):
    w = (np.exp(-((_XX - x) ** 2 + (_YY - y) ** 2) / (2 * r * r)) * s)[..., None]
    return a * (1 - w) + _hex(col) * w


def _vgrad(cols):
    t = _YY / H
    pos = np.linspace(0, 1, len(cols))
    a = np.zeros((H, W, 3), np.float32)
    for c in range(3):
        a[..., c] = np.interp(t, pos, [_hex(col)[c] for col in cols])
    return a


def _smooth_noise(cells, seed):
    r = np.random.default_rng(seed).random((cells[1], cells[0])).astype(np.float32)
    im = Image.fromarray((r * 255).astype("uint8")).resize((W, H), Image.BICUBIC)
    return np.asarray(im).astype(np.float32) / 255


def _caustics(seed):
    n1 = _smooth_noise((9, 11), seed) + 0.5 * _smooth_noise((19, 23), seed + 1)
    n2 = _smooth_noise((7, 9), seed + 2) + 0.5 * _smooth_noise((15, 19), seed + 3)
    c = np.clip(np.exp(-(np.sin(n1 * 26) ** 2) / 0.02) * 0.6 + np.exp(-(np.sin(n2 * 22) ** 2) / 0.03) * 0.5, 0, 1)
    return np.asarray(_img(c * 255).filter(ImageFilter.GaussianBlur(3))).astype(np.float32) / 255


def _silk(seed):
    n = _smooth_noise((3, 4), seed) * 3.0
    v = np.sin(_XX / W * 5.0 + _YY / H * 7.5 + n * 2.2) + 0.6 * np.sin(_XX / W * 11 - _YY / H * 3 + n * 3.1)
    return ((v - v.min()) / (v.max() - v.min()))[..., None]


def _grain(a, s, seed):
    return a + np.random.default_rng(seed).normal(0, s, (H, W, 1)).astype(np.float32)


def _linen(a, s, seed):
    r = np.random.default_rng(seed)
    return a + (r.normal(0, 1, (H, 1, 1)) + r.normal(0, 1, (1, W, 1))).astype(np.float32) * s


def _vignette(a, s, col="#000000"):
    d = np.sqrt(((_XX - W / 2) / (W * 0.75)) ** 2 + ((_YY - H / 2) / (H * 0.75)) ** 2)
    w = (np.clip(d, 0, 1) ** 2 * s)[..., None]
    return a * (1 - w) + _hex(col) * w


def _frond(d, sx, sy, ang, length, width, rnd):
    pts = []
    bend = rnd.uniform(-0.5, 0.5)
    for i in range(41):
        t = i / 40
        a = ang + bend * t * t
        pts.append((sx + math.cos(a) * length * t, sy + math.sin(a) * length * t))
    d.line(pts, fill=255, width=max(3, int(width * 0.08)))
    for i in range(3, 40, 2):
        t = i / 40
        px, py = pts[i]
        a = ang + bend * t * t
        L = width * (1.0 - t * 0.75) * rnd.uniform(0.85, 1.1)
        for side in (-1, 1):
            la = a + side * (0.9 + rnd.uniform(-0.15, 0.15)) - side * 0.25 * t
            nx, ny = math.cos(a) * width * 0.045, math.sin(a) * width * 0.045
            d.polygon([(px - nx, py - ny), (px + nx, py + ny), (px + math.cos(la) * L, py + math.sin(la) * L)], fill=255)


def _palms(spec, blur, seed):
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    rnd = random.Random(seed)
    for f in spec:
        _frond(d, *f, rnd)
    if blur:
        m = m.filter(ImageFilter.GaussianBlur(blur))
    return (np.asarray(m).astype(np.float32) / 255)[..., None]


PALMS_TOP = [(-40, -30, 0.55, 760, 300), (-60, 180, 0.15, 620, 260), (W + 40, -60, 2.55, 700, 280), (W + 60, 260, 3.0, 560, 230)]
PALMS_SHADOW = [[(W + 80, -80, 2.35, 1050, 360), (W + 120, 380, 2.8, 820, 300)],
                [(-80, -60, 0.75, 1000, 340), (-100, 420, 0.25, 760, 280)]]


def _bokeh(a, items, seed):
    lay = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(lay)
    r = random.Random(seed)
    for cx, cy, spread, n, rmin, rmax, col in items:
        base = _hex(col)
        for _ in range(n):
            x, y, rr, k = cx + r.gauss(0, spread), cy + r.gauss(0, spread * 0.8), r.uniform(rmin, rmax), r.uniform(0.35, 0.9)
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=tuple(int(v * k) for v in base))
    return a + np.asarray(lay.filter(ImageFilter.GaussianBlur(6))).astype(np.float32) * 0.9


def _sparkles(img, n, area, seed, smax=24, col=(255, 236, 190)):
    r = random.Random(seed)
    lay = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(lay)
    x0, y0, x1, y1 = area
    for _ in range(n):
        x, y = r.uniform(x0, x1), r.uniform(y0, y1)
        s = r.choice([1, 1, 1, 2, 2, 3]) * r.uniform(0.6, 1.0)
        if s > 2.2:
            L = smax * r.uniform(0.6, 1)
            d.polygon([(x, y - L), (x + L * 0.12, y), (x, y + L), (x - L * 0.12, y)], fill=col)
            d.polygon([(x - L * 0.7, y), (x, y + L * 0.1), (x + L * 0.7, y), (x, y - L * 0.1)], fill=col)
        else:
            d.ellipse([x - s, y - s, x + s, y + s], fill=col)
    return ImageChops.add(ImageChops.add(img, lay.filter(ImageFilter.GaussianBlur(5))), lay)


def _lace(a, col, op=0.07, step=150):
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    for gy in range(-step, H + step, step):
        for gx in range(-step, W + step, step):
            cx, cy = gx + (step // 2 if (gy // step) % 2 else 0), gy
            for rr in (18, 34, 52):
                d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=255, width=2)
            for k in range(8):
                an = k * math.pi / 4
                px, py = cx + math.cos(an) * 62, cy + math.sin(an) * 62
                d.ellipse([px - 14, py - 14, px + 14, py + 14], outline=255, width=2)
    w = (np.asarray(m.filter(ImageFilter.GaussianBlur(0.8))).astype(np.float32) / 255 * op)[..., None]
    return a * (1 - w) + _hex(col) * w


def _pearls(img, items):
    for cx, cy, r in items:
        size = int(r * 3.6)
        c = size / 2
        sh = Image.new("L", (size, size), 0)
        ImageDraw.Draw(sh).ellipse([c - r + r * 0.15, c - r + r * 0.25, c + r + r * 0.15, c + r + r * 0.25], fill=80)
        sh = sh.filter(ImageFilter.GaussianBlur(r * 0.25))
        img.paste(Image.new("RGB", (size, size), (120, 60, 70)), (int(cx - c), int(cy - c)), sh)
        ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
        dx, dy = (xs - c) / r, (ys - c) / r
        inside = (dx * dx + dy * dy) <= 1
        nz = np.sqrt(np.clip(1 - dx * dx - dy * dy, 0, 1))
        light = np.clip(-0.45 * dx - 0.6 * dy + 0.65 * nz, 0, 1)
        col = np.array([236, 222, 220]) * (0.62 + 0.38 * light[..., None])
        col = col + np.exp(-(((dx + 0.35) ** 2 + (dy + 0.42) ** 2) / 0.02))[..., None] * 255
        col = col + np.stack([np.sin(dx * 3) * 8, np.sin(dy * 3 + 1) * 6, np.cos(dx * 2) * 10], -1)
        pi = Image.fromarray(np.dstack([np.clip(col, 0, 255), inside * 255]).astype("uint8"), "RGBA")
        img.paste(pi, (int(cx - c), int(cy - c)), pi)


def _sun(a, x, y, r, col, s):
    d = np.sqrt((_XX - x) ** 2 + (_YY - y) ** 2)
    w = (np.clip(np.clip(1 - (d - r) / 6, 0, 1) + np.exp(-(d / (r * 3.2)) ** 2) * 0.55, 0, 1) * s)[..., None]
    return a * (1 - w) + _hex(col) * w


def _cover_crop(im, w=W, h=H):
    return ImageOps.fit(im.convert("RGB"), (w, h), Image.LANCZOS, centering=(0.5, 0.4))


def _luma(col):
    if isinstance(col, str) and col.startswith("@"):
        return 200
    c = _hex(col)
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def build_background(d: dict, role: str, idx: int, photo=None, ai_bg=None) -> Image.Image:
    bg = d["background"]
    t = bg.get("type", "solid")
    cols = bg.get("colors") or ["#F4EDE4"]
    c0 = cols[0]
    c1 = cols[1] if len(cols) > 1 else c0
    seed = 11 + idx
    if role == "cover" and photo is not None:
        t = "photo"
    if t == "photo" and photo is not None:
        im = _cover_crop(photo)
        a = np.asarray(im).astype(np.float32)
        dark = True
        grad = np.clip((_YY / H - 0.25) / 0.75, 0, 1)[..., None] ** 1.3
        a = a * (1 - grad * 0.78) + (_hex("#0E0A0A") if dark else _hex("#F7F1EA")) * grad * 0.78
        return _img(_grain(a, 4, seed))
    if t == "ai" and ai_bg is not None:
        im = _cover_crop(ai_bg)
        if role != "cover":
            im = im.filter(ImageFilter.GaussianBlur(10))
        a = np.asarray(im).astype(np.float32)
        tint = _hex("#0C0A0E") if d.get("_light_ink", True) else _hex("#FAF6F0")
        k = 0.30 if role == "cover" else 0.52
        return _img(_grain(a * (1 - k) + tint * k, 4, seed))
    if t in ("ai", "photo"):
        t = "mesh"
    if t == "solid":
        a = np.ones((H, W, 3), np.float32) * _hex(c0)
    elif t == "gradient":
        a = _vgrad(cols if len(cols) > 1 else [c0, c0])
    elif t == "mesh":
        a = np.ones((H, W, 3), np.float32) * _hex(c0)
        spots = [(W * 0.2, H * 0.15, 520, 0.85), (W * 0.9, H * 0.85, 600, 0.7), (W * 0.85, H * 0.2, 380, 0.5)]
        for (x, y, r, s), col in zip(spots, cols[1:] or [c0]):
            a = _blob(a, x, y, r, col, s)
    elif t == "water":
        a = _vgrad([c0, c1, cols[2] if len(cols) > 2 else c1])
        a = a + _caustics(seed)[..., None] * np.array([150, 225, 215], np.float32) * 0.30
        a = _blob(a, W * 0.85, -100, 520, "#BFF3EA", 0.3)
    elif t == "sunset":
        a = _vgrad(cols if len(cols) > 2 else ["#2A2050", "#6E3A78", "#B4527E", "#E57A66", "#F6B278"])
        a = _sun(a, W * 0.5, H * 1.04, 110, "#FFE9C2", 0.75)
    elif t == "silk":
        s = _silk(seed)
        a = _hex(c0) * (1 - s) + _hex(c1) * s
        a = a + np.exp(-((s[..., 0] - 0.82) ** 2) / 0.004)[..., None] * 40
    elif t == "linen":
        a = _blob(np.ones((H, W, 3), np.float32) * _hex(c0), W * 0.8, H * 0.1, 600, c1, 0.6)
        a = _linen(a, 3, seed)
    elif t == "paper":
        a = _blob(np.ones((H, W, 3), np.float32) * _hex(c0), W * 0.2, H * 0.1, 500, c1, 0.6)
        a = a - _smooth_noise((40, 50), seed)[..., None] * 10
    elif t == "lace":
        a = _blob(np.ones((H, W, 3), np.float32) * _hex(c0), W * 0.5, H * 0.35, 700, c1, 0.9)
        a = _lace(a, cols[2] if len(cols) > 2 else "#E9DCC8")
    elif t == "candle":
        a = np.ones((H, W, 3), np.float32) * _hex(c0)
        a = _blob(a, W * 0.78, H * 0.18, 380, c1, 0.85)
        a = _blob(a, W * 0.15, H * 0.95, 450, cols[2] if len(cols) > 2 else c0, 0.8)
        a = _bokeh(a, [(W * 0.85, H * 0.12, 150, 20, 10, 44, "#FFB86B"), (W * 0.08, H * 0.93, 110, 10, 8, 32, "#FF9F5A")], seed)
    elif t == "stars":
        a = _blob(np.ones((H, W, 3), np.float32) * _hex(c0), W * 0.5, H * 0.3, 620, c1, 0.85)
    else:
        a = np.ones((H, W, 3), np.float32) * _hex(c0)

    fx = set(d.get("effects") or [])
    if "linen" in fx and t != "linen":
        a = _linen(a, 4, seed)
    if "palms" in fx:
        pm = _palms(PALMS_TOP, 1, 4)
        a = a * (1 - pm * 0.92) + _hex(d.get("palm_color", "#2A1630")) * pm * 0.92
    if "palm_shadow" in fx:
        pm = _palms(PALMS_SHADOW[idx % 2], 11, 8)
        a = a * (1 - pm * 0.22) + _hex("#5A4A3A") * pm * 0.22
    if "bokeh" in fx and t != "candle":
        a = _bokeh(a, [(W * 0.85, H * 0.12, 150, 16, 10, 40, cols[-1]), (W * 0.1, H * 0.92, 110, 9, 8, 30, cols[-1])], seed)
    if "vignette" in fx:
        a = _vignette(a, 0.45, "#000000" if _luma(c0) < 128 else "#6B4A2A")
    if "grain" in fx:
        a = _grain(a, 6, seed)
    img = _img(a)
    if "sparkles" in fx or t == "stars":
        img = _sparkles(img, 45, (0, 0, W, 300), seed, 24)
        img = _sparkles(img, 30, (0, H - 220, W, H), seed + 7, 18)
        img = _sparkles(img, 12, (0, 300, 90, H - 220), seed + 9, 14)
        img = _sparkles(img, 12, (W - 90, 300, W, H - 220), seed + 13, 14)
    return img


# ===========================================================================
# Украшения
# ===========================================================================
def _orn_paint(d):
    return d.get("ornament_color") or d["muted"]


def _frame(img, d, gold):
    m = Image.new("L", (W, H), 0)
    dr = ImageDraw.Draw(m)
    i = 46
    dr.rectangle([i, i, W - i, H - i], outline=255, width=2)
    dr.rectangle([i + 12, i + 12, W - i - 12, H - i - 12], outline=255, width=1)
    if gold:
        for cx, cy in [(i, i), (W - i, i), (i, H - i), (W - i, H - i)]:
            dr.polygon([(cx, cy - 14), (cx + 14, cy), (cx, cy + 14), (cx - 14, cy)], fill=255)
    img.paste(_paint("@gold" if gold else _orn_paint(d)), (0, 0), m)


def _lines(img, d):
    m = Image.new("L", (W, H), 0)
    dr = ImageDraw.Draw(m)
    dr.line([96, 140, W - 96, 140], fill=255, width=2)
    dr.line([96, H - 140, W - 96, H - 140], fill=255, width=2)
    img.paste(_paint("@gold" if "gold" in (d.get("ornaments") or []) or d["accent"] == "@gold" else _orn_paint(d)), (0, 0), m)


def _divider(img, cx, y, d):
    m = Image.new("L", (W, H), 0)
    dr = ImageDraw.Draw(m)
    dr.line([cx - 60, y, cx - 16, y], fill=255, width=2)
    dr.line([cx + 16, y, cx + 60, y], fill=255, width=2)
    dr.polygon([(cx, y - 8), (cx + 8, y), (cx, y + 8), (cx - 8, y)], fill=255)
    img.paste(_paint("@gold" if d.get("_gold_orn") else _orn_paint(d)), (0, 0), m)


def _monogram(img, cx, cy, r, d):
    m = Image.new("L", (W, H), 0)
    dr = ImageDraw.Draw(m)
    dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=255, width=2)
    dr.ellipse([cx - r + 8, cy - r + 8, cx + r - 8, cy + r - 8], outline=255, width=1)
    dr.text((cx, cy + 2), MONOGRAM, font=_font("CormorantGaramond-VF.ttf", int(r * 0.95), 500), fill=255, anchor="mm")
    img.paste(_paint("@gold" if d.get("_gold_orn") else _orn_paint(d)), (0, 0), m)


def _wax_seal(img, cx, cy, r, col=(140, 28, 36)):
    ang = np.arctan2(_YY - cy, _XX - cx)
    rr = r * (1 + 0.06 * np.sin(ang * 9) + 0.03 * np.sin(ang * 23))
    dist = np.sqrt((_XX - cx) ** 2 + (_YY - cy) ** 2)
    inside = dist <= rr
    shade = np.clip(1.1 - ((_XX - cx + r * 0.3) ** 2 + (_YY - cy + r * 0.3) ** 2) / (r * r * 2.2), 0.55, 1.15)
    ring = np.exp(-((dist - r * 0.7) ** 2) / (r * 0.03) ** 2) * 0.35
    a = np.stack([col[c] * shade * (1 - ring) for c in range(3)], -1)
    mask = Image.fromarray((inside * 255).astype("uint8"))
    img.paste(Image.new("RGB", (W, H), (40, 20, 10)), (6, 10), mask.filter(ImageFilter.GaussianBlur(10)).point(lambda v: int(v * 0.5)))
    img.paste(_img(a), (0, 0), mask)
    dr = ImageDraw.Draw(img)
    f = _font("GreatVibes-Regular.ttf", int(r * 1.1))
    dr.text((cx, cy + 2), SIGNATURE[0], font=f, fill=(96, 14, 22), anchor="mm")
    dr.text((cx - 2, cy), SIGNATURE[0], font=f, fill=(186, 72, 76), anchor="mm")


def _card(img, box, d):
    x0, y0, x1, y1 = box
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    light = _luma(d["ink"]) < 128
    fill = (255, 248, 238, 150) if light else (12, 10, 14, 120)
    ImageDraw.Draw(ov).rounded_rectangle([x0, y0, x1, y1], radius=28, fill=fill)
    img.paste(ov, (0, 0), ov)


# ===========================================================================
# Пресеты
# ===========================================================================
PRESETS = {
    "cream": dict(label="Кремовая классика", background={"type": "solid", "colors": ["#F4EDE4"]},
                  title_font="playfair_bold", accent_font="playfair_italic", body_font="montserrat", number_font="playfair_bold",
                  ink="#3A1F2D", accent="#B5714E", muted="#8A7266", align="left",
                  cover_background={"type": "solid", "colors": ["#3A1F2D"]}, cover_ink="#F4EDE4", cover_accent="#E2B48C",
                  end_background={"type": "solid", "colors": ["#B5714E"]}, end_ink="#FFF8F1", end_accent="#3A1F2D", end_muted="#FFF8F1"),
    "dark": dict(label="Тёмная", background={"type": "solid", "colors": ["#1E1A1D"]}, title_font="playfair_bold",
                 accent_font="playfair_italic", body_font="montserrat", number_font="playfair_bold",
                 ink="#F2E9E1", accent="#E2B48C", muted="#9C8E86", effects=["grain"]),
    "rose": dict(label="Пудровая", background={"type": "solid", "colors": ["#F7E8E6"]}, title_font="playfair_bold",
                 accent_font="playfair_italic", body_font="montserrat", number_font="playfair_bold",
                 ink="#4A2330", accent="#C4566E", muted="#9B7A7F",
                 cover_background={"type": "solid", "colors": ["#C4566E"]}, cover_ink="#FFF5F3", cover_accent="#4A2330", cover_muted="#FFE3E6"),
    "turquoise": dict(label="Бирюза Мальдив", background={"type": "water", "colors": ["#0B4F5C", "#11808A", "#2BB3B1"]},
                      effects=["grain", "vignette"], title_font="forum_caps", accent_font="cormorant_italic",
                      body_font="manrope", number_font="cormorant_italic", ink="#FFFFFF", accent="#F6E7C8", muted="#D9F3EF",
                      shadow="#03262C", signature=True, accent_scale=1.25),
    "sunset": dict(label="Закат на ретрите", background={"type": "sunset", "colors": ["#2A2050", "#6E3A78", "#B4527E", "#E57A66", "#F6B278"]},
                   effects=["grain", "palms"], title_font="playfair", accent_font="playfair_italic", body_font="manrope",
                   number_font="playfair_italic", ink="#FFF6EA", accent="#FFE3A8", muted="#FFEBD6", foot_color="#FFF1E2",
                   shadow="#3A1630", align="center", signature=True),
    "emerald": dict(label="Old money: изумруд и золото", background={"type": "mesh", "colors": ["#0F2E26", "#1C4A3C", "#0A211B"]},
                    effects=["grain", "linen", "vignette"], ornaments=["gold_frame", "monogram", "divider"],
                    title_font="cormorant", accent_font="cormorant_italic", body_font="cormorant", number_font="cormorant_italic",
                    ink="#F3EBDD", accent="gold", muted="#CDBF9C", align="center", signature=True),
    "ivory": dict(label="Old money: слоновая кость", background={"type": "paper", "colors": ["#F3ECDF", "#FBF7EF"]},
                  effects=["grain"], ornaments=["frame", "monogram", "divider", "wax_seal"], ornament_color="#2B3A55",
                  title_font="cormorant", accent_font="cormorant_italic", body_font="cormorant", number_font="cormorant_italic",
                  ink="#1F2A40", accent="#8E2C34", muted="#6A6A72", align="center", signature=True),
    "lace": dict(label="Чёрное кружево", background={"type": "lace", "colors": ["#0B0A0B", "#1C1719", "#E9DCC8"]},
                 effects=["grain", "vignette"], ornaments=["lines"], title_font="tenor_caps", accent_font="playfair_italic",
                 body_font="manrope", number_font="playfair_italic", ink="#F4EEE6", accent="gold", muted="#BFAF92",
                 signature=True, accent_scale=1.3),
    "candle": dict(label="Свеча · личное ведение", background={"type": "candle", "colors": ["#140C09", "#7A4320", "#3A1E12"]},
                   effects=["grain", "vignette"], title_font="tenor_caps", accent_font="playfair_italic", body_font="manrope",
                   number_font="playfair_italic", ink="#F9DDE4", accent="#FFC9D6", muted="#E7C9B8", glow="#FF6F9A",
                   accent_glow="#FF6F9A", signature=True, accent_scale=1.3),
    "linen": dict(label="Белый лён и тень пальмы", background={"type": "linen", "colors": ["#F2EBE1", "#FBF8F2"]},
                  effects=["grain", "palm_shadow"], title_font="cormorant_light", accent_font="cormorant_italic",
                  body_font="manrope", number_font="cormorant_italic", ink="#3B2F26", accent="#A0673F", muted="#8A7A68",
                  signature=True),
    "silk": dict(label="Шампанский шёлк", background={"type": "silk", "colors": ["#B89468", "#F7E6C8"]},
                 effects=["grain", "vignette"], ornaments=["card", "divider"], title_font="playfair", accent_font="script",
                 body_font="manrope", number_font="playfair_italic", ink="#3E2A1A", accent="#9A5B2E", muted="#6E5440",
                 align="center", signature=True),
    "pearl": dict(label="Розовый жемчуг", background={"type": "mesh", "colors": ["#F4D9D6", "#FBEDEA", "#E9B9B6"]},
                  effects=["grain", "pearls"], title_font="playfair_bold", accent_font="playfair_italic", body_font="manrope",
                  number_font="playfair_italic", ink="#4A1F2E", accent="#C2416A", muted="#8C5A66", signature=True),
    "night": dict(label="Ночь достатка", background={"type": "stars", "colors": ["#070B1E", "#16224A"]},
                  effects=["grain", "vignette"], ornaments=["divider"], title_font="cormorant_caps", accent_font="script",
                  body_font="manrope", number_font="cormorant_italic", ink="gold", accent="#FFF3D6", muted="#C9B98E",
                  body_color="#D8D2C2", accent_glow="#FFE6A8", align="center", signature=True),
}
DEFAULT_PRESET = "cream"
COLOR_WORDS = {"gold": "@gold", "золото": "@gold", "rose_gold": "@rose_gold", "pearl": "@pearl", "silver": "@pearl"}
_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$|^#?[0-9a-fA-F]{3}$")


def _color(v, fallback):
    if v is None:
        return fallback
    s = str(v).strip()
    if s.lower() in COLOR_WORDS:
        return COLOR_WORDS[s.lower()]
    if s.startswith("@"):
        return s
    if _HEX_RE.match(s):
        return "#" + s.lstrip("#")
    return fallback


def resolve(design: dict | None, saved: dict | None = None) -> dict:
    """Пресет + переопределения → полный, проверенный дизайн."""
    design = dict(design or {})
    if design.get("saved") and saved:
        base_saved = saved.get(" ".join(str(design["saved"]).strip().strip("«»\"'.!").lower().split())[:40])
        if isinstance(base_saved, dict):
            extra = {k: v for k, v in design.items() if k != "saved"}
            design = {**base_saved, **extra}
    name = str(design.get("preset") or DEFAULT_PRESET).lower()
    d = json.loads(json.dumps(PRESETS.get(name, PRESETS[DEFAULT_PRESET])))
    d["preset"] = name if name in PRESETS else DEFAULT_PRESET
    for k, v in design.items():
        if k in ("preset", "saved") or v is None:
            continue
        if k == "background" and isinstance(v, dict):
            d["background"] = {**d.get("background", {}), **v}
            if "colors" not in v and v.get("type") != PRESETS.get(name, {}).get("background", {}).get("type"):
                d["background"].setdefault("colors", d["background"].get("colors"))
            # поменяли фон — особые цвета/фоны обложки и финала пресета больше не подходят
            for kk in [x for x in d if x.startswith(("cover_", "end_"))]:
                if kk not in design:
                    d.pop(kk)
        else:
            d[k] = v
    for k in ("ink", "accent", "muted", "body_color", "cover_ink", "cover_accent", "cover_muted", "end_ink",
              "end_accent", "end_muted", "foot_color", "ornament_color", "glow", "accent_glow", "shadow"):
        if k in d:
            fb = PRESETS[DEFAULT_PRESET].get(k) if k in ("ink", "accent", "muted") else None
            d[k] = _color(d[k], fb)
            if d[k] is None:
                d.pop(k)
    bgc = d.get("background", {}).get("colors") or ["#F4EDE4"]
    d["background"]["colors"] = [c for c in (_color(c, None) for c in bgc) if c and not c.startswith("@")] or ["#F4EDE4"]
    for k in ("title_font", "accent_font", "number_font"):
        if d.get(k) not in FACES:
            d[k] = PRESETS[DEFAULT_PRESET][k] if k != "accent_font" else "playfair_italic"
    if d.get("body_font") not in BODY_FACES:
        d["body_font"] = "manrope"
    d["align"] = "center" if d.get("align") == "center" else "left"
    d["effects"] = [e for e in (d.get("effects") or []) if isinstance(e, str)]
    d["ornaments"] = [o for o in (d.get("ornaments") or []) if isinstance(o, str)]
    d["_gold_orn"] = "gold_frame" in d["ornaments"] or d.get("accent") == "@gold" or d.get("ink") == "@gold"
    d["_light_ink"] = _luma(d["ink"]) >= 128
    return d


def public(d: dict) -> dict:
    """Дизайн без служебных полей — для сохранения и показа."""
    return {k: v for k, v in d.items() if not k.startswith("_") and k != "label"}


# ===========================================================================
# Отрисовка слайда
# ===========================================================================
def _role_colors(d, role):
    def g(k):
        return d.get(f"{role}_{k}") or d.get(k)
    return g("ink"), g("accent"), g("muted")


def render_slide(slide: dict, idx: int, total: int, d: dict, photo=None, ai_bg=None) -> bytes:
    role = "cover" if idx == 1 else ("end" if idx == total and total > 2 else "mid")
    bd = d
    if role in ("cover", "end") and d.get(f"{role}_background"):
        bd = {**d, "background": d[f"{role}_background"]}
    img = build_background(bd, role, idx, photo=photo if role == "cover" else None, ai_bg=ai_bg)
    ink, acc, muted = _role_colors(d, role)
    photo_cover = role == "cover" and photo is not None
    if photo_cover:
        # на фото текст всегда светлый поверх тёмного градиента — так читается на любом снимке
        ink, muted = "#FFFFFF", "#EDE3D8"
        if _luma(acc) < 150:
            acc = "#F3D9B0"
    body_col = d.get("body_color") if role == "mid" else None
    body_col = body_col or (muted if not muted.startswith("@") else "#E8D9B5")
    al = d["align"]
    orn = d["ornaments"]
    framed = "frame" in orn or "gold_frame" in orn
    P = 130 if (al == "center" or framed) else 96
    iw = W - 2 * P
    fn, fa = FACES[d["title_font"]], FACES[d["accent_font"]]
    if d.get("accent_scale") and fa.scale < d["accent_scale"]:
        fa = Face(fa.file, fa.wght, float(d["accent_scale"]), fa.upper, fa.track)
    bf = BODY_FACES[d["body_font"]]
    lead = 1.12 if fn.upper else 1.08
    busy = d["background"].get("type") in ("ai", "water", "sunset", "photo") and d["_light_ink"]
    shadow = (d["shadow"], 14, 0.55) if d.get("shadow") else (("#000000", 16, 0.55) if photo_cover or busy else None)
    glow, ag = d.get("glow"), d.get("accent_glow")
    small = _font(*SMALL_FONT)

    if "lines" in orn:
        _lines(img, d)
    if "frame" in orn or "gold_frame" in orn:
        _frame(img, d, gold="gold_frame" in orn)

    title, text = slide.get("title", ""), slide.get("text", "")
    top_lim = 200 if (framed or "lines" in orn) else 150
    bot_lim = H - (200 if (framed or "lines" in orn) else 170)
    if role == "cover" and "monogram" in orn:
        top_lim = 300
    if photo_cover:
        top_lim = int(H * 0.42)

    # Подбираем масштаб так, чтобы весь блок поместился
    base_sizes = {"cover": (140, 40), "mid": (100, 40), "end": (100, 38)}[role]
    if fn.upper:
        base_sizes = (int(base_sizes[0] * 0.86), base_sizes[1])
    for scale in [1.0, 0.92, 0.85, 0.78, 0.72, 0.66, 0.6]:
        tsize = int(base_sizes[0] * scale)
        bsize = max(26, int(base_sizes[1] * min(1.0, scale + 0.1)))
        parts = []  # (kind, height, payload)
        if role == "cover" and slide.get("kicker"):
            parts.append(("kicker", 46, slide["kicker"]))
        if role == "mid":
            nface = FACES[d["number_font"]]
            nsize = int(200 * scale) if not nface.upper else int(150 * scale)
            nf = nface.at(nsize)
            num = f"{idx - 1:02d}"
            bb = nf.getbbox(num, anchor="la")
            parts.append(("num", bb[3] + 30, (num, nf)))
        if title:
            ts, tl = _fit(title, fn, fa, iw, 640 * scale if role == "cover" else 420 * scale, tsize, 40, lead)
            parts.append(("title", int(len(tl) * ts * lead), (ts, tl)))
        if text:
            if role == "cover" and "divider" in orn:
                parts.append(("divider", 74, None))
            bs, bl = _fit(text, bf, bf, iw - (60 if al == "center" else 0), 420 * scale, bsize, 24, 1.45)
            parts.append(("gap", 0 if (role == "cover" and "divider" in orn) else 40, None))
            parts.append(("body", int(len(bl) * bs * 1.45), (bs, bl)))
        if role == "end" and d.get("signature"):
            parts.append(("sig", 150, None))
        total_h = sum(p[1] for p in parts)
        if total_h <= bot_lim - top_lim:
            break

    if photo_cover:
        y = bot_lim - total_h
    else:
        y = top_lim + (bot_lim - top_lim - total_h) // 2 - (20 if role != "cover" else 0)
        y = max(top_lim, y)

    if "card" in orn and not photo_cover:
        _card(img, (70, y - 70, W - 70, y + total_h + 60), d)
    if role == "cover" and "monogram" in orn and not photo_cover:
        _monogram(img, W // 2, min(200, y - 90), 56, d)

    cx = W // 2
    for kind, h, pl in parts:
        if kind == "kicker":
            _simple(img, (cx if al == "center" else P, y + 26), pl.upper(), small, d.get("kicker_color") and _color(d["kicker_color"], muted) or muted,
                    anchor="ms" if al == "center" else "ls", track=5)
        elif kind == "num":
            num, nf = pl
            col = d.get("number_color") and _color(d["number_color"], acc) or acc
            _simple(img, (cx if al == "center" else P - 6, y), num, nf, col, anchor="ma" if al == "center" else "la")
        elif kind == "title":
            ts, tl = pl
            _draw_block(img, tl, fn, fa, P, y, iw, ts, lead, al, ink, acc, shadow, glow, ag)
        elif kind == "divider":
            _divider(img, cx if al == "center" else P + 60, y + 40, d)
        elif kind == "body":
            bs, bl = pl
            off = 30 if al == "center" else 0
            _draw_block(img, bl, bf, bf, P + off, y, iw - 2 * off, bs, 1.45, al, body_col, body_col, shadow)
        elif kind == "sig":
            sf = _font("GreatVibes-Regular.ttf", 104)
            _simple(img, (cx if al == "center" else P, y + 20), SIGNATURE, sf, acc, anchor="ma" if al == "center" else "la")
        y += h

    # футер
    fc = d.get("foot_color") or muted
    if photo_cover:
        fc = muted
    fy = H - (96 if (framed or "lines" in orn) else 80)
    if shadow:
        sm = Image.new("L", (W, H), 0)
        ImageDraw.Draw(sm).rectangle([P - 10, fy - 34, W - P + 10, fy + 10], fill=90)
        img.paste(Image.new("RGB", (W, H), shadow[0]), (0, 0), sm.filter(ImageFilter.GaussianBlur(22)))
    right = "листай" if role == "cover" else f"{idx:02d} / {total:02d}"
    arrow = role == "cover"
    ar = _font("Manrope-VF.ttf", 26, 500)
    if al == "center":
        s = f"{HANDLE}   ·   {right}".upper()
        tw = sum(small.getlength(c) for c in s) + 3 * (len(s) - 1)
        _simple(img, (W // 2 - (18 if arrow else 0), fy), s, small, fc, anchor="ms", track=3)
        if arrow:
            _simple(img, (W // 2 - 18 + tw / 2 + 12, fy + 1), "→", ar, fc, anchor="ls")
    else:
        _simple(img, (P, fy), HANDLE.upper(), small, fc, anchor="ls", track=3)
        _simple(img, (W - P - (40 if arrow else 0), fy), right.upper(), small, fc, anchor="rs", track=3)
        if arrow:
            _simple(img, (W - P, fy + 1), "→", ar, fc, anchor="rs")

    if "pearls" in d["effects"]:
        if role == "cover":
            _pearls(img, [(W - 130, 130, 46), (W - 220, 90, 30), (W - 70, 230, 34), (W - 190, 200, 22), (W - 300, 60, 18)])
        elif role == "end":
            _pearls(img, [(W - 140, H - 230, 44), (W - 230, H - 170, 28), (W - 70, H - 310, 26)])
        else:
            _pearls(img, [(W - 110, 120, 34), (W - 180, 70, 20)])
    if role == "end" and "wax_seal" in orn:
        _wax_seal(img, W - 220, H - 300, 80)

    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=6)
    return buf.getvalue()


def render(data: dict, design: dict | None = None, photo: bytes | None = None, ai_bg: bytes | None = None) -> list[bytes]:
    d = design if (design and "_light_ink" in design) else resolve(design or data.get("design"))
    ph = Image.open(io.BytesIO(photo)) if photo else None
    ai = Image.open(io.BytesIO(ai_bg)) if ai_bg else None
    total = len(data["slides"])
    return [render_slide(s, i, total, d, ph, ai) for i, s in enumerate(data["slides"], 1)]


SAMPLE = {"slides": [
    {"kicker": "Деньги и женственность", "title": "Почему умные женщины зарабатывают **меньше,** чем могут",
     "text": "Не потому что мало знают. И не потому что мало работают."},
    {"title": "Потолок не в рынке — он **внутри**", "text": "Можно повысить цену прямо сейчас."},
    {"title": "Сохрани и **перешли** подруге", "text": ""}]}


def preview_cover(preset: str) -> bytes:
    d = resolve({"preset": preset})
    return render_slide(SAMPLE["slides"][0], 1, 3, d)
