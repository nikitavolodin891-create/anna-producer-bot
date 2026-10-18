"""Ссылки на соцсети: Instagram (рилсы, посты, карусели), TikTok, YouTube Shorts.

Что делаем со ссылкой:
1. Достаём данные поста: автор, подпись, просмотры/лайки/комментарии, топ-комментарии.
   Instagram — через Apify (Инстаграм не пускает без входа, а Apify это умеет),
   если Apify не настроен или не сработал — пробуем yt-dlp.
   TikTok / YouTube — через yt-dlp.
2. Скачиваем видео, расшифровываем речь (Whisper) и вырезаем кадры —
   чтобы Claude понял и то, что говорят, и то, что показано и написано на экране.
3. Для каруселей/фото — скачиваем картинки.
"""
import os
import re
import base64
import asyncio
import logging
import tempfile

import httpx

logger = logging.getLogger(__name__)

APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
APIFY_IG_ACTOR = os.environ.get("APIFY_IG_ACTOR", "apify~instagram-scraper")
# Если Инстаграм режет yt-dlp по IP сервера, можно положить cookies-файл (Netscape-формат)
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE")

MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_FRAMES = 6
MAX_CAROUSEL_IMAGES = 10
FRAME_HEIGHT = 720

IG_RE = re.compile(r"https?://(?:www\.|m\.)?instagram\.com/(?:[\w.]+/)?(reels?|p|tv)/([\w-]+)", re.I)
TIKTOK_RE = re.compile(r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s]+", re.I)
YOUTUBE_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:shorts/|watch\?v=)|youtu\.be/)[\w-]+[^\s]*", re.I)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}


class SocialError(Exception):
    """Понятная пользователю ошибка (текст показываем как есть)."""


# ---------------------------------------------------------------------------
# Распознавание ссылок
# ---------------------------------------------------------------------------
def detect_social(url: str) -> tuple[str, str] | None:
    """Возвращает (платформа, нормализованная ссылка) или None, если это не соцсеть."""
    m = IG_RE.match(url)
    if m:
        kind, code = m.group(1).lower(), m.group(2)
        path = "p" if kind == "p" else "reel"
        return "instagram", f"https://www.instagram.com/{path}/{code}/"
    if TIKTOK_RE.match(url):
        return "tiktok", url.split("?")[0]
    if YOUTUBE_RE.match(url):
        return "youtube", url
    return None


# ---------------------------------------------------------------------------
# Получение данных поста
# ---------------------------------------------------------------------------
async def fetch_instagram_apify(url: str) -> dict:
    """Данные поста Instagram через Apify (instagram-scraper)."""
    endpoint = f"https://api.apify.com/v2/acts/{APIFY_IG_ACTOR}/run-sync-get-dataset-items"
    payload = {
        "directUrls": [url],
        "resultsType": "posts",
        "resultsLimit": 1,
        "addParentData": False,
    }
    async with httpx.AsyncClient(timeout=180) as http:
        resp = await http.post(endpoint, params={"token": APIFY_TOKEN}, json=payload)
    if resp.status_code == 401:
        raise SocialError("Apify не принял токен — проверь APIFY_TOKEN")
    if resp.status_code == 402:
        raise SocialError("на Apify закончились кредиты")
    resp.raise_for_status()
    items = resp.json()
    if not items or not isinstance(items, list):
        raise SocialError("пост не найден (возможно, аккаунт закрытый или пост удалён)")
    item = items[0]
    if item.get("error"):
        raise SocialError(f"Apify: {item.get('errorDescription') or item.get('error')}")
    return _normalize_apify(item, url)


def _normalize_apify(item: dict, url: str) -> dict:
    images: list[str] = []
    if item.get("type") == "Sidecar":
        for child in item.get("childPosts") or []:
            if child.get("type") != "Video" and child.get("displayUrl"):
                images.append(child["displayUrl"])
        if not images:
            images = list(item.get("images") or [])
    elif item.get("type") == "Image" and item.get("displayUrl"):
        images = [item["displayUrl"]]

    video_url = item.get("videoUrl")
    if not video_url and item.get("type") == "Sidecar":
        for child in item.get("childPosts") or []:
            if child.get("videoUrl"):
                video_url = child["videoUrl"]
                break

    comments = [
        f"@{c.get('ownerUsername', '?')}: {c.get('text', '').strip()}"
        for c in (item.get("latestComments") or [])[:15]
        if c.get("text")
    ]
    music = item.get("musicInfo") or {}
    return {
        "platform": "instagram",
        "url": item.get("url") or url,
        "type": {"Video": "рилс", "Sidecar": "карусель", "Image": "фото-пост"}.get(item.get("type"), item.get("type") or "пост"),
        "author": item.get("ownerUsername"),
        "author_name": item.get("ownerFullName"),
        "caption": item.get("caption") or "",
        "hashtags": item.get("hashtags") or [],
        "views": item.get("videoPlayCount") or item.get("videoViewCount"),
        "likes": item.get("likesCount"),
        "comments_count": item.get("commentsCount"),
        "shares": item.get("sharesCount"),
        "duration": item.get("videoDuration"),
        "posted": item.get("timestamp"),
        "music": " — ".join(x for x in (music.get("artist_name"), music.get("song_name")) if x) or None,
        "top_comments": comments,
        "video_url": video_url,
        "image_urls": images[:MAX_CAROUSEL_IMAGES],
        "thumbnail_url": item.get("displayUrl"),
    }


