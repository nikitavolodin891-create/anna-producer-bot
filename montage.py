"""Автомонтаж Reels.

Два движка:
1. Submagic (если задан SUBMAGIC_API_KEY) — профессиональный монтаж: субтитры в стиле
   топовых рилсов, magic zooms, AI-перебивки, вырезка пауз и неудачных дублей, хук-заголовок.
2. Свой монтаж на ffmpeg (запасной, бесплатный) — вырезка пауз по словам, формат 9:16,
   наезды на чередующихся фразах, крупные субтитры с подсветкой текущего слова, хук-заголовок.

Если Submagic не настроен, не оплачен или упал — автоматически монтируем своим движком.
"""
import os
import re
import json
import asyncio
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

SUBMAGIC_API_KEY = os.environ.get("SUBMAGIC_API_KEY")
SUBMAGIC_BASE = "https://api.submagic.co/v1"
SUBMAGIC_TEMPLATE = os.environ.get("SUBMAGIC_TEMPLATE")  # пусто = шаблон по умолчанию в Submagic
SUBMAGIC_POLL_SECONDS = 20  # лимит API — 100 проверок статуса в час
SUBMAGIC_MAX_WAIT_SECONDS = 20 * 60

_HERE = os.path.dirname(os.path.abspath(__file__))
# в контейнере шрифт лежит в /app/fonts (см. Dockerfile), локально — рядом с кодом
FONTS_DIR = os.path.join(_HERE, "fonts") if os.path.isdir(os.path.join(_HERE, "fonts")) else _HERE
FONT_NAME = "Montserrat ExtraBold"

OUT_W, OUT_H = 1080, 1920
MAX_PAUSE = 0.45      # паузы длиннее — вырезаем
CUT_PAD = 0.12        # запас вокруг слов, чтобы не обрезать звук
ZOOM = 1.12           # наезд на чередующихся фразах
WORDS_PER_CAPTION = 3
FFMPEG_TIMEOUT = 20 * 60
# потоки ffmpeg: меньше потоков — меньше памяти (на пробном тарифе Railway её мало)
FF_THREADS = os.environ.get("FFMPEG_THREADS", "2")


class MontageError(Exception):
    """Понятная пользователю ошибка."""


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Plan:
    """Продюсерский план монтажа (его составляет Claude по расшифровке)."""
    hook_title: str = ""
    pace: str = "fast"          # natural | fast | extra-fast
    zooms: bool = True
    brolls: bool = False
    captions: bool = True
    post_caption: str = ""
    comment: str = ""
    extra: dict = field(default_factory=dict)


def parse_plan(text: str) -> Plan:
    """Достаёт JSON-план из ответа Claude (терпимо к мусору вокруг)."""
    m = re.search(r"<plan>(.*?)</plan>", text, re.DOTALL) or re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return Plan()
    raw = m.group(1) if m.lastindex else m.group(0)
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Не удалось разобрать план монтажа: %s", raw[:500])
        return Plan()
    pace = str(data.get("pace", "fast"))
    return Plan(
        hook_title=str(data.get("hook_title") or "")[:90],
        pace=pace if pace in ("natural", "fast", "extra-fast") else "fast",
        zooms=bool(data.get("zooms", True)),
        brolls=bool(data.get("brolls", False)),
        captions=bool(data.get("captions", True)),
        post_caption=str(data.get("post_caption") or ""),
        comment=str(data.get("comment") or ""),
        extra=data,
    )


# ---------------------------------------------------------------------------
# Вспомогательное: ffprobe / запуск процессов
# ---------------------------------------------------------------------------
async def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "timeout"
    if proc.returncode != 0:
        logger.error("%s failed: %s", cmd[0], err.decode(errors="ignore")[-3000:])
    return proc.returncode, out.decode(errors="ignore")


