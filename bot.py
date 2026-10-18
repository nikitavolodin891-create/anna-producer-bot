import os
import io
import re
import json
import base64
import html
import logging
import asyncio
import tempfile
import shutil
from collections import defaultdict

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
import cloudinary
import cloudinary.uploader
from openai import AsyncOpenAI

from prompt import SYSTEM_PROMPT as ANNA_PROMPT, LINK_ANALYSIS_PROMPT, MONTAGE_PLAN_PROMPT
import social
import montage

logging.basicConfig(level=logging.INFO)
# httpx пишет в лог каждый запрос к Telegram вместе с токеном бота в URL — глушим
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # нужен для распознавания голосовых и кружков
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "4000"))

# Кому разрешено пользоваться ботом: Telegram user ID через запятую.
# Пусто — бот открыт всем (так было раньше). Узнать свой ID: команда /id.
ALLOWED_USER_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "")) if x.strip().isdigit()
}

# Асинхронные клиенты: пока Claude думает над одним сообщением, бот не зависает для остальных.
client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Cloudinary — публичный хостинг для видео
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True,
)

FORMAT_NOTE = """

=== ФОРМАТ ОТВЕТОВ В TELEGRAM ===
Ты отвечаешь в Telegram. Можно использовать **жирный** для акцентов и заголовков разделов,
списки через "- ", эмодзи — умеренно. НЕ используй таблицы, # заголовки и вложенные списки —
Telegram их не показывает. Пиши так, чтобы удобно читалось с телефона.
"""

SYSTEM_PROMPT = ANNA_PROMPT + FORMAT_NOTE

# ---------------------------------------------------------------------------
# История переписки
# ---------------------------------------------------------------------------
# DATA_DIR указывает на подключённый в Railway Volume (переживает redeploy).
DATA_DIR = os.environ.get("DATA_DIR", "/data")
HISTORY_FILE = os.path.join(DATA_DIR, "conversations.json")
MAX_HISTORY_MESSAGES = 40  # сколько последних сообщений на чат храним и передаём модели