def _ytdlp_extract(url: str, out_dir: str) -> tuple[dict, str | None]:
    """Синхронно: метаданные + скачивание видео через yt-dlp. Запускать в потоке."""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": os.path.join(out_dir, "video.%(ext)s"),
        # mp4 до ~720p — достаточно для анализа и быстро качается
        "format": "best[ext=mp4][height<=1280]/best[height<=1280]/best",
        "max_filesize": MAX_VIDEO_BYTES,
        "socket_timeout": 30,
        "http_headers": BROWSER_HEADERS,
    }
    if YTDLP_COOKIES_FILE and os.path.exists(YTDLP_COOKIES_FILE):
        opts["cookiefile"] = YTDLP_COOKIES_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]
        path = None
        for f in os.listdir(out_dir):
            if f.startswith("video."):
                path = os.path.join(out_dir, f)
        return info, path


async def fetch_with_ytdlp(url: str, platform: str, out_dir: str) -> tuple[dict, str | None]:
    try:
        info, path = await asyncio.wait_for(asyncio.to_thread(_ytdlp_extract, url, out_dir), timeout=240)
    except asyncio.TimeoutError:
        raise SocialError("видео качается слишком долго")
    except Exception as e:
        text = str(e).lower()
        if platform == "instagram" and ("login" in text or "rate" in text or "cookies" in text or "empty media" in text):
            raise SocialError(
                "Инстаграм не отдаёт пост без входа. Нужен Apify — добавь APIFY_TOKEN в настройки бота"
            )
        if "private" in text:
            raise SocialError("это закрытый аккаунт или видео")
        raise SocialError(f"не получилось скачать: {str(e)[:200]}")

    data = {
        "platform": platform,
        "url": info.get("webpage_url") or url,
        "type": "видео",
        "author": info.get("uploader_id") or info.get("channel") or info.get("uploader"),
        "author_name": info.get("uploader") or info.get("channel"),
        "caption": info.get("description") or info.get("title") or "",
        "title": info.get("title"),
        "hashtags": info.get("tags") or [],
        "views": info.get("view_count"),
        "likes": info.get("like_count"),
        "comments_count": info.get("comment_count"),
        "duration": info.get("duration"),
        "posted": info.get("upload_date"),
        "music": " — ".join(x for x in (info.get("artist"), info.get("track")) if x) or None,
        "top_comments": [],
        "video_url": None,
        "image_urls": [],
        "thumbnail_url": info.get("thumbnail"),
    }
    return data, path


# ---------------------------------------------------------------------------
# Медиа: скачивание, кадры
# ---------------------------------------------------------------------------
async def download_bytes(url: str, max_bytes: int = MAX_VIDEO_BYTES) -> bytes:
    async with httpx.AsyncClient(timeout=120, follow_redirects=True, headers=BROWSER_HEADERS) as http:
        async with http.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks, total = [], 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise SocialError("видео слишком большое для анализа")
                chunks.append(chunk)
    return b"".join(chunks)


async def _run(*cmd: str, timeout: int = 120) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, b""
    if proc.returncode != 0:
        logger.error("%s failed: %s", cmd[0], err.decode(errors="ignore")[-1500:])
    return proc.returncode, out


async def video_duration(path: str) -> float:
    code, out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path)
    try:
        return float(out.decode().strip()) if code == 0 else 0.0
    except ValueError:
        return 0.0


async def extract_frames(path: str, count: int = MAX_FRAMES) -> list[bytes]:
    """Равномерно вырезает кадры из видео (первый — на 0.3 сек, чтобы поймать хук)."""
    duration = await video_duration(path)
    if duration <= 0:
        return []
    if duration < 3:
        times = [duration / 2]
    else:
        step = (duration - 0.6) / max(count - 1, 1)
        times = [0.3 + i * step for i in range(count)]
    frames: list[bytes] = []
    out_dir = os.path.dirname(path)
    for i, t in enumerate(times):
        dst = os.path.join(out_dir, f"frame_{i}.jpg")
        code, _ = await _run(
            "ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
            "-vf", f"scale=-2:'min({FRAME_HEIGHT},ih)'", "-q:v", "4", dst, timeout=60,
        )
        if code == 0 and os.path.exists(dst):
            with open(dst, "rb") as f:
                frames.append(f.read())
    return frames