async def probe(path: str) -> dict:
    code, out = await _run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height:stream_tags=rotate:stream_side_data=rotation",
        "-of", "json", path,
    ], timeout=60)
    if code != 0:
        raise MontageError("не получилось прочитать видео — возможно, файл повреждён")
    data = json.loads(out or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise MontageError("в файле нет видеодорожки")
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "width": v.get("width"),
        "height": v.get("height"),
    }


# ---------------------------------------------------------------------------
# Submagic
# ---------------------------------------------------------------------------
def submagic_enabled() -> bool:
    return bool(SUBMAGIC_API_KEY)


def _submagic_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("message") or body.get("error") or str(body)
    except Exception:
        msg = resp.text[:300]
    if resp.status_code in (401, 403):
        return f"Submagic не принял ключ или тариф не включает API ({msg})"
    if resp.status_code == 402:
        return "на Submagic закончились минуты/оплата"
    if resp.status_code == 429:
        return "Submagic просит подождать (лимит запросов)"
    return f"Submagic ответил ошибкой {resp.status_code}: {msg}"


async def submagic_edit(src_path: str, language: str, plan: Plan, title: str, on_progress=None) -> str:
    """Загружает видео в Submagic, ждёт монтажа и возвращает ссылку на готовое видео."""
    headers = {"x-api-key": SUBMAGIC_API_KEY}
    form: dict[str, str] = {
        "title": (title or "Reels")[:100],
        "language": language,
        "magicZooms": "true" if plan.zooms else "false",
        "magicBrolls": "true" if plan.brolls else "false",
        "removeSilencePace": plan.pace,
        "removeBadTakes": "true",
        "cleanAudio": "true",
        "disableCaptions": "false" if plan.captions else "true",
    }
    if plan.brolls:
        form["magicBrollsPercentage"] = "30"
    if SUBMAGIC_TEMPLATE:
        form["templateName"] = SUBMAGIC_TEMPLATE
    if plan.hook_title:
        form["hookTitle"] = json.dumps({"text": plan.hook_title[:100]}, ensure_ascii=False)

    timeout = httpx.Timeout(connect=30, read=600, write=600, pool=30)
    async with httpx.AsyncClient(timeout=timeout) as http:
        with open(src_path, "rb") as f:
            resp = await http.post(
                f"{SUBMAGIC_BASE}/projects/upload",
                headers=headers,
                data=form,
                files={"file": (os.path.basename(src_path), f, "video/mp4")},
            )
        if resp.status_code == 400 and "hookTitle" in form:
            # например, неподходящий шаблон заголовка — пробуем без хук-заголовка
            logger.warning("Submagic 400 с hookTitle: %s — повтор без него", resp.text[:300])
            form.pop("hookTitle")
            with open(src_path, "rb") as f:
                resp = await http.post(
                    f"{SUBMAGIC_BASE}/projects/upload", headers=headers, data=form,
                    files={"file": (os.path.basename(src_path), f, "video/mp4")},
                )
        if resp.status_code not in (200, 201):
            raise MontageError(_submagic_error(resp))
        project_id = resp.json().get("id")
        if not project_id:
            raise MontageError("Submagic не вернул id проекта")
        logger.info("Submagic project %s created", project_id)

        waited = 0
        last_status = None
        while waited < SUBMAGIC_MAX_WAIT_SECONDS:
            await asyncio.sleep(SUBMAGIC_POLL_SECONDS)
            waited += SUBMAGIC_POLL_SECONDS
            r = await http.get(f"{SUBMAGIC_BASE}/projects/{project_id}", headers=headers)
            if r.status_code == 429:
                continue
            if r.status_code != 200:
                raise MontageError(_submagic_error(r))
            data = r.json()
            status = data.get("status")
            if status != last_status and on_progress:
                await on_progress(status)
            last_status = status
            if status == "completed":
                url = data.get("downloadUrl") or data.get("directUrl")
                if url:
                    return url
                # проект готов, но ещё рендерится — ждём ссылку
                continue
            if status == "failed":
                raise MontageError(f"Submagic не смог смонтировать: {data.get('failureReason') or 'без причины'}")
        raise MontageError("Submagic монтирует слишком долго (больше 20 минут)")


