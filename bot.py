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
from openai import AsyncOpenAI

from prompt import BOT_CAPABILITIES
from prompt import SYSTEM_PROMPT as ANNA_PROMPT, LINK_ANALYSIS_PROMPT, VIDEO_REVIEW_PROMPT, AUDIT_PROMPT, TRENDS_PROMPT, EDITOR_PROMPT, SCRIPT_FORMAT_NOTE, FIXER_PROMPT, CAROUSEL_PROMPT, WEB_SEARCH_NOTE
import social
import audit
import trends
import facts
import lint
import carousel
import design_store
import datetime
from telegram import InputMediaPhoto, InputMediaDocument

logging.basicConfig(level=logging.INFO)
# httpx пишет в лог каждый запрос к Telegram вместе с токеном бота в URL — глушим
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # нужен для распознавания голосовых и кружков
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "4000"))
# Поиск в интернете (встроенный инструмент Claude): $10 за 1000 поисков + токены результатов.
WEB_SEARCH_ENABLED = os.environ.get("WEB_SEARCH_ENABLED", "1") != "0"
WEB_SEARCH_MAX_USES = int(os.environ.get("WEB_SEARCH_MAX_USES", "5"))

# Кому разрешено пользоваться ботом: Telegram user ID через запятую.
# Пусто — бот открыт всем (так было раньше). Узнать свой ID: команда /id.
ALLOWED_USER_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "")) if x.strip().isdigit()
}

# Асинхронные клиенты: пока Claude думает над одним сообщением, бот не зависает для остальных.
client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

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


MEMORY_RE = re.compile(r"<memory>(.*?)</memory>", re.DOTALL)