def image_block(data: bytes, mime: str = "image/jpeg") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}}


def _sniff_mime(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
async def analyze_link(url: str, platform: str, transcribe=None) -> dict:
    """Собирает всё о посте по ссылке.

    transcribe — async-функция (bytes, filename) -> str для расшифровки речи.
    Возвращает dict: meta (данные поста), transcript, frames (jpeg bytes), images (bytes, mime).
    """
    with tempfile.TemporaryDirectory() as tmp:
        meta: dict | None = None
        video_path: str | None = None
        errors: list[str] = []

        if platform == "instagram" and APIFY_TOKEN:
            try:
                meta = await fetch_instagram_apify(url)
            except SocialError as e:
                errors.append(str(e))
            except Exception as e:
                logger.exception("Apify error")
                errors.append(f"Apify не ответил: {str(e)[:150]}")

        if meta is None:
            try:
                meta, video_path = await fetch_with_ytdlp(url, platform, tmp)
            except SocialError as e:
                errors.append(str(e))
                raise SocialError("; ".join(dict.fromkeys(errors)))

        # видео: скачиваем, если ещё не скачано
        if video_path is None and meta.get("video_url"):
            try:
                data = await download_bytes(meta["video_url"])
                video_path = os.path.join(tmp, "video.mp4")
                with open(video_path, "wb") as f:
                    f.write(data)
            except Exception:
                logger.exception("Не удалось скачать видео поста")
                video_path = None

        transcript = ""
        frames: list[bytes] = []
        if video_path and os.path.exists(video_path):
            frames = await extract_frames(video_path)
            if transcribe is not None:
                try:
                    with open(video_path, "rb") as f:
                        transcript = await transcribe(f.read(), "video.mp4")
                except Exception:
                    logger.exception("Не удалось расшифровать видео")

        images: list[tuple[bytes, str]] = []
        for img_url in meta.get("image_urls") or []:
            try:
                data = await download_bytes(img_url, max_bytes=8 * 1024 * 1024)
                mime = _sniff_mime(data)
                if mime:
                    images.append((data, mime))
            except Exception:
                logger.warning("Не удалось скачать картинку карусели %s", img_url)

        if not frames and not images and meta.get("thumbnail_url"):
            try:
                data = await download_bytes(meta["thumbnail_url"], max_bytes=8 * 1024 * 1024)
                mime = _sniff_mime(data)
                if mime:
                    images.append((data, mime))
            except Exception:
                pass

        return {"meta": meta, "transcript": transcript, "frames": frames, "images": images}


def _fmt_num(n) -> str:
    if n is None:
        return "—"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн"
    if n >= 1_000:
        return f"{n / 1_000:.1f} тыс."
    return str(n)


def describe(result: dict) -> str:
    """Текстовая сводка о посте — её видит Claude и она же сохраняется в истории."""
    m = result["meta"]
    lines = [
        f"[Пост из {m['platform'].capitalize()}: {m['url']}]",
        f"Тип: {m.get('type')}",
        f"Автор: @{m.get('author')}" + (f" ({m['author_name']})" if m.get("author_name") else ""),
        f"Просмотры: {_fmt_num(m.get('views'))} · Лайки: {_fmt_num(m.get('likes'))} · Комментарии: {_fmt_num(m.get('comments_count'))}"
        + (f" · Репосты: {_fmt_num(m['shares'])}" if m.get("shares") is not None else ""),
    ]
    if m.get("duration"):
        lines.append(f"Длительность: {float(m['duration']):.0f} сек")
    if m.get("posted"):
        lines.append(f"Опубликован: {m['posted']}")
    if m.get("music"):
        lines.append(f"Музыка: {m['music']}")
    if m.get("title") and m.get("title") != m.get("caption"):
        lines.append(f"Заголовок: {m['title']}")
    lines.append(f"Подпись: {m.get('caption') or '(нет)'}"[:3000])
    if m.get("top_comments"):
        lines.append("Комментарии зрителей:\n" + "\n".join(m["top_comments"]))
    if result.get("transcript"):
        lines.append(f"Расшифровка речи в видео:\n{result['transcript'][:6000]}")
    elif result.get("frames"):
        lines.append("Речи в видео нет (или не распознана) — смысл передаётся картинкой, текстом на экране и музыкой.")
    if result.get("frames"):
        lines.append(f"Ниже — {len(result['frames'])} кадров из видео по порядку (первый — начало ролика, хук).")
    if result.get("images"):
        lines.append(f"Ниже — {len(result['images'])} изображений поста по порядку (слайды карусели).")
    return "\n".join(lines)