async def download_file(url: str, dst: str) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30), follow_redirects=True) as http:
        async with http.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dst, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)


# ---------------------------------------------------------------------------
# Свой монтаж (ffmpeg)
# ---------------------------------------------------------------------------
def keep_segments(words: list[Word], duration: float) -> list[tuple[float, float]]:
    """Фрагменты, которые оставляем: речь без длинных пауз."""
    if not words:
        return [(0.0, duration)]
    segs: list[list[float]] = []
    for w in sorted(words, key=lambda x: x.start):
        start = max(0.0, w.start - CUT_PAD)
        end = min(duration, w.end + CUT_PAD)
        if segs and w.start - (segs[-1][1] - CUT_PAD) <= MAX_PAUSE:
            segs[-1][1] = max(segs[-1][1], end)
        else:
            if segs and start < segs[-1][1]:
                start = segs[-1][1]
            segs.append([start, end])
    return [(round(s, 3), round(e, 3)) for s, e in segs if e - s >= 0.15]


def retime_words(words: list[Word], segs: list[tuple[float, float]], durations: list[float] | None = None) -> list[Word]:
    """Пересчитывает время слов под смонтированное видео (после вырезки пауз).
    durations — реальные длительности нарезанных кусков (могут отличаться на доли кадра)."""
    out: list[Word] = []
    offset = 0.0
    for idx, (s, e) in enumerate(segs):
        for w in words:
            mid = (w.start + w.end) / 2
            if s <= mid <= e:
                ns = offset + max(w.start, s) - s
                ne = offset + min(w.end, e) - s
                out.append(Word(w.text, round(ns, 3), round(max(ne, ns + 0.08), 3)))
        offset += durations[idx] if durations and idx < len(durations) and durations[idx] > 0 else e - s
    return out


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_clean(text: str) -> str:
    return re.sub(r"[{}\\]", "", text).replace("\n", " ").strip()


def _wrap_hook(text: str, max_len: int = 20) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_len:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return r"\N".join(lines[:4])