def _load_conversations() -> dict:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return {int(k): v for k, v in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception:
        logger.exception("Не удалось прочитать файл истории, начинаем с чистого листа")
        return {}


def _save_conversations() -> None:
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        tmp_path = HISTORY_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(conversations, f, ensure_ascii=False)
        os.replace(tmp_path, HISTORY_FILE)  # атомарно: файл не побьётся, если контейнер упадёт посреди записи
    except Exception:
        logger.exception("Не удалось сохранить историю в %s", HISTORY_FILE)


def _sanitize_history(history: list[dict]) -> list[dict]:
    """Приводит историю к виду, который точно примет API:
    - только текстовые сообщения (картинки/PDF в истории не храним — они раздувают запросы);
    - начинается с сообщения пользователя;
    - роли чередуются (подряд идущие сообщения одной роли склеиваются).
    """
    clean: list[dict] = []
    for msg in history:
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant"):
            continue
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            content = "\n".join(t for t in texts if t) or "[вложение]"
        if not isinstance(content, str) or not content.strip():
            continue
        if clean and clean[-1]["role"] == role:
            clean[-1]["content"] += "\n\n" + content
        else:
            clean.append({"role": role, "content": content})
    while clean and clean[0]["role"] != "user":
        clean.pop(0)
    return clean


conversations: dict[int, list[dict]] = {
    chat_id: _sanitize_history(h) for chat_id, h in _load_conversations().items()
}
chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ---------------------------------------------------------------------------
# Отправка ответов: разбивка длинных текстов + форматирование
# ---------------------------------------------------------------------------
TELEGRAM_SAFE_LEN = 3800  # лимит Telegram — 4096 символов, держим запас под HTML-теги


def markdown_to_telegram_html(text: str) -> str:
    """Переводит простой Markdown от Claude в HTML, который понимает Telegram."""
    text = html.escape(text, quote=False)
    # заголовки "# ...", "## ..." → жирная строка
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # блоки кода ```...```
    text = re.sub(r"```(?:\w+)?\n?(.*?)```", r"<pre>\1</pre>", text, flags=re.DOTALL)
    # **жирный** и __жирный__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)
    # `код`
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    # маркеры списков "* " / "- " в начале строки → "• "
    text = re.sub(r"^(\s*)[\*\-]\s+", r"\1• ", text, flags=re.MULTILINE)
    # горизонтальные линии
    text = re.sub(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", "", text, flags=re.MULTILINE)
    return text


def split_text(text: str, limit: int = TELEGRAM_SAFE_LEN) -> list[str]:
    """Режет длинный текст на куски по абзацам/строкам, чтобы влезть в лимит Telegram."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


async def send_long(update: Update, text: str) -> None:
    """Отправляет ответ любой длины: частями и с форматированием (если оно сломается — простым текстом)."""
    if not text or not text.strip():
        text = "Пустой ответ — попробуй переформулировать вопрос."
    for chunk in split_text(text):
        try:
            await update.effective_message.reply_text(
                markdown_to_telegram_html(chunk),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest:
            # Если Telegram не принял HTML (кривая разметка) — шлём как есть, без форматирования.
            await update.effective_message.reply_text(chunk, disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Общение с Claude
# ---------------------------------------------------------------------------
async def ask_claude(
    update: Update,
    chat_id: int,
    content: str | list[dict],
    history_note: str | None = None,
) -> None:
    """Единая точка общения с Claude.

    content — то, что видит модель сейчас (текст или текст + картинка/PDF).
    history_note — как это сообщение запомнить в истории (без тяжёлых вложений).
    В историю сообщение попадает ТОЛЬКО если Claude успешно ответил — ошибки
    больше не засоряют контекст и не ломают следующие запросы.
    """
    async with chat_locks[chat_id]:
        history = conversations.get(chat_id, [])
        note = history_note if history_note is not None else (content if isinstance(content, str) else "[вложение]")

        messages = _sanitize_history(history + [{"role": "user", "content": note}])[-MAX_HISTORY_MESSAGES:]
        messages = _sanitize_history(messages)  # после обрезки снова начинаем с user
        # последнее сообщение отдаём модели в полном виде (с картинкой/PDF, если есть)
        messages[-1] = {"role": "user", "content": content}

        await update.effective_chat.send_action("typing")
        typing_task = asyncio.create_task(_keep_typing(update))
        try:
            response = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
            reply_text = "".join(block.text for block in response.content if block.type == "text")
        except anthropic.APIStatusError as e:
            logger.exception("Anthropic API error")
            if e.status_code == 529 or e.status_code >= 500:
                msg = "Claude сейчас перегружен. Попробуй ещё раз через минуту 🙏"
            elif e.status_code == 429:
                msg = "Слишком много запросов подряд. Подожди минутку и повтори."
            else:
                msg = f"Ошибка при обращении к агенту ({e.status_code}). Попробуй ещё раз."
            await update.effective_message.reply_text(msg)
            return
        except Exception:
            logger.exception("Anthropic API error")
            await update.effective_message.reply_text("Не получилось получить ответ. Попробуй ещё раз.")
            return
        finally:
            typing_task.cancel()

        if response.stop_reason == "max_tokens":
            reply_text += "\n\n_(ответ обрезан по длине — напиши «продолжи», и я допишу)_"

        history = history + [
            {"role": "user", "content": note},
            {"role": "assistant", "content": reply_text},
        ]
        conversations[chat_id] = _sanitize_history(history[-MAX_HISTORY_MESSAGES:])
        _save_conversations()

    await send_long(update, reply_text)


async def _keep_typing(update: Update) -> None:
    """Держит статус «печатает…», пока Claude пишет длинный ответ."""
    try:
        while True:
            await asyncio.sleep(4.5)
            await update.effective_chat.send_action("typing")
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Доступ
# ---------------------------------------------------------------------------
def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def deny(update: Update) -> None:
    user = update.effective_user
    logger.warning("Отказ в доступе: user_id=%s username=%s", user and user.id, user and user.username)
    await update.effective_message.reply_text(
        "Это приватный бот. Если тебе нужен доступ — передай владельцу свой ID: "
        f"{user.id if user else '?'}"
    )


def guarded(handler):
    """Обёртка: пускает в хендлер только разрешённых пользователей."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message is None:
            return
        if not is_allowed(update):
            await deny(update)
            return
        await handler(update, context)
    wrapper.__name__ = handler.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я Анна — твой ИИ-продюсер для Instagram @anyalakshmi ✨\n\n"
        "Что я умею:\n"
        "• Идеи, хуки и сценарии Reels в твоём голосе\n"
        "• Подписи к постам, разбор того, что заходит\n"
        "• Отвечаю на любые вопросы — про контент, бизнес и просто жизнь\n"
        "• Понимаю голосовые и кружки 🎙\n"
        "• Разбираю рилсы и посты по ссылке (Instagram, TikTok, YouTube)\n"
        "• Читаю фото, PDF и сайты\n"
        "• Монтирую Reels: пришли видео — верну готовый ролик с субтитрами 🎬\n"
        "  (в подписи к видео можно написать пожелания, например «динамично»)\n\n"
        "Просто напиши или наговори, что нужно.\n"
        "/new — начать новый диалог (забыть контекст)"
    )


async def new_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conversations[update.effective_chat.id] = []
    _save_conversations()
    await update.message.reply_text("Начали с чистого листа ✨ О чём поговорим?")


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Доступна всем, чтобы человек мог узнать свой ID для списка доступа.
    await update.message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


# ---------------------------------------------------------------------------
# Текст и ссылки
# ---------------------------------------------------------------------------
URL_RE = re.compile(r'https?://[^\s<>"\']+')
MAX_FETCH_BYTES = 8 * 1024 * 1024
MAX_LINKS_PER_MESSAGE = 2


async def fetch_url_content(url: str) -> dict:
    """Скачивает содержимое по ссылке и определяет, как его отдать Клоду."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept-Language": "ru,en;q=0.8",
            },
        ) as http_client:
            resp = await http_client.get(url)
            resp.raise_for_status()

            content_length = int(resp.headers.get("content-length") or 0)
            if content_length and content_length > MAX_FETCH_BYTES:
                return {"kind": "error", "message": f"файл слишком большой ({content_length / 1024 / 1024:.1f} МБ)"}

            data = resp.content
            if len(data) > MAX_FETCH_BYTES:
                return {"kind": "error", "message": "файл слишком большой"}

            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()

            if content_type in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                return {"kind": "image", "data": data, "mime": content_type}
            if content_type == "application/pdf":
                return {"kind": "pdf", "data": data}
            if "html" in content_type or content_type.startswith("text/") or content_type == "":
                soup = BeautifulSoup(data, "html.parser")
                title = (soup.title.string.strip() if soup.title and soup.title.string else "")
                og_desc = soup.find("meta", attrs={"property": "og:description"}) or soup.find(
                    "meta", attrs={"name": "description"}
                )
                desc = og_desc.get("content", "").strip() if og_desc else ""
                for tag in soup(["script", "style", "noscript", "svg"]):
                    tag.decompose()
                text = soup.get_text(separator="\n")
                text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
                combined = "\n\n".join(x for x in (title, desc, text) if x)
                if len(combined) < 40:
                    return {"kind": "error", "message": "страница не отдаёт текст (нужен вход или JavaScript)"}
                return {"kind": "text", "content": combined[:12000]}
            return {"kind": "error", "message": f"не умею обрабатывать содержимое типа {content_type or 'неизвестно'}"}

    except httpx.HTTPStatusError as e:
        return {"kind": "error", "message": f"сайт ответил ошибкой {e.response.status_code}"}
    except Exception as e:
        return {"kind": "error", "message": f"не получилось открыть ссылку: {e}"}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await process_user_text(update, update.message.text)


async def process_user_text(update: Update, user_text: str) -> None:
    """Общая обработка текста — и набранного, и распознанного из голосового/кружка."""
    chat_id = update.effective_chat.id
    urls = URL_RE.findall(user_text)[:MAX_LINKS_PER_MESSAGE]

    if not urls:
        await ask_claude(update, chat_id, user_text)
        return

    # Ссылки на соцсети (Instagram / TikTok / YouTube) — отдельный разбор с видео и кадрами
    social_link = next((s for s in (social.detect_social(u) for u in urls) if s), None)
    if social_link:
        await process_social_link(update, user_text, *social_link)
        return

    await update.effective_message.reply_text("Открываю ссылку, секунду...")

    content_blocks: list[dict] = []
    notes: list[str] = []
    for url in urls:
        result = await fetch_url_content(url)
        if result["kind"] == "text":
            content_blocks.append({"type": "text", "text": f"[Содержимое страницы {url}]:\n{result['content']}"})
            notes.append(f"[Ссылка: {url}]")
        elif result["kind"] == "image":
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": result["mime"], "data": base64.b64encode(result["data"]).decode()},
            })
            notes.append(f"[Изображение по ссылке: {url}]")
        elif result["kind"] == "pdf":
            content_blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(result["data"]).decode()},
            })
            notes.append(f"[PDF по ссылке: {url}]")
        else:
            content_blocks.append({"type": "text", "text": f"[Не удалось открыть ссылку {url}: {result['message']}]"})
            notes.append(f"[Не удалось открыть ссылку {url}: {result['message']}]")

    content_blocks.append({"type": "text", "text": user_text})
    await ask_claude(update, chat_id, content_blocks, history_note=" ".join(notes + [user_text]))


PLATFORM_NAMES = {"instagram": "Инстаграм", "tiktok": "TikTok", "youtube": "YouTube"}


async def process_social_link(update: Update, user_text: str, platform: str, url: str) -> None:
    """Рилс/пост по ссылке: данные, расшифровка, кадры → разбор от Claude."""
    status = await update.effective_message.reply_text(
        f"Открываю {PLATFORM_NAMES.get(platform, 'ссылку')}: скачиваю, смотрю и слушаю ролик... "
        "Это займёт до минуты ⏳"
    )
    await update.effective_chat.send_action("typing")

    try:
        result = await social.analyze_link(url, platform, transcribe=transcribe if openai_client else None)
    except social.SocialError as e:
        await status.edit_text(f"Не получилось открыть ссылку: {e}")
        return
    except Exception:
        logger.exception("Social link error")
        await status.edit_text("Не получилось открыть ссылку. Попробуй ещё раз чуть позже.")
        return

    summary = social.describe(result)
    try:
        m = result["meta"]
        await status.edit_text(
            f"Открыла ✅ @{m.get('author') or '?'} · {social._fmt_num(m.get('views'))} просмотров. Разбираю..."
        )
    except BadRequest:
        pass

    # что хочет пользователь кроме самой ссылки (например «сделай такой же про деньги»)
    request = URL_RE.sub("", user_text).strip()
    task = request if len(request) >= 3 else LINK_ANALYSIS_PROMPT

    content: list[dict] = [{"type": "text", "text": summary}]
    content += [social.image_block(fr) for fr in result["frames"]]
    content += [social.image_block(data, mime) for data, mime in result["images"]]
    content.append({"type": "text", "text": task})

    # В историю — текстовая сводка (без картинок), чтобы можно было продолжить: «а ещё вариант?»
    note = summary[:8000] + (f"\n\n{request}" if len(request) >= 3 else "\n\n[Разбери этот пост]")
    await ask_claude(update, update.effective_chat.id, content, history_note=note)


# ---------------------------------------------------------------------------
# Файлы из Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_FILE_LIMIT = 2000 * 1024 * 1024  # лимит self-hosted Telegram Bot API сервера


async def download_telegram_file(tg_file) -> bytes:
    """Скачивает файл Telegram. В режиме --local сервер отдаёт абсолютный путь на диске."""
    file_path = tg_file.file_path
    if file_path and os.path.isabs(file_path) and os.path.exists(file_path):
        with open(file_path, "rb") as f:
            return f.read()
    buf = io.BytesIO()
    await tg_file.download_to_memory(out=buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Голосовые, аудио и кружки
# ---------------------------------------------------------------------------
WHISPER_MAX_BYTES = 25 * 1024 * 1024  # лимит Whisper API на файл


async def extract_audio_mp3(data: bytes, suffix: str) -> bytes | None:
    """Достаёт из видео/аудио дорожку в лёгкий mp3 (моно, 64 кбит/с) — для Whisper."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        src = os.path.join(tmp_dir, "in" + suffix)
        dst = os.path.join(tmp_dir, "out.mp3")
        with open(src, "wb") as f:
            f.write(data)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", dst,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(dst):
            logger.error("ffmpeg не смог достать звук: %s", stderr.decode(errors="ignore")[-2000:])
            return None
        with open(dst, "rb") as f:
            return f.read()


async def transcribe(data: bytes, filename: str) -> str:
    """Распознаёт речь через Whisper. Видео и тяжёлые файлы сначала превращаем в лёгкий mp3."""
    suffix = os.path.splitext(filename)[1] or ".ogg"
    if suffix in (".mp4", ".mov", ".webm", ".mkv") or len(data) > WHISPER_MAX_BYTES:
        audio = await extract_audio_mp3(data, suffix)
        if audio is None:
            raise RuntimeError("не удалось извлечь звук из файла")
        data, filename = audio, "audio.mp3"
    if len(data) > WHISPER_MAX_BYTES:
        raise RuntimeError("запись слишком длинная для распознавания (больше ~50 минут)")
    buf = io.BytesIO(data)
    buf.name = filename  # Whisper определяет формат по расширению
    transcript = await openai_client.audio.transcriptions.create(model="whisper-1", file=buf)
    return transcript.text.strip()


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Голосовые, аудиофайлы и кружки: распознаём и отвечаем как на обычный текст."""
    msg = update.message
    if openai_client is None:
        await msg.reply_text("Распознавание голоса не настроено (нет ключа OpenAI). Напиши, пожалуйста, текстом.")
        return

    if msg.voice:
        media, filename, label = msg.voice, "voice.ogg", "голосовое"
    elif msg.video_note:
        media, filename, label = msg.video_note, "circle.mp4", "кружок"
    elif msg.audio:
        media = msg.audio
        filename = msg.audio.file_name or "audio.mp3"
        label = "аудио"
    else:
        return

    status = await msg.reply_text(f"Слушаю {label}... 🎧")
    await update.effective_chat.send_action("typing")

    try:
        tg_file = await context.bot.get_file(media.file_id)
        data = await download_telegram_file(tg_file)
        recognized_text = await transcribe(data, filename)
    except Exception as e:
        logger.exception("Transcription error")
        await status.edit_text(f"Не получилось распознать {label}: {e}\nПопробуй ещё раз или напиши текстом.")
        return

    if not recognized_text:
        await status.edit_text("Не расслышала, там как будто пусто. Попробуй ещё раз или напиши текстом.")
        return

    shown = recognized_text if len(recognized_text) <= 700 else recognized_text[:700] + "…"
    try:
        await status.edit_text(f"🎙 «{shown}»")
    except BadRequest:
        pass

    caption = (msg.caption or "").strip()
    text_for_model = f"{caption}\n\n{recognized_text}" if caption else recognized_text
    await process_user_text(update, text_for_model)


# ---------------------------------------------------------------------------
# Видео: сжатие и заливка на Cloudinary (режим «просто ссылка»)
# ---------------------------------------------------------------------------
CLOUDINARY_MAX_BYTES = 100 * 1024 * 1024
COMPRESS_TARGET_BYTES = int(CLOUDINARY_MAX_BYTES * 0.9)
COMPRESS_MAX_HEIGHT = 1080
COMPRESS_TIMEOUT_SECONDS = 480


async def _ffprobe_video_info(path: str) -> dict | None:
    """Длительность и высота видео через ffprobe."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "format=duration:stream=height",
            "-of", "default=noprint_wrappers=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        info: dict = {}
        for line in stdout.decode(errors="ignore").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                info[key.strip()] = value.strip()
        duration = float(info.get("duration", 0) or 0)
        height = int(info.get("height", 0) or 0)
        if duration <= 0:
            return None
        return {"duration": duration, "height": height}
    except Exception:
        logger.exception("ffprobe не смог определить параметры видео")
        return None


async def compress_video_if_needed(data: bytes) -> bytes:
    """Пережимает видео больше 100 МБ под лимит Cloudinary (с даунскейлом до 1080p)."""
    if len(data) <= CLOUDINARY_MAX_BYTES:
        return data

    with tempfile.TemporaryDirectory() as tmp_dir:
        src_path = os.path.join(tmp_dir, "in.mp4")
        dst_path = os.path.join(tmp_dir, "out.mp4")
        with open(src_path, "wb") as f:
            f.write(data)

        try:
            free_bytes = shutil.disk_usage(tmp_dir).free
            logger.info("Сжатие видео: %.1f МБ, свободно %.1f МБ", len(data) / 1048576, free_bytes / 1048576)
        except Exception:
            pass

        info = await _ffprobe_video_info(src_path)
        if not info:
            return data
        duration, height = info["duration"], info["height"]

        audio_kbps = 128
        target_total_kbps = (COMPRESS_TARGET_BYTES * 8 / 1000) / duration
        video_kbps = max(int(target_total_kbps - audio_kbps), 150)

        cmd = ["ffmpeg", "-y", "-i", src_path]
        if height and height > COMPRESS_MAX_HEIGHT:
            cmd += ["-vf", f"scale=-2:{COMPRESS_MAX_HEIGHT}"]
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            dst_path,
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=COMPRESS_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.error("ffmpeg сжатие не уложилось в %s сек", COMPRESS_TIMEOUT_SECONDS)
            proc.kill()
            await proc.wait()
            return data

        if proc.returncode != 0 or not os.path.exists(dst_path):
            logger.error("ffmpeg сжатие не удалось: %s", stderr.decode(errors="ignore")[-4000:])
            return data

        with open(dst_path, "rb") as f:
            compressed = f.read()
        return compressed if compressed and len(compressed) < len(data) else data


async def save_telegram_file(tg_file, dst: str) -> None:
    """Сохраняет файл Telegram на диск, не держа его целиком в памяти."""
    file_path = tg_file.file_path
    if file_path and os.path.isabs(file_path) and os.path.exists(file_path):
        shutil.copyfile(file_path, dst)
    else:
        await tg_file.download_to_drive(dst)


async def upload_video_link(update: Update, context: ContextTypes.DEFAULT_TYPE, video) -> None:
    """Старый режим: просто загрузить видео на Cloudinary и дать публичную ссылку."""
    status = await update.message.reply_text("Загружаю видео, подожди немного...")
    await update.effective_chat.send_action("upload_video")
    try:
        tg_file = await context.bot.get_file(video.file_id)
        data = await download_telegram_file(tg_file)

        if len(data) > CLOUDINARY_MAX_BYTES:
            await status.edit_text(
                f"Видео весит {len(data) / 1048576:.0f} МБ — сжимаю под лимит. "
                "Для 4K это может занять несколько минут..."
            )
            data = await compress_video_if_needed(data)
            if len(data) > CLOUDINARY_MAX_BYTES:
                await update.message.reply_text(
                    "Не получилось сжать видео меньше 100 МБ. Сожми его при отправке в Telegram "
                    "или залей на Google Drive/Dropbox и пришли ссылку."
                )
                return

        upload_result = await asyncio.to_thread(
            cloudinary.uploader.upload_large, io.BytesIO(data), resource_type="video", folder="anna_producer_bot",
        )
        await update.message.reply_text(f"Готово! Публичная ссылка на видео:\n\n{upload_result['secure_url']}")
    except Exception as e:
        logger.exception("Video upload error")
        await update.message.reply_text(f"Не получилось загрузить видео: {e}\nПопробуй ещё раз.")


# ---------------------------------------------------------------------------
# Автомонтаж
# ---------------------------------------------------------------------------
LINK_MODE_RE = re.compile(r"(ссылк|cloudinary|без монтажа|не монтир)", re.I)
MONTAGE_MAX_SECONDS = int(os.environ.get("MONTAGE_MAX_SECONDS", "600"))  # до 10 минут исходника
WHISPER_LANGS = {"russian": "ru", "english": "en", "ukrainian": "uk", "spanish": "es", "german": "de", "french": "fr"}


async def transcribe_words(video_path: str) -> tuple[list[montage.Word], str, str]:
    """Расшифровка с таймкодом каждого слова — по ним режем паузы и ставим субтитры."""
    audio_path = video_path + ".mp3"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", audio_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0 or not os.path.exists(audio_path):
        return [], "", "ru"
    if os.path.getsize(audio_path) > WHISPER_MAX_BYTES:
        raise RuntimeError("видео слишком длинное для расшифровки")
    with open(audio_path, "rb") as f:
        resp = await openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.mp3", f.read()),
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )
    words = [
        montage.Word(w.word.strip(), float(w.start), float(w.end))
        for w in (getattr(resp, "words", None) or [])
        if w.word.strip()
    ]
    lang = WHISPER_LANGS.get((getattr(resp, "language", "") or "").lower(), "ru")
    return words, (resp.text or "").strip(), lang