def current_system_prompt() -> str:
    """Промпт Анны + свежие факты из последнего аудита её аккаунта (если он был)."""
    prompt = SYSTEM_PROMPT + BOT_CAPABILITIES
    saved = audit.load_client_audit(DATA_DIR)
    if saved:
        prompt += (
            f"\n\n=== ЖИВЫЕ ДАННЫЕ АККАУНТА @{audit.CLIENT_INSTAGRAM} (аудит ботом от {saved['date']}) ===\n"
            + saved["summary"]
            + "\nОпирайся на эти данные, когда советуешь темы, хуки и форматы. Они свежее, чем аудит выше.\n"
        )
    trend_mem = audit.load_memory(DATA_DIR, "__trends__")
    if trend_mem:
        prompt += (
            f"\n=== ТРЕНДЫ НИШИ (собраны ботом {trend_mem['date']}) ===\n" + trend_mem["summary"]
            + "\nУчитывай их, когда предлагаешь хуки и идеи (если они не устарели — старше недели считай ориентиром, а не фактом).\n"
        )
    facts_text = facts.prompt_block(DATA_DIR)
    if facts_text:
        prompt += (
            "\n=== РЕАЛЬНЫЕ ИСТОРИИ И ФАКТЫ АННЫ (рассказала сама — это правда, используй смело) ===\n"
            + facts_text
            + "\nКогда пишешь сценарии и подписи, в первую очередь строй их на этих историях и фактах: "
            "конкретные детали, сцены, цифры отсюда можно использовать дословно. Пометку [вставь…] ставь, "
            "только если подходящей истории здесь нет.\n"
        )
    return prompt


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
def _web_search_tool() -> dict:
    return {"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES}


async def _create(system: str, messages: list[dict], max_tokens: int, web_search: bool):
    """Запрос к Claude с (необязательным) поиском в интернете.

    Возвращает (текст, stop_reason, источники). Поиск идёт на стороне Anthropic: модель сама решает,
    искать или нет. Долгий поиск API может поставить на паузу (pause_turn) — тогда продолжаем.
    Если поиск выключен в организации Anthropic (400), повторяем запрос без него.
    """
    kwargs = {"model": CLAUDE_MODEL, "max_tokens": max_tokens, "system": system}
    use_search = web_search and WEB_SEARCH_ENABLED
    msgs = list(messages)
    texts: list[str] = []
    sources: dict[str, str] = {}
    for _ in range(4):
        try:
            if use_search:
                response = await client.messages.create(messages=msgs, tools=[_web_search_tool()], **kwargs)
            else:
                response = await client.messages.create(messages=msgs, **kwargs)
        except anthropic.BadRequestError as e:
            if use_search and "web" in str(e).lower() and "search" in str(e).lower():
                logger.warning("Web search недоступен, отвечаю без него: %s", e)
                use_search = False
                continue
            raise
        for block in response.content:
            if block.type == "text":
                texts.append(block.text)
                for c in getattr(block, "citations", None) or []:
                    url = getattr(c, "url", None)
                    if url and url not in sources and len(sources) < 6:
                        sources[url] = (getattr(c, "title", None) or url).strip()
        if response.stop_reason != "pause_turn":
            break
        # продолжение: отдаём паузнутый ответ ассистента обратно как есть
        msgs = msgs + [{"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in response.content]}]
    return "".join(texts), response.stop_reason, sources


def _with_sources(text: str, sources: dict[str, str]) -> str:
    if not sources:
        return text
    missing = {u: t for u, t in sources.items() if u not in text}
    if not missing:
        return text
    lines = [f"• {t[:80]} — {u}" for u, t in list(missing.items())[:5]]
    return text.rstrip() + "\n\n**Источники:**\n" + "\n".join(lines)


def system_with_search() -> str:
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
    d = datetime.date.today()
    return current_system_prompt() + WEB_SEARCH_NOTE.format(today=f"{d.day} {months[d.month - 1]} {d.year}")


async def ask_claude(
    update: Update,
    chat_id: int,
    content: str | list[dict],
    history_note: str | None = None,
    max_tokens: int | None = None,
    web_search: bool = False,
    send: bool = True,
    edit: bool = True,
    history_reply=None,
) -> str | None:
    """Единая точка общения с Claude. Возвращает полный ответ (или None при ошибке).

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
            system = system_with_search() if (web_search and WEB_SEARCH_ENABLED) else current_system_prompt()
            reply_text, stop_reason, sources = await _create(
                system, messages, max_tokens or CLAUDE_MAX_TOKENS, web_search
            )
            if edit and stop_reason != "max_tokens" and needs_editing(reply_text):
                reply_text = await edit_draft(reply_text, max_tokens or CLAUDE_MAX_TOKENS)
        except anthropic.APIStatusError as e:
            logger.exception("Anthropic API error")
            if e.status_code == 529 or e.status_code >= 500:
                msg = "Claude сейчас перегружен. Попробуй ещё раз через минуту 🙏"
            elif e.status_code == 429:
                msg = "Слишком много запросов подряд. Подожди минутку и повтори."
            else:
                msg = f"Ошибка при обращении к агенту ({e.status_code}). Попробуй ещё раз."
            await update.effective_message.reply_text(msg)
            return None
        except Exception:
            logger.exception("Anthropic API error")
            await update.effective_message.reply_text("Не получилось получить ответ. Попробуй ещё раз.")
            return None
        finally:
            typing_task.cancel()

        full_reply = reply_text
        reply_text = MEMORY_RE.sub("", reply_text).strip()  # служебный блок памяти пользователю не показываем
        if send:
            reply_text = _with_sources(reply_text, sources)

        if stop_reason == "max_tokens" and send:
            reply_text += "\n\n_(ответ обрезан по длине — напиши «продолжи», и я допишу)_"

        stored = reply_text
        if history_reply:
            try:
                stored = history_reply(reply_text)
            except Exception:
                pass
        history = history + [
            {"role": "user", "content": note},
            {"role": "assistant", "content": stored},
        ]
        conversations[chat_id] = _sanitize_history(history[-MAX_HISTORY_MESSAGES:])
        _save_conversations()

    if send:
        await send_long(update, reply_text)
    return full_reply


# Признаки того, что в ответе есть контент для публикации (хуки, сценарии, подписи) —
# такие ответы проходят вторую вычитку строгим редактором.
CONTENT_MARKERS_RE = re.compile(r"(\[ХУК|\[ГОЛОС|\[ТЕКСТ НА ЭКРАНЕ|\[ПОДПИСЬ|подпись к посту|сценари[йия]\s*\d|хуки?\s+для)", re.I)
EDITOR_ENABLED = os.environ.get("EDITOR_ENABLED", "1") != "0"


def needs_editing(text: str) -> bool:
    return EDITOR_ENABLED and len(text) > 600 and bool(CONTENT_MARKERS_RE.search(text))


async def fix_lint_issues(text: str, max_tokens: int) -> str:
    """Третий шаг: код находит выдуманные эпизоды и цепочки повторов — модель точечно их исправляет.
    Делаем до 2 раундов; если что-то идёт не так — возвращаем текст как есть."""
    facts_text = facts.prompt_block(DATA_DIR)
    for round_no in (1, 2):
        issues = lint.find_issues(text, facts_text)
        if not issues:
            return text
        logger.info("Проверка (раунд %s): %s проблем — %s", round_no, len(issues), [i[:60] for i in issues])
        try:
            resp = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=FIXER_PROMPT + _editor_facts(),
                messages=[{"role": "user", "content": "ПРОБЛЕМЫ:\n" + "\n".join(f"{i+1}. {x}" for i, x in enumerate(issues))
                           + f"\n\nТЕКСТ:\n\n{text}"}],
            )
            fixed = MEMORY_RE.sub("", "".join(b.text for b in resp.content if b.type == "text")).strip()
            if resp.stop_reason == "max_tokens" or len(fixed) < len(text) * 0.75:
                logger.warning("Фиксер вернул неполный текст — оставляю как было")
                return text
            text = fixed
        except Exception:
            logger.exception("Фиксер не сработал")
            return text
    return text


def _editor_facts() -> str:
    block = facts.prompt_block(DATA_DIR)
    if not block:
        return ""
    return ("\n\nРЕАЛЬНЫЕ ИСТОРИИ И ФАКТЫ АННЫ (рассказала сама — это НЕ выдумка, такие детали НЕ заменяй "
            "на [вставь…] и не удаляй):\n" + block)


async def edit_draft(draft: str, max_tokens: int) -> str:
    """Второй проход: редактор вычищает тройки, выдуманную биографию, повторы призывов.
    Если что-то пошло не так — возвращаем черновик как есть (ответ пользователь получит в любом случае)."""
    memory = MEMORY_RE.search(draft)
    body = MEMORY_RE.sub("", draft).strip()
    try:
        resp = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=EDITOR_PROMPT + _editor_facts(),
            messages=[{"role": "user", "content": f"ЧЕРНОВИК:\n\n{body}"}],
        )
        edited = "".join(b.text for b in resp.content if b.type == "text").strip()
        edited = MEMORY_RE.sub("", edited).strip()
        if resp.stop_reason == "max_tokens" or len(edited) < len(body) * 0.6:
            logger.warning("Редактор вернул неполный текст (%s → %s симв.) — оставляю черновик", len(body), len(edited))
            return draft
        logger.info("Редактор: %s → %s симв.", len(body), len(edited))
        edited = await fix_lint_issues(edited, max_tokens)
        return edited + (f"\n\n{memory.group(0)}" if memory else "")
    except Exception:
        logger.exception("Редактор не сработал — отправляю черновик")
        return draft


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
        "• Анализирую твой Инстаграм и аккаунты конкурентов (кинь ссылку на профиль)\n"
        "• Нахожу, что сейчас в тренде в нише, и пишу хуки под тебя\n"
        "• Разбираю скриншоты статистики (досмотры, сохранения)\n"
        "• Отвечаю на любые вопросы — про контент, бизнес и просто жизнь\n"
        "• Понимаю голосовые и кружки 🎙\n"
        "• Разбираю рилсы и посты по ссылке (Instagram, TikTok, YouTube)\n"
        "• Читаю фото, PDF и сайты\n"
        "• Делаю карусели — готовые картинки-слайды + подпись 🖼 Дизайн опиши словами, пришли скрин-пример или своё фото для обложки\n"
        "• Ищу в интернете свежее: цены, адреса, новости 🔎\n"
        "• Разбираю твои видео: пришли ролик — скажу, что с хуком и что улучшить 🎬\n\n"
        "Просто напиши или наговори, что нужно. Полный список с примерами — /help\n"
        "/trends — что сейчас залетает в нише + 15 хуков для тебя\n"
        "/audit — аудит твоего Инстаграма (или /audit @ник — любого аккаунта)\n"
        "/carousel тема — карусель-картинки для Instagram (дизайн — любыми словами или по скрину)\n"
        "/designs — показать готовые дизайны каруселей\n"
        "/competitors — аккаунты-ориентиры для трендов\n"
        "/memory — что я о тебе помню (добавить: «Запомни: …»)\n"
        "/new — начать новый диалог (забыть контекст)"
    )


HELP_TEXT = (
    "✨ <b>Что я умею</b>\n\n"
    "<b>Карусели</b>\n"
    "• «сделай карусель про …» или /carousel тема\n"
    "• Из своих фото: выдели несколько фото, отправь одним сообщением и подпиши «сделай из этого карусель про …»\n"
    "• С фото из Instagram: «сделай карусель про …, возьми фото из моей инсты»\n"
    "• Дизайн словами: «в стиле old money», «бордовый с золотом», «как море»\n"
    "• Готовые стили: /designs · по примеру: скрин чужой карусели + «в таком стиле»\n"
    "• Рисунок: «сверху нарисуй пальму» · нейрофон: «с нейрофоном: вилла на закате»\n"
    "• Понравилось — «запомни дизайн как мой стиль», потом «карусель в дизайне мой стиль»\n"
    "• Правки: «поменяй слайд 3 на …», «перерисуй темнее»\n\n"
    "<b>Instagram</b>\n"
    "• /audit — аудит твоего аккаунта, /audit @ник — любого открытого\n"
    "• Ссылка на рилс или пост — разберу, почему залетел\n"
    "• /trends — что сейчас залетает в нише + хуки, /competitors — ориентиры\n"
    "• Скрин статистики (охваты, досмотры) — разберу цифры\n\n"
    "<b>Тексты и идеи</b>\n"
    "• Сценарии рилс, хуки, подписи в твоём голосе, проверка и правка текстов\n"
    "• «Запомни: …» — запомню факт или историю, /memory — что я помню\n\n"
    "<b>Ещё</b>\n"
    "• Голосовые и кружки 🎙 · видео-разбор: пришли ролик 🎬\n"
    "• Поиск в интернете: цены, адреса, новости 🔎 · фото, PDF, сайты\n"
    "• /new — начать диалог заново"
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = facts.load(DATA_DIR)
    if not items:
        await update.message.reply_text(
            "Память пока пустая.\n\nНапиши или надиктуй: «Запомни: …» — историю из жизни, факт о себе, "
            "даты ретрита, цены, — и я буду писать сценарии на твоём реальном материале."
        )
        return
    lines = [f"{i}. ({it.get('date', '')}) {it['text'][:300]}{'…' if len(it['text']) > 300 else ''}" for i, it in enumerate(items, 1)]
    text = "🧠 Что я о тебе помню:\n\n" + "\n\n".join(lines) + "\n\nУдалить: /forget номер"
    for chunk in split_text(text):
        await update.message.reply_text(chunk)


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args or [""])[0]
    if not arg.isdigit():
        await update.message.reply_text("Напиши номер записи: /forget 3 (список — /memory)")
        return
    removed = facts.remove(DATA_DIR, int(arg))
    await update.message.reply_text(
        f"Удалила: «{removed['text'][:150]}»" if removed else "Нет записи с таким номером. Список — /memory"
    )


async def remember_fact(update: Update, text: str) -> None:
    n = facts.add(DATA_DIR, text)
    await update.effective_message.reply_text(
        f"🧠 Запомнила (запись №{n}). Теперь буду использовать это в сценариях и подписях.\n"
        "Посмотреть всё — /memory"
    )


async def new_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conversations[update.effective_chat.id] = []
    _save_conversations()
    await update.message.reply_text("Начали с чистого листа ✨ О чём поговорим?")


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Доступна всем, чтобы человек мог узнать свой ID для списка доступа.
    await update.message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


# ---------------------------------------------------------------------------
# Аудит аккаунта
# ---------------------------------------------------------------------------
AUDIT_INTENT_RE = re.compile(
    r"(проанализ|анализ|аудит|разбе?р|разобрать|посмотри|оцени)\w*\s+(\w+\s+){0,3}(мой\s+|мою\s+|наш\w*\s+)?"
    r"(аккаунт|акк|профил|инст|инстаграм|страниц|блог)",
    re.I,
)
AUDIT_COOLDOWN: dict[str, float] = {}


async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = " ".join(context.args or []).strip()
    handle = audit.normalize_handle(arg) if arg else audit.CLIENT_INSTAGRAM
    if not handle:
        await update.message.reply_text("Не поняла ник. Напиши так: /audit или /audit @nickname")
        return
    await run_audit(update, handle)


async def run_audit(update: Update, handle: str, extra: str = "") -> None:
    """Выгружает аккаунт, считает статистику и просит Claude сделать аудит."""
    handle = handle.lstrip("@")
    is_client = handle.lower() == audit.CLIENT_INSTAGRAM.lower()
    status = await update.effective_message.reply_text(
        f"🔎 Выгружаю @{handle}: последние {audit.AUDIT_POSTS} публикаций с цифрами. Это займёт 1–3 минуты ⏳"
    )
    try:
        data = await audit.fetch_account(handle)
        stats = audit.build_stats(data)
    except audit.AuditError as e:
        await status.edit_text(f"Не получилось проанализировать аккаунт: {e}")
        return
    except Exception:
        logger.exception("Audit fetch error")
        await status.edit_text("Не получилось выгрузить аккаунт. Попробуй ещё раз через пару минут.")
        return

    try:
        await status.edit_text(
            f"✅ Выгрузила @{handle}: {len(stats['rows'])} публикаций · подписчиков: {social._fmt_num(stats['followers'])}\n"
            "Анализирую как продюсер..."
        )
    except BadRequest:
        pass

    summary = audit.stats_text(stats)
    content: list[dict] = [{"type": "text", "text": summary}]
    for label, data_bytes, mime in await audit.covers(stats):
        content.append({"type": "text", "text": f"Обложка — {label}"})
        content.append(social.image_block(data_bytes, mime))

    who = "аккаунт Анны @" + handle if is_client else f"аккаунт @{handle} (конкурент/референс — что из этого взять Анне)"
    hooks_for = "в голосе Анны, под её аудиторию" if is_client else "адаптированных под Анну и её позиционирование, а не копии"
    task = AUDIT_PROMPT.format(who=who, hooks_for=hooks_for)
    if extra and not AUDIT_INTENT_RE.fullmatch(extra):
        task += f"\n\nДополнительный запрос: {extra}"
    content.append({"type": "text", "text": task})

    note = summary[:7000] + f"\n\n[Аудит аккаунта @{handle}]"
    reply = await ask_claude(update, update.effective_chat.id, content, history_note=note, max_tokens=8000)
    if reply and is_client:
        m = MEMORY_RE.search(reply)
        memory = m.group(1).strip() if m else ""
        base_facts = (
            f"Подписчиков: {social._fmt_num(stats['followers'])}; медиана просмотров рилсов: "
            f"{social._fmt_num(stats['median_plays'])}; частота: ~{stats['per_week']} публикаций/нед."
        )
        audit.save_audit(DATA_DIR, handle, base_facts + "\n" + memory)


# ---------------------------------------------------------------------------
# Тренды ниши
# ---------------------------------------------------------------------------
TRENDS_INTENT_RE = re.compile(
    r"(тренд|залета|залетел|виральн|вирусн|в топе|что сейчас (снимают|заходит|популярн)|"
    r"популярн\w*\s+(сейчас\s+)?(хук|тем|ролик|рилс|reels))",
    re.I,
)


async def trends_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    force = bool(args and args[0].lower() in ("обнови", "refresh", "!"))
    if force:
        args = args[1:]
    hashtags = [a.lstrip("#").lower() for a in args if a.strip("#")] or None
    await run_trends(update, hashtags=hashtags, force=force)


async def competitors_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/competitors — список; /competitors @a @b — добавить; /competitors удалить @a; /competitors очистить."""
    args = " ".join(context.args or []).strip()
    current = trends.load_competitors(DATA_DIR)
    low = args.lower()
    if low in ("очистить", "clear"):
        current = []
        trends.save_competitors(DATA_DIR, current)
    elif low.startswith(("удалить", "убрать", "remove", "-")):
        drop = {h.lower() for h in trends.parse_handles(re.sub(r"^(удалить|убрать|remove|-)", "", args, flags=re.I))}
        current = [h for h in current if h.lower() not in drop]
        trends.save_competitors(DATA_DIR, current)
    elif args:
        new = [h for h in trends.parse_handles(args) if h.lower() not in {c.lower() for c in current}]
        current = (current + new)[: trends.MAX_ACCOUNTS]
        trends.save_competitors(DATA_DIR, current)
    listing = "\n".join(f"• @{h}" for h in current) or "пока пусто"
    await update.message.reply_text(
        f"Аккаунты-ориентиры для трендов ({len(current)}/{trends.MAX_ACCOUNTS}):\n{listing}\n\n"
        "Добавить: /competitors @ник1 @ник2\nУбрать: /competitors удалить @ник\n"
        "По ним бот ищет «залёты» — ролики, набравшие в 2+ раза больше обычного."
    )


async def run_trends(update: Update, hashtags: list[str] | None = None, topic: str = "", force: bool = False) -> None:
    status = await update.effective_message.reply_text(
        "📈 Собираю, что сейчас залетает в нише: хэштеги + аккаунты-ориентиры. Это займёт 1–3 минуты ⏳"
    )
    try:
        result = await trends.collect(DATA_DIR, hashtags=hashtags, force=force)
    except trends.TrendsError as e:
        await status.edit_text(f"Не получилось собрать тренды: {e}")
        return
    except Exception:
        logger.exception("Trends error")
        await status.edit_text("Не получилось собрать тренды. Попробуй ещё раз через пару минут.")
        return

    try:
        hint = "" if result["competitors"] else "\n💡 Добавь аккаунты-ориентиры (/competitors @ник) — тренды станут точнее."
        await status.edit_text(
            f"✅ Нашла {len(result['rows'])} свежих залетевших рилсов"
            + (" (из кеша, обновить: /trends обнови)" if result.get("cached") else "")
            + ". Придумываю хуки для Анны..." + hint
        )
    except BadRequest:
        pass

    summary = trends.trends_text(result)
    content: list[dict] = [{"type": "text", "text": summary}]
    for label, data_bytes, mime in await trends.covers(result):
        content.append({"type": "text", "text": f"Обложка {label}"})
        content.append(social.image_block(data_bytes, mime))
    topic_line = f"\n\nЗапрос Анны: {topic}" if topic else ""
    content.append({"type": "text", "text": TRENDS_PROMPT.format(topic=topic_line)})

    note = summary[:7000] + "\n\n[Тренды ниши и хуки]" + (f" {topic}" if topic else "")
    reply = await ask_claude(update, update.effective_chat.id, content, history_note=note, max_tokens=8000)
    if reply:
        m = MEMORY_RE.search(reply)
        if m:
            audit.save_audit(DATA_DIR, "__trends__", m.group(1).strip())


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


SCRIPT_INTENT_RE = re.compile(r"(сценари|рилс|reels|хук|ролик|подпис\w*\s+к\s+пост|что\s+снять|идеи\s+для\s+(видео|контента))", re.I)


async def process_user_text(update: Update, user_text: str) -> None:
    """Общая обработка текста — и набранного, и распознанного из голосового/кружка."""
    chat_id = update.effective_chat.id
    urls = URL_RE.findall(user_text)[:MAX_LINKS_PER_MESSAGE]

    if await handle_design_commands(update, user_text):
        return

    to_remember = facts.extract(user_text)
    if to_remember:
        await remember_fact(update, to_remember)
        return

    if not urls and CAROUSEL_INTENT_RE.search(user_text):
        await run_carousel(update, user_text)
        return

    if not urls:
        if TRENDS_INTENT_RE.search(user_text):
            await run_trends(update, topic=user_text)
            return
        if AUDIT_INTENT_RE.search(user_text):
            await run_audit(update, audit.CLIENT_INSTAGRAM, extra=user_text)
            return
        if SCRIPT_INTENT_RE.search(user_text):
            # формат-требования видит только модель; в историю идёт чистый текст Анны
            await ask_claude(update, chat_id, user_text + SCRIPT_FORMAT_NOTE, history_note=user_text,
                             max_tokens=8000, web_search=True)
            return
        await ask_claude(update, chat_id, user_text, web_search=True)
        return

    # Ссылка на профиль Instagram — аудит аккаунта
    profile = next((h for h in (audit.detect_profile(u) for u in urls) if h), None)
    if profile:
        await run_audit(update, profile, extra=URL_RE.sub("", user_text).strip())
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


# ---------------------------------------------------------------------------
# Карусели
# ---------------------------------------------------------------------------
CAROUSEL_INTENT_RE = re.compile(r"(карусел|слайд)", re.I)


async def carousel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    topic = " ".join(context.args or []).strip()
    if not topic:
        await update.message.reply_text(
            "Напиши тему или свой текст, например:\n"
            "/carousel 5 причин, почему деньги уходят быстрее, чем приходят\n\n"
            "Можно и без команды: «сделай карусель про …».\n\n"
            "🎨 Дизайн — как хочешь:\n"
            "• словами: «в стиле old money», «бордовый с золотом», «как море на Мальдивах»\n"
            "• готовый: /designs — покажу все стили\n"
            "• по примеру: пришли скрин понравившейся карусели с подписью «сделай карусель про … в таком стиле»\n"
            "• со своим фото: пришли фото с подписью «карусель про …» — оно станет обложкой\n"
            "• нейрофон: «… с нейрофоном: вилла на Бали на закате»\n\n"
            "Понравился дизайн — «запомни дизайн как мой шёлк», потом «карусель про … в дизайне мой шёлк». "
            "Сохранить как «основной» — будет по умолчанию."
        )
        return
    await run_carousel(update, "Сделай карусель: " + topic)


def _carousel_history(reply: str) -> str:
    try:
        data = carousel.parse(reply)
        return "[Карусель]\n" + carousel.as_text(data) + "\n\n" + carousel.design_note(data.get("design"))
    except Exception:
        return reply


IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-1")
AI_BG_SUFFIX = (
    ", vertical 4:5 composition, photorealistic editorial photography, soft natural light, calm areas of "
    "negative space for overlay text, high-end lifestyle aesthetic. No people, no faces, no text, no letters, "
    "no logos, no watermark."
)


async def generate_ai_background(prompt: str) -> tuple[bytes | None, str]:
    """Фон-картинка от OpenAI. Возвращает (байты, причина_ошибки)."""
    if openai_client is None:
        return None, "нет ключа OpenAI"
    full = prompt.strip()[:900] + AI_BG_SUFFIX
    errors = []
    attempts = [
        dict(model=IMAGE_MODEL, prompt=full, size="1024x1536", quality="medium", n=1),
        dict(model="dall-e-3", prompt=full, size="1024x1792", quality="standard", response_format="b64_json", n=1),
    ]
    for kw in attempts:
        try:
            r = await asyncio.wait_for(openai_client.images.generate(**kw), timeout=150)
            item = r.data[0]
            if getattr(item, "b64_json", None):
                return base64.b64decode(item.b64_json), ""
            if getattr(item, "url", None):
                async with httpx.AsyncClient(timeout=60) as h:
                    resp = await h.get(item.url)
                    resp.raise_for_status()
                    return resp.content, ""
        except Exception as e:
            logger.warning("Image generation failed (%s): %s", kw["model"], e)
            errors.append(f"{kw['model']}: {str(e)[:120]}")
    return None, "; ".join(errors) or "неизвестная ошибка"


MAX_CAROUSEL_PHOTOS = 14
_PH = r"(фото|фотк|фоточ|снимк|картинк|кадр)"
_IG = r"(инст|insta|профил|ленты|аккаунт|страниц)"
INSTA_PHOTOS_RE = re.compile(rf"{_PH}[^.!?\n]{{0,50}}{_IG}|{_IG}[^.!?\n]{{0,50}}{_PH}", re.I)
_insta_cache: dict = {"t": 0.0, "photos": []}


def _preview_jpeg(data: bytes, side: int = 900) -> bytes:
    """Уменьшенная копия фото для Claude (экономим токены)."""
    from PIL import Image as _Image, ImageOps as _ImageOps
    im = _Image.open(io.BytesIO(data))
    try:
        im = _ImageOps.exif_transpose(im)
    except Exception:
        pass
    im = im.convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def _assign_photos(raw: dict, photos: list[bytes], n_slides: int) -> list:
    """Какое фото на какой слайд: из design.slide_photos (номера с 1) или старое photo_use=cover."""
    per = [None] * n_slides
    if not photos or raw.get("photo_use") == "reference":
        return per
    sp = raw.get("slide_photos")
    if isinstance(sp, list) and sp:
        used = set()
        for i, v in enumerate(sp[:n_slides]):
            try:
                k = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= k <= len(photos) and k not in used:
                per[i] = photos[k - 1]
                used.add(k)
    elif raw.get("photo_use") == "cover" or (len(photos) == 1 and raw.get("photo_use") != "reference"):
        per[0] = photos[0]
    return per


async def fetch_insta_photos(limit: int = 12) -> tuple[list[bytes], str]:
    """Последние фото из Instagram Анны через Apify (кэш на час)."""
    import time as _time
    if _insta_cache["photos"] and _time.time() - _insta_cache["t"] < 3600:
        return _insta_cache["photos"], ""
    try:
        posts = await audit._apify({"directUrls": [f"https://www.instagram.com/{audit.CLIENT_INSTAGRAM}/"],
                                    "resultsType": "posts", "resultsLimit": 24, "addParentData": False}, timeout=180)
    except Exception as e:
        return [], str(e)[:150]
    urls: list[str] = []
    videos: list[str] = []
    for p in posts:
        if not isinstance(p, dict) or p.get("error"):
            continue
        kind = (p.get("type") or "").lower()
        if kind == "video":
            if p.get("displayUrl"):
                videos.append(p["displayUrl"])
            continue
        cand = [c.get("displayUrl") for c in (p.get("childPosts") or []) if isinstance(c, dict) and (c.get("type") or "").lower() != "video"]
        cand = [u for u in cand if u] or list(p.get("images") or []) or [p.get("displayUrl")]
        urls += [u for u in cand[:2] if u]
    urls = (urls + videos)[: limit + 6]
    out: list[bytes] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as h:
        for u in urls:
            if len(out) >= limit:
                break
            try:
                r = await h.get(u)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                    out.append(r.content)
            except Exception:
                continue
    if not out:
        return [], "не нашла фото в последних публикациях"
    _insta_cache.update(t=_time.time(), photos=out)
    return out, ""


async def run_carousel(update: Update, user_text: str, image: bytes | None = None, image_mime: str = "image/jpeg",
                       images: list[bytes] | None = None) -> None:
    """Тексты и дизайн пишет Claude (JSON), картинки рисует carousel.py, шлём альбомом + файлами + подпись."""
    chat_id = update.effective_chat.id
    status = await update.effective_message.reply_text("🎨 Пишу тексты слайдов и собираю дизайн… ⏳")
    prompt = CAROUSEL_PROMPT.replace("{saved_designs}", design_store.prompt_block(DATA_DIR))
    prev = design_store.last(DATA_DIR, chat_id)
    if prev:
        prompt += "\n\nДизайн прошлой карусели: " + json.dumps(prev, ensure_ascii=False)
    photos: list[bytes] = list(images or [])
    if image is not None:
        photos.insert(0, image)
    from_insta = False
    if INSTA_PHOTOS_RE.search(user_text or ""):
        try:
            await status.edit_text("📸 Смотрю фото в Instagram Анны… это до минуты ⏳")
        except Exception:
            pass
        insta, err = await fetch_insta_photos()
        if insta:
            photos += insta
            from_insta = True
        else:
            await update.effective_message.reply_text(f"Не получилось взять фото из Instagram ({err}). Сделаю без них.")
        try:
            await status.edit_text("🎨 Пишу тексты слайдов и собираю дизайн… ⏳")
        except Exception:
            pass
    photos = photos[:MAX_CAROUSEL_PHOTOS]
    if photos:
        content = []
        for i, ph in enumerate(photos, 1):
            content.append({"type": "text", "text": f"Фото {i}:"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.b64encode(_preview_jpeg(ph)).decode()}})
        own = len(photos) - (len(insta) if from_insta else 0)
        info = f"[Приложено фото: {len(photos)}."
        if from_insta:
            info += f" Фото {own + 1}–{len(photos)} — последние снимки из Instagram Анны, выбери подходящие для слайдов."
        info += "]"
        content.append({"type": "text", "text": (user_text or "Сделай карусель") + "\n\n" + info + "\n\n" + prompt})
        note = f"[Фото: {len(photos)}] {user_text}"
    else:
        content = user_text + "\n\n" + prompt
        note = user_text
    reply = await ask_claude(
        update, chat_id, content, history_note=note,
        max_tokens=12000, web_search=True, send=False, edit=False, history_reply=_carousel_history,
    )
    if reply is None:
        return
    warn = ""
    try:
        try:
            data = carousel.parse(reply)
        except carousel.CarouselError:
            # кривой JSON (обрезан, лишний текст, сломан SVG) — просим Claude починить, без истории
            logger.warning("Carousel JSON broken, asking Claude to repair")
            fixed, _, _ = await _create(
                "Ты чинишь JSON. Верни ТОЛЬКО валидный JSON без пояснений.",
                [{"role": "user", "content": "Исправь и верни валидный JSON карусели в том же формате "
                  "({\"slides\": [...], \"caption\": \"...\", \"design\": {...}}). Если в design есть "
                  "illustration и она сломана — просто убери её. Вот ответ:\n\n" + reply[:60000]}],
                9000, False)
            data = carousel.parse(fixed)
        raw = data.get("design") or {}
        d = carousel.resolve(raw, saved=design_store.saved(DATA_DIR))
        slide_photos = _assign_photos(raw, photos, len(data["slides"]))
        ai_bg = None
        if d["background"].get("type") == "ai":
            try:
                await status.edit_text("🖼 Генерирую нейрофон… это до минуты ⏳")
            except Exception:
                pass
            ai_bg, err = await generate_ai_background(str(raw.get("ai_prompt") or d.get("ai_prompt") or user_text))
            if ai_bg is None:
                warn = f"\n\n⚠️ Нейрофон не получился ({err[:200]}) — нарисовала без него."
        try:
            images = await asyncio.to_thread(carousel.render, data, d, None, ai_bg, slide_photos)
        except Exception:
            # дизайн не отрисовался — не подводим: рисуем те же тексты и фото в базовом стиле
            logger.exception("Carousel render failed, falling back to the default design")
            d = carousel.resolve({"preset": carousel.DEFAULT_PRESET})
            images = await asyncio.to_thread(carousel.render, data, d, None, None, slide_photos)
            warn += "\n\n⚠️ Выбранный дизайн не отрисовался, сделала в базовом стиле. Попроси другой стиль, если нужно."
    except Exception as e:
        logger.exception("Carousel error")
        await status.edit_text("Не получилось собрать карусель — попробуй ещё раз через минуту или переформулируй запрос 🙏")
        return
    design_store.set_last(DATA_DIR, chat_id, carousel.public(d))
    try:
        await status.delete()
    except Exception:
        pass
    n = len(images)
    await update.effective_message.reply_media_group([InputMediaPhoto(img) for img in images])
    # те же слайды файлами — без сжатия Telegram, для загрузки в Instagram в полном качестве
    await update.effective_message.reply_media_group(
        [InputMediaDocument(img, filename=f"slide_{i:02d}.png") for i, img in enumerate(images, 1)]
    )
    label = d.get("label") if raw.get("preset") and len(raw) <= 2 else None
    if raw.get("saved"):
        label = f"сохранённый «{raw['saved']}»"
    design_line = f"🎨 Дизайн: {label}" if label else "🎨 Дизайн: свой, собран под запрос"
    await send_long(
        update,
        f"✅ Карусель готова: {n} слайдов (сверху — превью, ниже — файлы в полном качестве для Instagram).\n"
        + design_line + warn + "\n\n"
        + carousel.as_text(data)
        + "\n\n_Поправить: «в карусели поменяй слайд 3 на …», «перерисуй в изумруде / темнее / с золотом». "
          "Понравился дизайн — «запомни дизайн как …». Все стили — /designs._",
    )


# ---------------------------------------------------------------------------
# Дизайны: показать, сохранить, удалить
# ---------------------------------------------------------------------------
DESIGN_LIST_RE = re.compile(r"^\s*(покажи|какие|список)\b.{0,25}\b(дизайн|стил|оформлени)", re.I)
DESIGN_SAVE_RE = re.compile(
    r"^\s*(запомни|сохрани)\s+(этот\s+|этот\s+же\s+|последний\s+)?(дизайн|стиль|оформление)(\s+карусели)?"
    r"(\s*(как|под\s+названием|:|—|-)?\s*(?P<name>.+?))?\s*$", re.I)
DESIGN_DELETE_RE = re.compile(r"^\s*(удали|забудь)\s+(дизайн|стиль)\s+(?P<name>.+?)\s*$", re.I)
_preview_cache: dict[str, bytes] = {}


async def handle_design_commands(update: Update, text: str) -> bool:
    t = (text or "").strip()
    if len(t) > 120:
        return False
    m = DESIGN_SAVE_RE.match(t)
    if m:
        last = design_store.last(DATA_DIR, update.effective_chat.id)
        if not last:
            await update.effective_message.reply_text("Пока нечего запоминать — сначала сделаем карусель 🙂")
            return True
        name = (m.group("name") or "основной").strip()
        key = design_store.save_design(DATA_DIR, name, last)
        extra = " Теперь он будет по умолчанию." if key == "основной" else f" Пиши: «карусель про … в дизайне {key}»."
        await update.effective_message.reply_text(f"💾 Запомнила дизайн как «{key}».{extra}")
        return True
    m = DESIGN_DELETE_RE.match(t)
    if m:
        ok = design_store.delete(DATA_DIR, m.group("name"))
        await update.effective_message.reply_text("🗑 Удалила." if ok else "Такого сохранённого дизайна нет. Список — /designs")
        return True
    if DESIGN_LIST_RE.match(t):
        await show_designs(update)
        return True
    return False


async def designs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_designs(update)


async def show_designs(update: Update) -> None:
    status = await update.effective_message.reply_text("Рисую примеры дизайнов… ⏳")
    names = list(carousel.PRESETS)
    try:
        missing = [n for n in names if n not in _preview_cache]
        if missing:
            rendered = await asyncio.to_thread(lambda: {n: carousel.preview_cover(n) for n in missing})
            _preview_cache.update(rendered)
    except Exception as e:
        logger.exception("Preview error")
        await status.edit_text(f"Не получилось нарисовать примеры ({e}).")
        return
    try:
        await status.delete()
    except Exception:
        pass
    for chunk in (names[:10], names[10:]):
        if chunk:
            await update.effective_message.reply_media_group(
                [InputMediaPhoto(_preview_cache[n], caption=f"{carousel.PRESETS[n]['label']} — «{n}»") for n in chunk]
            )
    saved = design_store.saved(DATA_DIR)
    lines = [f"• {carousel.PRESETS[n]['label']} — {n}" for n in names]
    txt = "🎨 Готовые дизайны:\n" + "\n".join(lines)
    if saved:
        txt += "\n\n💾 Твои сохранённые: " + ", ".join(f"«{k}»" for k in saved)
    txt += ("\n\nПиши: «карусель про деньги в стиле emerald» или опиши свой: «бордовый бархат с золотом», "
            "«как море». Можно прислать скрин-пример или фото для обложки.")
    await update.effective_message.reply_text(txt)


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
            "ffmpeg", "-y", "-threads", "2", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", dst,
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
# Видео: продюсерский разбор ролика (речь + кадры)
# ---------------------------------------------------------------------------
VIDEO_MAX_SECONDS = int(os.environ.get("VIDEO_MAX_SECONDS", "900"))  # до 15 минут


async def save_telegram_file(tg_file, dst: str) -> None:
    """Сохраняет файл Telegram на диск, не держа его целиком в памяти."""
    file_path = tg_file.file_path
    if file_path and os.path.isabs(file_path) and os.path.exists(file_path):
        shutil.copyfile(file_path, dst)
    else:
        await tg_file.download_to_drive(dst)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Видео → расшифровка речи + кадры → продюсерский разбор от Claude."""
    msg = update.message
    video = msg.video or msg.document
    if video is None:
        return

    file_size = getattr(video, "file_size", None)
    if file_size and file_size > TELEGRAM_BOT_FILE_LIMIT:
        await msg.reply_text(f"Файл весит {file_size / 1048576:.0f} МБ — это больше лимита 2 ГБ. Пришли покороче.")
        return

    status = await msg.reply_text("🎬 Смотрю и слушаю ролик... Это займёт до минуты ⏳")
    await update.effective_chat.send_action("typing")
    caption = (msg.caption or "").strip()

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "video.mp4")
        try:
            tg_file = await context.bot.get_file(video.file_id)
            await save_telegram_file(tg_file, src)
            duration = await social.video_duration(src)
        except Exception:
            logger.exception("Video download error")
            await status.edit_text("Не получилось скачать видео. Попробуй отправить ещё раз.")
            return

        if duration > VIDEO_MAX_SECONDS:
            await status.edit_text(
                f"Видео длится {int(duration // 60)} мин — слишком долго для разбора. Пришли фрагмент до 15 минут."
            )
            return

        frames = await social.extract_frames(src)
        transcript = ""
        if openai_client is not None:
            try:
                audio = await extract_audio_mp3_from_path(src)
                if audio:
                    transcript = await transcribe(audio, "audio.mp3")
            except Exception:
                logger.exception("Video transcription error")

    if not frames and not transcript:
        await status.edit_text("Не получилось разобрать видео — возможно, файл повреждён. Попробуй другой.")
        return

    try:
        await status.edit_text("✅ Посмотрела ролик. Разбираю как продюсер...")
    except BadRequest:
        pass

    summary_lines = [f"[Видео от Анны · {duration:.0f} сек]"]
    summary_lines.append(
        f"Расшифровка речи:\n{transcript[:6000]}" if transcript
        else "Речи в видео нет (или не распознана) — смысл передаётся картинкой и текстом на экране."
    )
    if frames:
        summary_lines.append(f"Ниже — {len(frames)} кадров из видео по порядку (первый — начало ролика, хук).")
    summary = "\n".join(summary_lines)

    content: list[dict] = [{"type": "text", "text": summary}]
    content += [social.image_block(fr) for fr in frames]
    content.append({"type": "text", "text": caption or VIDEO_REVIEW_PROMPT})

    note = summary[:6000] + (f"\n\n{caption}" if caption else "\n\n[Разбери моё видео]")
    await ask_claude(update, update.effective_chat.id, content, history_note=note)


async def extract_audio_mp3_from_path(path: str) -> bytes | None:
    """Звук из видео в лёгкий mp3 для Whisper — прямо с диска, без загрузки видео в память."""
    dst = path + ".mp3"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-threads", "2", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", dst,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(dst):
        logger.info("В видео нет звука или ffmpeg не смог его достать: %s", stderr.decode(errors="ignore")[-300:])
        return None
    with open(dst, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Фото и документы
# ---------------------------------------------------------------------------
DEFAULT_IMAGE_PROMPT = (
    "Посмотри на это изображение и прокомментируй его содержательно — как продюсер, "
    "если это релевантно контенту Анны, или просто по существу, если это что-то другое."
)
SUPPORTED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


_album_buffers: dict = {}
ALBUM_WAIT = 2.5


async def _collect_album(update: Update, data: bytes, caption: str) -> None:
    """Несколько фото одним сообщением Telegram присылает по одному — собираем их вместе."""
    gid = update.message.media_group_id
    buf = _album_buffers.setdefault(gid, {"photos": [], "caption": "", "update": update, "task": None})
    buf["photos"].append((update.message.message_id, data))
    if caption and not buf["caption"]:
        buf["caption"] = caption
        buf["update"] = update
    if buf["task"]:
        buf["task"].cancel()
    buf["task"] = asyncio.create_task(_flush_album(gid))


async def _flush_album(gid) -> None:
    try:
        await asyncio.sleep(ALBUM_WAIT)
    except asyncio.CancelledError:
        return
    buf = _album_buffers.pop(gid, None)
    if not buf:
        return
    photos = [d for _, d in sorted(buf["photos"], key=lambda x: x[0])]
    caption, update = buf["caption"], buf["update"]
    try:
        if caption and CAROUSEL_INTENT_RE.search(caption):
            await run_carousel(update, caption, images=photos)
            return
        content = []
        for i, ph in enumerate(photos[:MAX_CAROUSEL_PHOTOS], 1):
            content.append({"type": "text", "text": f"Фото {i}:"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.b64encode(_preview_jpeg(ph, 1400)).decode()}})
        content.append({"type": "text", "text": caption or DEFAULT_IMAGE_PROMPT})
        await ask_claude(update, update.effective_chat.id, content, history_note=f"[Фото: {len(photos)}] {caption}")
    except Exception:
        logger.exception("Album processing error")
        await update.effective_message.reply_text("Не получилось обработать фото. Попробуй ещё раз.")


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

    if update.message.media_group_id:
        await _collect_album(update, data, caption)
        return

    if caption and CAROUSEL_INTENT_RE.search(caption):
        await run_carousel(update, caption, image=data, image_mime="image/jpeg")
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

    if is_image and update.message.media_group_id:
        await _collect_album(update, data, caption)
        return
    if is_image and caption and CAROUSEL_INTENT_RE.search(caption):
        await run_carousel(update, caption, image=data, image_mime=mime)
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
    app.add_handler(CommandHandler("help", guarded(help_command)))
    app.add_handler(CommandHandler("memory", guarded(memory_command)))
    app.add_handler(CommandHandler("forget", guarded(forget_command)))
    app.add_handler(CommandHandler("audit", guarded(audit_command)))
    app.add_handler(CommandHandler(["trends", "hooks"], guarded(trends_command)))
    app.add_handler(CommandHandler("competitors", guarded(competitors_command)))
    app.add_handler(CommandHandler(["carousel", "karusel"], guarded(carousel_command)))
    app.add_handler(CommandHandler(["designs", "design"], guarded(designs_command)))
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