def build_ass(words: list[Word], total: float, hook_title: str, captions: bool) -> str:
    """Субтитры в стиле рилсов: крупно, по 2-3 слова, текущее слово подсвечено."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT_NAME},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,7,3,2,80,80,560,1
Style: Hook,{FONT_NAME},78,&H00111111,&H00111111,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,3,18,0,8,90,90,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    if hook_title:
        end = min(3.5, total)
        events.append(
            f"Dialogue: 1,{_ass_time(0)},{_ass_time(end)},Hook,,0,0,0,,"
            r"{\fad(150,250)}" + _wrap_hook(_ass_clean(hook_title).upper())
        )
    if captions and words:
        # группируем слова в подписи по 2-3 слова (новая группа и после паузы)
        groups: list[list[Word]] = []
        for w in words:
            if (
                not groups
                or len(groups[-1]) >= WORDS_PER_CAPTION
                or w.start - groups[-1][-1].end > 0.35
                or len(" ".join(x.text for x in groups[-1])) + len(w.text) > 18
            ):
                groups.append([w])
            else:
                groups[-1].append(w)
        highlight = r"{\c&H0AD6FF&\fscx108\fscy108}"  # тёплый золотой, чуть крупнее
        normal = r"{\c&HFFFFFF&\fscx100\fscy100}"
        for gi, g in enumerate(groups):
            g_end = g[-1].end
            next_start = groups[gi + 1][0].start if gi + 1 < len(groups) else total
            g_end = min(max(g_end, g[-1].start + 0.3), next_start)
            for i, w in enumerate(g):
                start = w.start
                end = g[i + 1].start if i + 1 < len(g) else g_end
                if end <= start:
                    continue
                parts = []
                for j, x in enumerate(g):
                    t = _ass_clean(x.text).upper()
                    parts.append(f"{highlight}{t}{normal}" if j == i else t)
                events.append(
                    f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,," + " ".join(parts)
                )
    return header + "\n".join(events) + "\n"


async def _duration(path: str) -> float:
    code, out = await _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], timeout=60)
    try:
        return float(out.strip()) if code == 0 else 0.0
    except ValueError:
        return 0.0


async def ffmpeg_edit(src_path: str, dst_path: str, words: list[Word], plan: Plan, work_dir: str) -> dict:
    """Свой монтаж: паузы → 9:16 → наезды → субтитры → хук-заголовок.

    Экономно по памяти (сервер маленький): каждый фрагмент режется отдельным процессом,
    затем склейка без перекодирования и финальный проход с субтитрами. Память не растёт
    с длиной видео.
    """
    info = await probe(src_path)
    duration = info["duration"]
    if duration <= 0:
        raise MontageError("не получилось определить длительность видео")

    segs = keep_segments(words, duration) if info["has_audio"] else [(0.0, duration)]
    base = f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H}"
    zw, zh = int(OUT_W * ZOOM) // 2 * 2, int(OUT_H * ZOOM) // 2 * 2
    zoomed = f"scale={zw}:{zh}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H}"

    # 1. Нарезка фрагментов (по одному процессу на фрагмент)
    seg_files: list[str] = []
    durations: list[float] = []
    for i, (s, e) in enumerate(segs):
        out = os.path.join(work_dir, f"seg_{i:04d}.mp4")
        vf = (zoomed if (plan.zooms and i % 2 == 1) else base) + ",setsar=1,fps=30"
        cmd = ["ffmpeg", "-y", "-threads", FF_THREADS, "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", src_path,
               "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
               "-threads", FF_THREADS, "-x264-params", "rc-lookahead=10"]
        if info["has_audio"]:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        else:
            cmd += ["-an"]
        cmd.append(out)
        code, _ = await _run(cmd, timeout=600)
        if code != 0 or not os.path.exists(out):
            raise MontageError("монтаж не получился (ошибка при нарезке)" if code != -1 else "монтаж занял слишком много времени")
        seg_files.append(out)
        durations.append(await _duration(out))

    # 2. Склейка без перекодирования
    list_path = os.path.join(work_dir, "list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        f.writelines(f"file '{p}'\n" for p in seg_files)
    joined = os.path.join(work_dir, "joined.mp4")
    code, _ = await _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", joined], timeout=600)
    if code != 0 or not os.path.exists(joined):
        raise MontageError("монтаж не получился (ошибка при склейке)")
    for p in seg_files:
        try:
            os.remove(p)
        except OSError:
            pass

    # 3. Субтитры и хук-заголовок — по реальным длительностям кусков
    total = await _duration(joined) or sum(durations)
    new_words = retime_words(words, segs, durations)
    ass_path = os.path.join(work_dir, "subs.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(build_ass(new_words, total, plan.hook_title, plan.captions))

    ass_escaped = ass_path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    fonts_escaped = FONTS_DIR.replace(":", r"\:").replace("'", r"\'")
    cmd = ["ffmpeg", "-y", "-threads", FF_THREADS, "-i", joined,
           "-vf", f"subtitles='{ass_escaped}':fontsdir='{fonts_escaped}'",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-threads", FF_THREADS, "-x264-params", "rc-lookahead=10", "-movflags", "+faststart"]
    cmd += ["-c:a", "aac", "-b:a", "160k"] if info["has_audio"] else ["-an"]
    cmd.append(dst_path)
    code, _ = await _run(cmd, timeout=FFMPEG_TIMEOUT)
    if code != 0 or not os.path.exists(dst_path):
        raise MontageError("монтаж не получился (ошибка при наложении субтитров)" if code != -1 else "монтаж занял слишком много времени")
    return {"original": duration, "final": total, "cuts": max(len(segs) - 1, 0)}
