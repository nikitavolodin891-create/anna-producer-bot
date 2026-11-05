"""Автомонтаж рилсов: склейка нескольких видео, вырезание пауз и оговорок, субтитры, хук, музыка.

Как работает:
1. По каждому клипу достаём звук и распознаём речь Whisper'ом С ТАЙМКОДАМИ КАЖДОГО СЛОВА.
2. Claude («режиссёр монтажа») по расшифровке выбирает лучшие куски и дубли, порядок, хук и подпись.
3. Код сам подрезает куски по границам слов и выкидывает паузы/«эээ» внутри.
4. ffmpeg режет куски в вертикаль 1080×1920, склеивает, вжигает субтитры и хук (libass), подмешивает музыку.

Память: ffmpeg работает потоково, с -threads 2 — даже 4K HEVC с iPhone укладывается в ~1 ГБ.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

FONTS_DIR = os.environ.get("FONTS_DIR", "/app/fonts")
OUT_W, OUT_H, FPS = 1080, 1920, 30
MAX_OUTPUT_SECONDS = float(os.environ.get("MONTAGE_MAX_SECONDS", "90"))
MAX_PIECES = 80
PAUSE_GAP = 0.45      # пауза между словами длиннее — вырезаем
PAD_BEFORE = 0.10     # запас перед первым словом куска
PAD_AFTER = 0.18      # запас после последнего слова
MERGE_GAP = 0.25      # соседние куски ближе — склеиваем в один
FILLER_RE = re.compile(r"^(э+|э+м+|м+|а+|эээ+|ммм+|хм+|угу)$", re.I)
FF_THREADS = os.environ.get("FFMPEG_THREADS", "4")


class MontageError(Exception):
    pass


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Clip:
    path: str
    name: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = True
    words: list[Word] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)  # [{start,end,text}]


@dataclass
class Piece:
    clip: int          # индекс клипа (с 0)
    start: float
    end: float
    words: list[Word] = field(default_factory=list)

    @property
    def dur(self) -> float:
        return max(0.0, self.end - self.start)


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------
async def _run(*args: str, timeout: float = 1800) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise MontageError("ffmpeg работал слишком долго")
    text = (out or b"").decode(errors="ignore") + (err or b"").decode(errors="ignore")
    return proc.returncode, text


async def probe(path: str) -> dict:
    code, out = await _run(
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height:stream_side_data=rotation:stream_tags=rotate",
        "-of", "json", path, timeout=60,
    )
    if code != 0:
        raise MontageError("не получилось прочитать видео")
    try:
        info = json.loads(out[out.index("{"):])
    except (ValueError, json.JSONDecodeError):
        raise MontageError("не получилось прочитать видео")
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = rot or int(float((v.get("tags") or {}).get("rotate", 0) or 0))
    if abs(rot) % 180 == 90:
        w, h = h, w
    return {
        "duration": float((info.get("format") or {}).get("duration") or 0),
        "width": w, "height": h,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "has_video": bool(v),
    }


async def extract_audio(path: str, dst: str) -> bool:
    code, out = await _run(
        "ffmpeg", "-y", "-threads", FF_THREADS, "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", dst,
        timeout=900,
    )
    return code == 0 and os.path.exists(dst) and os.path.getsize(dst) > 1000


async def transcribe_words(openai_client, mp3_path: str) -> tuple[list[Word], list[dict]]:
    """Whisper с таймкодами слов и фраз."""
    with open(mp3_path, "rb") as f:
        resp = await openai_client.audio.transcriptions.create(
            model="whisper-1", file=f, response_format="verbose_json",
            timestamp_granularities=["word", "segment"], language="ru",
        )
    def g(o, k, d=None):
        return o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)
    words = [Word(float(g(w, "start")), float(g(w, "end")), str(g(w, "word")).strip()) for w in (g(resp, "words") or [])]
    segs = [{"start": float(g(s, "start")), "end": float(g(s, "end")), "text": str(g(s, "text")).strip()}
            for s in (g(resp, "segments") or [])]
    words = [w for w in words if w.text and w.end > w.start - 0.01]
    return words, segs


# ---------------------------------------------------------------------------
# План монтажа
# ---------------------------------------------------------------------------
PLANNER_SYSTEM = """Ты — режиссёр монтажа Reels для эксперта Анны (женская аудитория, отношения, деньги,
энергия, саморазвитие). Тебе дают расшифровки исходных клипов с таймкодами фраз. Твоя задача —
собрать из них ОДИН сильный вертикальный ролик.