async def make_montage_plan(transcript: str, wishes: str) -> montage.Plan:
    """Claude-продюсер решает: хук-заголовок, темп, наезды/перебивки, подпись к посту."""
    if not transcript and not wishes:
        return montage.Plan(hook_title="")
    prompt = MONTAGE_PLAN_PROMPT.format(
        transcript=transcript[:8000] or "(речи нет или не распознана)",
        wishes=wishes or "(нет — реши сама как продюсер)",
    )
    try:
        resp = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        return montage.parse_plan(text)
    except Exception:
        logger.exception("Не удалось составить план монтажа")
        return montage.Plan()


def _mmss(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


async def remember(chat_id: int, user_note: str, assistant_text: str) -> None:
    async with chat_locks[chat_id]:
        history = conversations.get(chat_id, []) + [
            {"role": "user", "content": user_note},
            {"role": "assistant", "content": assistant_text},
        ]
        conversations[chat_id] = _sanitize_history(history[-MAX_HISTORY_MESSAGES:])
        _save_conversations()


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Видео → автомонтаж. Если в подписи «ссылка» / «без монтажа» — просто публичная ссылка."""
    msg = update.message
    video = msg.video or msg.document
    if video is None:
        return

    file_size = getattr(video, "file_size", None)
    if file_size and file_size > TELEGRAM_BOT_FILE_LIMIT:
        await msg.reply_text(
            f"Файл весит {file_size / 1048576:.0f} МБ — это больше лимита 2 ГБ.\n"
            "Сожми его при отправке или пришли покороче."
        )
        return

    caption = (msg.caption or "").strip()
    if LINK_MODE_RE.search(caption):
        await upload_video_link(update, context, video)
        return
    await montage_video(update, context, video, caption)


async def montage_video(update: Update, context: ContextTypes.DEFAULT_TYPE, video, wishes: str) -> None:
    chat_id = update.effective_chat.id
    steps: list[str] = []
    status = await update.message.reply_text("🎬 Принял видео — монтирую. Это займёт несколько минут.\n\n⏳ Скачиваю...")

    async def step(done: str | None, now: str | None) -> None:
        if done:
            steps.append(f"✅ {done}")
        text = "🎬 Монтаж\n\n" + "\n".join(steps) + (f"\n⏳ {now}" if now else "")
        try:
            await status.edit_text(text)
        except BadRequest:
            pass

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "source.mp4")
        try:
            tg_file = await context.bot.get_file(video.file_id)
            await save_telegram_file(tg_file, src)
            info = await montage.probe(src)
        except montage.MontageError as e:
            await step(None, None)
            await update.message.reply_text(f"Не получилось открыть видео: {e}")
            return
        except Exception:
            logger.exception("Montage download error")
            await update.message.reply_text("Не получилось скачать видео. Попробуй отправить ещё раз.")
            return

        if info["duration"] > MONTAGE_MAX_SECONDS:
            await status.edit_text(
                f"Видео длится {_mmss(info['duration'])} — для Reels это слишком долго "
                f"(монтирую до {_mmss(MONTAGE_MAX_SECONDS)}). Пришли фрагмент покороче."
            )
            return

        # 1. Расшифровка речи с таймкодами слов
        words, transcript, lang = [], "", "ru"
        if info["has_audio"] and openai_client is not None:
            await step(f"Скачала ({_mmss(info['duration'])})", "Слушаю и расшифровываю речь...")
            try:
                words, transcript, lang = await transcribe_words(src)
            except Exception:
                logger.exception("Montage transcription error")
        else:
            await step(f"Скачала ({_mmss(info['duration'])})", None)

        # 2. Продюсерский план
        await step("Расшифровала речь" if transcript else "Речи нет — монтирую по картинке", "Продюсирую: хук, темп, подпись к посту...")
        plan = await make_montage_plan(transcript, wishes)

        # 3. Монтаж: Submagic → запасной свой
        result = os.path.join(tmp, "final.mp4")
        engine = None
        fallback_note = ""
        if montage.submagic_enabled():
            await step("Продумала хук и подпись", "Монтирую в Submagic: субтитры, наезды, паузы (обычно 2–5 минут)...")

            async def on_progress(s):
                labels = {"processing": "обрабатывает", "transcribing": "расшифровывает", "exporting": "рендерит видео"}
                if s in labels:
                    await step(None, f"Submagic {labels[s]}...")
            try:
                url = await montage.submagic_edit(src, lang, plan, plan.hook_title or "Reels", on_progress=on_progress)
                await montage.download_file(url, result)
                engine = "Submagic"
            except Exception as e:
                logger.exception("Submagic error")
                fallback_note = f"Submagic не сработал ({e}) — смонтировала сама."
                await step(None, "Submagic не сработал — монтирую сама...")
        else:
            await step("Продумала хук и подпись", "Монтирую: режу паузы, ставлю субтитры и наезды...")

        stats = None
        if engine is None:
            try:
                stats = await montage.ffmpeg_edit(src, result, words, plan, tmp)
                engine = "ffmpeg"
            except montage.MontageError as e:
                await step(None, None)
                await update.message.reply_text(f"Не получилось смонтировать: {e}")
                return
            except Exception:
                logger.exception("ffmpeg montage error")
                await update.message.reply_text("Не получилось смонтировать видео. Попробуй ещё раз.")
                return

        await step("Смонтировала", "Отправляю видео...")
        try:
            final_info = await montage.probe(result)
        except Exception:
            final_info = {"duration": 0}

        if stats:
            summary = (
                f"✂️ Было {_mmss(stats['original'])} → стало {_mmss(stats['final'])}, "
                f"вырезала пауз: {stats['cuts']}"
            )
        else:
            summary = f"✂️ Смонтировано в Submagic · {_mmss(final_info.get('duration', 0))}"
        if fallback_note:
            summary += f"\n⚠️ {fallback_note}"

        await update.effective_chat.send_action("upload_video")
        try:
            with open(result, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=summary[:1000],
                    supports_streaming=True,
                    width=1080,
                    height=1920,
                    duration=int(final_info.get("duration") or 0) or None,
                    read_timeout=600,
                    write_timeout=600,
                )
        except Exception:
            logger.exception("Send video error")
            await update.message.reply_text("Видео смонтировано, но не получилось отправить его в Telegram. Попробуй ещё раз.")
            return

    try:
        await status.delete()
    except Exception:
        pass

    parts = []
    if plan.comment:
        parts.append(f"**Комментарий продюсера:** {plan.comment}")
    if plan.hook_title:
        parts.append(f"**Хук на экране:** {plan.hook_title}")
    if plan.post_caption:
        parts.append(f"**Подпись к посту:**\n\n{plan.post_caption}")
    text = "\n\n".join(parts)
    if text:
        await send_long(update, text)

    note = "[Прислала видео на монтаж]" + (f" Пожелания: {wishes}" if wishes else "")
    if transcript:
        note += f"\nРасшифровка ролика: {transcript[:4000]}"
    await remember(chat_id, note, text or "[Смонтировала видео]")


# ---------------------------------------------------------------------------
# Фото и документы
# ---------------------------------------------------------------------------
DEFAULT_IMAGE_PROMPT = (
    "Посмотри на это изображение и прокомментируй его содержательно — как продюсер, "
    "если это релевантно контенту Анны, или просто по существу, если это что-то другое."
)
SUPPORTED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фото: отправляем в Claude как изображение (у Claude есть зрение)."""
    photo = update.message.photo[-1] if update.message.photo else None
    if photo is None:
        return
    caption = (update.message.caption or "").strip()
    await update.effective_chat.send_action("typing")

    try:
        tg_file = await context.bot.get_file(photo.file_id)
        data = await download_telegram_file(tg_file)
    except Exception as e:
        logger.exception("Photo download error")
        await update.message.reply_text(f"Не получилось скачать фото: {e}\nПопробуй ещё раз.")
        return

    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(data).decode()}},
        {"type": "text", "text": caption or DEFAULT_IMAGE_PROMPT},
    ]
    note = f"[Фото] {caption}" if caption else "[Фото без подписи]"
    await ask_claude(update, update.effective_chat.id, content, history_note=note)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Файлы-документы: PDF, изображения, текстовые файлы."""
    doc = update.message.document
    if doc is None:
        return

    mime = (doc.mime_type or "").lower()
    file_name = doc.file_name or ""
    lower_name = file_name.lower()
    caption = (update.message.caption or "").strip()

    is_pdf = mime == "application/pdf" or lower_name.endswith(".pdf")
    is_image = mime in SUPPORTED_IMAGE_MIMES
    is_text = mime.startswith("text/") or lower_name.endswith((".txt", ".md", ".csv", ".json"))
    is_audio = mime.startswith("audio/") or lower_name.endswith((".mp3", ".m4a", ".ogg", ".wav", ".opus"))

    if is_audio:
        await handle_audio_document(update, context, doc)
        return

    if not (is_pdf or is_image or is_text):
        await update.message.reply_text(
            f"Пока не умею читать файлы такого типа ({mime or 'неизвестный формат'}).\n"
            "Поддерживаю: PDF, изображения, текстовые файлы, аудио и видео."
        )
        return

    if doc.file_size and doc.file_size > 30 * 1024 * 1024:
        await update.message.reply_text("Файл больше 30 МБ — такой не смогу разобрать целиком. Пришли поменьше.")
        return

    await update.effective_chat.send_action("typing")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = await download_telegram_file(tg_file)
    except Exception as e:
        logger.exception("Document download error")
        await update.message.reply_text(f"Не получилось скачать файл: {e}\nПопробуй ещё раз.")
        return

    if is_pdf:
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()}}
        default_prompt = "Посмотри этот PDF и разбери содержимое по существу."
        note_label = f"[PDF: {file_name}]"
    elif is_image:
        block = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}}
        default_prompt = DEFAULT_IMAGE_PROMPT
        note_label = f"[Изображение-файл: {file_name}]"
    else:
        block = {"type": "text", "text": f"[Файл {file_name}]:\n{data.decode('utf-8', errors='ignore')[:60000]}"}
        default_prompt = "Разбери этот файл по существу."
        note_label = f"[Файл: {file_name}]"

    content = [block, {"type": "text", "text": caption or default_prompt}]
    note = f"{note_label} {caption}" if caption else note_label
    await ask_claude(update, update.effective_chat.id, content, history_note=note)


async def handle_audio_document(update: Update, context: ContextTypes.DEFAULT_TYPE, doc) -> None:
    """Аудио, присланное файлом (например, запись из диктофона)."""
    if openai_client is None:
        await update.message.reply_text("Распознавание голоса не настроено (нет ключа OpenAI).")
        return
    status = await update.message.reply_text("Слушаю запись... 🎧")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = await download_telegram_file(tg_file)
        text = await transcribe(data, doc.file_name or "audio.mp3")
    except Exception as e:
        logger.exception("Audio document error")
        await status.edit_text(f"Не получилось распознать запись: {e}")
        return
    if not text:
        await status.edit_text("В записи не слышно речи.")
        return
    shown = text if len(text) <= 700 else text[:700] + "…"
    try:
        await status.edit_text(f"🎙 «{shown}»")
    except BadRequest:
        pass
    caption = (update.message.caption or "").strip()
    await process_user_text(update, f"{caption}\n\n{text}" if caption else text)


# ---------------------------------------------------------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Необработанная ошибка", exc_info=context.error)


def main() -> None:
    # Свой (self-hosted) Telegram Bot API сервер — снимает лимит 20 МБ на скачивание файлов.
    # Сервер и бот работают в одном контейнере (см. start.sh) и делят общий диск.
    local_api_url = os.environ.get("LOCAL_BOT_API_URL", "http://localhost:8081")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .base_url(f"{local_api_url}/bot")
        .base_file_url(f"{local_api_url}/file/bot")
        .connect_timeout(60)
        .read_timeout(120)
        .write_timeout(120)
        .pool_timeout(60)
        .concurrent_updates(True)  # обрабатываем сообщения параллельно, порядок в чате держит chat_locks
        .build()
    )
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("start", guarded(start)))
    app.add_handler(CommandHandler("new", guarded(new_dialog)))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, guarded(handle_video)))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, guarded(handle_voice)))
    app.add_handler(MessageHandler(filters.PHOTO, guarded(handle_photo)))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.VIDEO, guarded(handle_document)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guarded(handle_message)))
    app.add_error_handler(on_error)
    logger.info("Bot started (model=%s, access=%s)", CLAUDE_MODEL, "restricted" if ALLOWED_USER_IDS else "open")
    app.run_polling()


if __name__ == "__main__":
    main()