Правила:
- Начало ролика = самая цепляющая мысль (хук), даже если в исходнике она звучит в середине.
- Если фраза повторяется (дубль) — бери последний удачный вариант, остальные выкидывай.
- Оговорки, обрывы, «так, ещё раз», технические фразы, тишину и разговоры не по теме — выкидывай.
- Логика и смысл должны сохраниться: ролик должен звучать как цельная речь.
- Длина: по умолчанию 25–60 секунд (если просят иначе — делай как просят). Максимум 90 секунд.
- Таймкоды бери ТОЛЬКО из данных. Можно брать кусок фразы, но границы — по смыслу целых слов.
- Клипы без речи (b-roll) можно вставлять короткими кусками 1.5–4 сек между мыслями или в начало.
- hook — текст на экран в первые секунды: до 6 слов, по-русски, живо, без кавычек и эмодзи.
- caption — подпись к посту в голосе Анны: 2–4 коротких абзаца + призыв (без выдуманных фактов).

Ответ — ТОЛЬКО JSON без пояснений:
{"pieces":[{"clip":1,"from":12.3,"to":25.0}], "hook":"...", "caption":"...", "notes":"1–2 предложения: что сделано"}"""


def planner_input(clips: list[Clip], request: str, previous: dict | None = None) -> str:
    lines = []
    for i, c in enumerate(clips, 1):
        lines.append(f"=== Клип {i}: «{c.name}», {c.duration:.1f} сек ===")
        if not c.segments:
            lines.append("(речи нет — b-roll, можно брать любые отрезки)")
            continue
        for s in c.segments:
            lines.append(f"[{s['start']:.2f}–{s['end']:.2f}] {s['text']}")
    if previous:
        lines.append("\n=== Предыдущая версия монтажа (её просят поправить) ===")
        lines.append(json.dumps({k: previous.get(k) for k in ("pieces", "hook", "caption")}, ensure_ascii=False))
    lines.append(f"\n=== Запрос Анны ===\n{request or 'Смонтируй рилс'}")
    return "\n".join(lines)


def parse_plan(text: str, clips: list[Clip]) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise MontageError("режиссёр не вернул план")
    try:
        raw = json.loads(m.group(0))
    except json.JSONDecodeError:
        raise MontageError("режиссёр вернул кривой план")
    pieces = []
    for p in raw.get("pieces") or []:
        try:
            ci = int(p["clip"]) - 1
            a, b = float(p["from"]), float(p["to"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= ci < len(clips)):
            continue
        dur = clips[ci].duration or b
        a, b = max(0.0, a), min(dur, b)
        if b - a >= 0.4:
            pieces.append({"clip": ci + 1, "from": round(a, 2), "to": round(b, 2)})
    if not pieces:
        raise MontageError("в плане нет ни одного куска")
    return {
        "pieces": pieces,
        "hook": str(raw.get("hook") or "").strip().strip("«»\"")[:60],
        "caption": str(raw.get("caption") or "").strip(),
        "notes": str(raw.get("notes") or "").strip(),
    }


def fallback_plan(clips: list[Clip]) -> dict:
    """Без Claude/речи: каждый клип целиком, подрезанный по речи, в исходном порядке."""
    pieces = []
    for i, c in enumerate(clips, 1):
        if c.words:
            pieces.append({"clip": i, "from": c.words[0].start, "to": c.words[-1].end})
        else:
            pieces.append({"clip": i, "from": 0.0, "to": min(c.duration, 6.0)})
    return {"pieces": pieces, "hook": "", "caption": "", "notes": "Склеила клипы по порядку и убрала паузы."}


def build_pieces(plan: dict, clips: list[Clip], cut_pauses: bool = True) -> list[Piece]:
    """План → точные куски: подрезка по словам, вырезание пауз и «эээ», лимит длины."""
    out: list[Piece] = []
    for p in plan["pieces"]:
        ci = p["clip"] - 1
        c = clips[ci]
        a, b = float(p["from"]), float(p["to"])
        ws = [w for w in c.words if w.end > a + 0.05 and w.start < b - 0.05]
        if not ws:  # b-roll или кусок без слов
            out.append(Piece(ci, a, min(b, c.duration or b), []))
            continue
        groups: list[list[Word]] = [[]]
        for w in ws:
            if cut_pauses and FILLER_RE.match(re.sub(r"[^\wё]", "", w.text.lower())):
                if groups[-1]:
                    groups.append([])
                continue
            if cut_pauses and groups[-1] and w.start - groups[-1][-1].end > PAUSE_GAP:
                groups.append([])
            groups[-1].append(w)
        for g in groups:
            if not g:
                continue
            s = max(0.0, g[0].start - PAD_BEFORE)
            e = min(c.duration or g[-1].end + PAD_AFTER, g[-1].end + PAD_AFTER)
            out.append(Piece(ci, s, e, g))
    # склеиваем соседние куски одного клипа
    merged: list[Piece] = []
    for pc in out:
        if merged and merged[-1].clip == pc.clip and 0 <= pc.start - merged[-1].end <= MERGE_GAP:
            merged[-1].end = pc.end
            merged[-1].words += pc.words
        else:
            merged.append(pc)
    merged = [m for m in merged if m.dur >= 0.3][:MAX_PIECES]
    total, capped = 0.0, []
    for m in merged:
        if total + m.dur > MAX_OUTPUT_SECONDS:
            break
        capped.append(m)
        total += m.dur
    if not capped:
        raise MontageError("после подрезки ничего не осталось")
    return capped


# ---------------------------------------------------------------------------
# Субтитры и хук (ASS)
# ---------------------------------------------------------------------------
def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def subtitle_chunks(pieces: list[Piece], seg_durs: list[float], max_words: int = 3, max_chars: int = 18):
    """Слова → короткие строки по 1–3 слова на итоговой таймлинии."""
    chunks = []
    offset = 0.0
    for pc, d in zip(pieces, seg_durs):
        cur: list[tuple[float, float, str]] = []
        for w in pc.words:
            ws = offset + (w.start - pc.start)
            we = offset + (w.end - pc.start)
            ws, we = max(offset, ws), min(offset + d, we)
            if we <= ws:
                continue
            text = w.text.strip()
            if cur and (len(cur) >= max_words or len(" ".join(x[2] for x in cur)) + len(text) > max_chars
                        or ws - cur[-1][1] > 0.35):
                chunks.append((cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
                cur = []
            cur.append((ws, we, text))
            if re.search(r"[.!?…]$", text):
                chunks.append((cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
                cur = []
        if cur:
            chunks.append((cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
        offset += d
    # держим строку на экране до начала следующей (если зазор маленький) — меньше мигания
    fixed = []
    for i, (s, e, t) in enumerate(chunks):
        nxt = chunks[i + 1][0] if i + 1 < len(chunks) else None
        if nxt is not None and 0 < nxt - e < 0.4:
            e = nxt
        fixed.append((s, max(e, s + 0.25), t))
    return fixed


def build_ass(pieces: list[Piece], seg_durs: list[float], hook: str = "", subtitles: bool = True) -> str:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,Montserrat,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,560,204
Style: Hook,Montserrat,88,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,26,0,8,90,90,300,204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if hook:
        ev.append(f"Dialogue: 1,{_ts(0.05)},{_ts(min(3.2, sum(seg_durs)))},Hook,,0,0,0,,{{\\fad(150,250)}}{_esc(hook)}")
    if subtitles:
        for s, e, t in subtitle_chunks(pieces, seg_durs):
            ev.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Sub,,0,0,0,,{{\\fad(40,0)}}{_esc(t)}")
    return head + "\n".join(ev) + "\n"


# ---------------------------------------------------------------------------
# Рендер
# ---------------------------------------------------------------------------
VF_VERTICAL = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase:flags=bicubic,"
               f"crop={OUT_W}:{OUT_H},setsar=1,fps={FPS},format=yuv420p")


async def render_piece(clip: Clip, pc: Piece, dst: str) -> float:
    d = pc.dur
    fade = min(0.04, d / 4)
    af = f"aresample=48000,aformat=channel_layouts=stereo,afade=t=in:d={fade:.3f},afade=t=out:st={d - fade:.3f}:d={fade:.3f}"
    args = ["ffmpeg", "-y", "-threads", FF_THREADS, "-ss", f"{pc.start:.3f}", "-i", clip.path]
    if not clip.has_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    args += ["-t", f"{d:.3f}", "-map", "0:v:0", "-map", "0:a:0" if clip.has_audio else "1:a:0",
             "-vf", VF_VERTICAL, "-af", af,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-g", str(FPS * 2),
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", dst]
    code, out = await _run(*args)
    if code != 0 or not os.path.exists(dst):
        logger.error("ffmpeg piece failed: %s", out[-1500:])
        raise MontageError("не получилось нарезать кусок видео")
    info = await probe(dst)
    return info["duration"] or d


async def render(clips: list[Clip], pieces: list[Piece], workdir: str, hook: str = "", subtitles: bool = True,
                 music_path: str | None = None, progress=None) -> tuple[str, float]:
    seg_paths, seg_durs = [], []
    for i, pc in enumerate(pieces):
        dst = os.path.join(workdir, f"seg_{i:03d}.mp4")
        seg_durs.append(await render_piece(clips[pc.clip], pc, dst))
        seg_paths.append(dst)
        if progress and (i % 5 == 4 or i == len(pieces) - 1):
            await progress(i + 1, len(pieces))
    lst = os.path.join(workdir, "list.txt")
    with open(lst, "w") as f:
        for p in seg_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    joined = os.path.join(workdir, "joined.mp4")
    code, out = await _run("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined)
    if code != 0:
        logger.error("concat failed: %s", out[-1500:])
        raise MontageError("не получилось склеить куски")
    for p in seg_paths:
        try:
            os.remove(p)
        except OSError:
            pass

    total = sum(seg_durs)
    ass_path = os.path.join(workdir, "subs.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(build_ass(pieces, seg_durs, hook=hook, subtitles=subtitles))

    out_path = os.path.join(workdir, "reel.mp4")
    vf = f"ass={ass_path}:fontsdir={FONTS_DIR}"
    args = ["ffmpeg", "-y", "-threads", FF_THREADS, "-i", joined]
    if music_path:
        args += ["-stream_loop", "-1", "-i", music_path]
        fo = max(0.0, total - 1.5)
        af = (f"[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=0.16,afade=t=out:st={fo:.2f}:d=1.5[m];"
              f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
        args += ["-filter_complex", f"[0:v]{vf}[v];{af}", "-map", "[v]", "-map", "[a]"]
    else:
        args += ["-vf", vf, "-map", "0:v", "-map", "0:a"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "10M", "-bufsize", "20M",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}",
             "-movflags", "+faststart", out_path]
    code, out = await _run(*args)
    if code != 0 or not os.path.exists(out_path):
        logger.error("final render failed: %s", out[-2000:])
        raise MontageError("не получилось собрать итоговое видео")
    try:
        os.remove(joined)
    except OSError:
        pass
    return out_path, total
