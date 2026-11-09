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
import time
from collections import defaultdict

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import anthropic
from openai import AsyncOpenAI

from prompt import BOT_CAPABILITIES
from prompt import HITS_PROMPT
from prompt import SYSTEM_PROMPT as ANNA_PROMPT, LINK_ANALYSIS_PROMPT, VIDEO_REVIEW_PROMPT, AUDIT_PROMPT, TRENDS_PROMPT, EDITOR_PROMPT, SCRIPT_FORMAT_NOTE, FIXER_PROMPT, CAROUSEL_PROMPT, WEB_SEARCH_NOTE
import social
import audit
import trends
import facts
import lint
import carousel
import design_store
import montage
import brief
import iginsights
import vidiq
import anna_profile
import launches
import datetime
from telegram import InputMediaPhoto, InputMediaDocument, InlineKeyboardButton, InlineKeyboardMarkup

logging.basicConfig(level=logging.INFO)
# httpx пишет в лог каждый запрос к Telegram вместе с токеном бота в URL — глушим
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # нужен для распознавания голосовых и кружков
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "4000"))
# Дешёвая модель для простых вопросов (контент всегда пишет CLAUDE_MODEL). Выключить: FAST_MODEL=off
FAST_MODEL = os.environ.get("FAST_MODEL", "claude-haiku-4-5")
# Поиск в интернете (встроенный инструмент Claude): $10 за 1000 поисков + токены результатов.
WEB_SEARCH_ENABLED = os.environ.get("WEB_SEARCH_ENABLED", "1") != "0"
WEB_SEARCH_MAX_USES = int(os.environ.get("WEB_SEARCH_MAX_USES", "2"))

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

SYSTEM_PROMPT = ANNA_PROMPT + anna_profile.PROFILE + FORMAT_NOTE

# ---------------------------------------------------------------------------
# История переписки
# ---------------------------------------------------------------------------
# DATA_DIR указывает на подключённый в Railway Volume (переживает redeploy).
DATA_DIR = os.environ.get("DATA_DIR", "/data")
HISTORY_FILE = os.path.join(DATA_DIR, "conversations.json")
MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", "8"))  # сколько последних сообщений на чат храним и передаём модели (было 40 — дорого)


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


def _italics(text: str) -> str:
    """*текст* → <i>текст</i>, но не трогает 2*3*4 и звёздочки с пробелами внутри."""
    out, pos = [], 0
    for m in re.finditer(r"\*([^*\n]+)\*", text):
        a, b = m.start(), m.end()
        before = text[a - 1] if a else " "
        after = text[b] if b < len(text) else " "
        inner = m.group(1)
        if before.isalnum() or after.isalnum() or inner[0].isspace() or inner[-1].isspace():
            continue
        out.append(text[pos:a])
        out.append(f"<i>{inner}</i>")
        pos = b
    out.append(text[pos:])
    return "".join(out)


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
    # *курсив* (одиночные звёздочки; маркеры списков уже заменены на «•»)
    text = _italics(text)
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
# Кэширование промпта: большая неизменная инструкция (prompt.py, ~15–20 тыс. токенов) и история
# переписки кэшируются на стороне Anthropic. Повторное чтение из кэша стоит 10% от обычной цены,
# запись в кэш — 125% (TTL 5 минут, продлевается при каждом попадании) или 200% (TTL 1 час).
# Выключить: PROMPT_CACHE=0. По умолчанию кэш на 1 час (максимум у Anthropic); 5 минут: PROMPT_CACHE_TTL=5m.
PROMPT_CACHE = os.environ.get("PROMPT_CACHE", "1") != "0"
PROMPT_CACHE_TTL = os.environ.get("PROMPT_CACHE_TTL", "1h")


def _cache_control(ttl: str | None = None) -> dict:
    cc = {"type": "ephemeral"}
    if (ttl or PROMPT_CACHE_TTL) == "1h":
        cc["ttl"] = "1h"
    return cc


def _system_blocks(system: str):
    """Системный промпт как блок с меткой кэша (или как есть, если кэш выключен)."""
    if not PROMPT_CACHE or not system:
        return system
    return [{"type": "text", "text": system, "cache_control": _cache_control()}]


def _cache_last_message(messages: list[dict]) -> list[dict]:
    """Ставит метку кэша на последнее сообщение пользователя: на следующем запросе
    вся история до него читается из кэша. Исходный список не меняем."""
    if not PROMPT_CACHE or not messages or messages[-1].get("role") != "user":
        return messages
    msgs = list(messages)
    content = msgs[-1]["content"]
    # Большие разовые запросы (сценарии, сводки, видео, картинки) в историю сохраняются коротко —
    # следующий запрос их кэш не прочитает, а запись в кэш дороже обычного входа. Их не кэшируем.
    if isinstance(content, list) or len(str(content)) > 4000:
        return messages
    if isinstance(content, str):
        if not content.strip():
            return messages
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list) and content:
        blocks = [dict(b) if isinstance(b, dict) else b for b in content]
        if not isinstance(blocks[-1], dict) or blocks[-1].get("type") not in ("text", "image", "document"):
            return messages
    else:
        return messages
    blocks[-1] = {**blocks[-1], "cache_control": _cache_control("5m")}  # переписка: 5 минут (запись 125%, а не 200%)
    msgs[-1] = {**msgs[-1], "content": blocks}
    return msgs


# ---------------------------------------------------------------------------
# Учёт расходов на Claude (оценка по токенам) + предупреждения владельцу
# ---------------------------------------------------------------------------
_PRICES = {  # $ за 1M токенов: вход, из кэша, запись в кэш (1ч), выход
    "haiku": (1.0, 0.10, 2.0, 5.0),
    "sonnet": (3.0, 0.30, 6.0, 15.0),
    "opus": (5.0, 0.50, 10.0, 25.0),
}
HEAVY_DAILY_LIMIT = int(os.environ.get("HEAVY_DAILY_LIMIT", "5"))
LOW_BALANCE_USD = float(os.environ.get("LOW_BALANCE_USD", "2"))
_pending_alerts: list[str] = []


def _spend_path() -> str:
    return os.path.join(DATA_DIR, "spend.json")


def _load_spend() -> dict:
    try:
        with open(_spend_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"total": 0.0, "days": {}}


def _save_spend(d: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_spend_path(), "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        logger.exception("spend save error")


def _add_spend(model: str, u) -> None:
    key = next((k for k in _PRICES if k in (model or "").lower()), "sonnet")
    p_in, p_cr, p_cw, p_out = _PRICES[key]
    usd = ((getattr(u, "input_tokens", 0) or 0) * p_in + (getattr(u, "cache_read_input_tokens", 0) or 0) * p_cr
           + (getattr(u, "cache_creation_input_tokens", 0) or 0) * p_cw + (getattr(u, "output_tokens", 0) or 0) * p_out) / 1e6
    d = _load_spend()
    day = datetime.datetime.now(audit.LOCAL_TZ).strftime("%Y-%m-%d")
    d["total"] = d.get("total", 0.0) + usd
    d.setdefault("days", {})[day] = d["days"].get(day, 0.0) + usd
    d["days"] = dict(sorted(d["days"].items())[-60:])
    bal = d.get("balance")
    if bal:
        left = bal["usd"] - (d["total"] - bal["spent_at_set"])
        if left < LOW_BALANCE_USD and not bal.get("warned"):
            bal["warned"] = True
            _pending_alerts.append(
                f"[!] На счёте Claude осталось примерно ${max(left, 0):.2f} (оценка бота). "
                "Пополни: platform.claude.com → Billing → Buy credits, потом напиши мне /balance и сумму на счёте."
            )
    _save_spend(d)


def spend_week() -> float:
    d = _load_spend().get("days", {})
    since = (datetime.datetime.now(audit.LOCAL_TZ) - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    return sum(v for k, v in d.items() if k > since)


def _owner_chats() -> list[int]:
    return [int(cid) for cid, st in _all_state().items() if st.get("owner")]


async def _flush_alerts(bot) -> None:
    while _pending_alerts:
        text = _pending_alerts.pop(0)
        for cid in _owner_chats():
            try:
                await bot.send_message(cid, text)
            except Exception:
                logger.exception("owner alert error")


async def _heavy_ok(update) -> bool:
    """Не больше HEAVY_DAILY_LIMIT тяжёлых запросов в день на чат (сценарии, план, разбор видео, аудит).
    Сообщения по расписанию лимит не тратят."""
    if isinstance(update, _ChatProxy) or HEAVY_DAILY_LIMIT <= 0:
        return True
    cid = update.effective_chat.id
    st = _get_state(cid)
    if st.get("owner"):
        return True
    day = datetime.datetime.now(audit.LOCAL_TZ).strftime("%Y-%m-%d")
    n = st.get("heavy_n", 0) if st.get("heavy_day") == day else 0
    if n >= HEAVY_DAILY_LIMIT:
        await update.effective_message.reply_text(
            f"На сегодня большие разборы закончились ({HEAVY_DAILY_LIMIT} из {HEAVY_DAILY_LIMIT}) 🙏 "
            "Сценарии, план и проверку видео продолжим завтра утром. А на обычные вопросы отвечу и сейчас."
        )
        return False
    _set_state(cid, "heavy_day", day)
    _set_state(cid, "heavy_n", n + 1)
    return True


async def owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _set_state(update.effective_chat.id, "owner", True)
    await update.message.reply_text(
        "Готово: ты владелец бота. Сюда буду писать, если на счёте Claude останется мало денег, "
        "и для тебя нет дневного лимита. Напиши /balance 10 — сколько сейчас долларов на счёте Claude, "
        "чтобы я считала остаток."
    )


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = _load_spend()
    args = context.args or []
    if args:
        try:
            usd = float(args[0].replace(",", ".").lstrip("$"))
        except ValueError:
            await update.message.reply_text("Напиши так: /balance 10 — сколько долларов сейчас на счёте Claude.")
            return
        d["balance"] = {"usd": usd, "spent_at_set": d.get("total", 0.0), "warned": False}
        _save_spend(d)
        await update.message.reply_text(f"Запомнила: на счёте ${usd:.2f}. Предупрежу, когда останется меньше ${LOW_BALANCE_USD:.0f}.")
        return
    bal = d.get("balance")
    lines = [f"Расходы на ИИ за 7 дней: ≈ ${spend_week():.2f}"]
    today = datetime.datetime.now(audit.LOCAL_TZ).strftime("%Y-%m-%d")
    lines.append(f"Сегодня: ≈ ${d.get('days', {}).get(today, 0.0):.2f}")
    if bal:
        lines.append(f"Остаток на счёте Claude: ≈ ${bal['usd'] - (d.get('total', 0.0) - bal['spent_at_set']):.2f} (оценка)")
    else:
        lines.append("Остаток не знаю: напиши /balance 10 — сколько сейчас на счёте Claude.")
    await update.message.reply_text("\n".join(lines))


def _log_usage(where: str, response) -> None:
    u = getattr(response, "usage", None)
    if u is None:
        return
    try:
        _add_spend(getattr(response, "model", "") or "", u)
    except Exception:
        logger.exception("spend tracking error")
    logger.info(
        "Токены [%s]: вход %s, из кэша %s, в кэш %s, выход %s", where,
        getattr(u, "input_tokens", 0), getattr(u, "cache_read_input_tokens", 0) or 0,
        getattr(u, "cache_creation_input_tokens", 0) or 0, getattr(u, "output_tokens", 0),
    )


def _web_search_tool() -> dict:
    return {"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES}


async def _create(system: str, messages: list[dict], max_tokens: int, web_search: bool, model: str | None = None):
    """Запрос к Claude с (необязательным) поиском в интернете.

    Возвращает (текст, stop_reason, источники). Поиск идёт на стороне Anthropic: модель сама решает,
    искать или нет. Долгий поиск API может поставить на паузу (pause_turn) — тогда продолжаем.
    Если поиск выключен в организации Anthropic (400), повторяем запрос без него.
    """
    kwargs = {"model": model or CLAUDE_MODEL, "max_tokens": max_tokens, "system": _system_blocks(system)}
    use_search = web_search and WEB_SEARCH_ENABLED
    msgs = _cache_last_message(list(messages))
    texts: list[str] = []
    sources: dict[str, str] = {}
    for _ in range(4):
        try:
            if use_search:
                response = await client.messages.create(messages=msgs, tools=[_web_search_tool()], **kwargs)
            else:
                response = await client.messages.create(messages=msgs, **kwargs)
        except (anthropic.NotFoundError, anthropic.BadRequestError) as e:
            if kwargs["model"] != CLAUDE_MODEL and "model" in str(e).lower():
                logger.warning("Быстрая модель %s недоступна, отвечаю основной: %s", kwargs["model"], e)
                kwargs["model"] = CLAUDE_MODEL
                continue
            if isinstance(e, anthropic.BadRequestError) and use_search and "web" in str(e).lower() and "search" in str(e).lower():
                logger.warning("Web search недоступен, отвечаю без него: %s", e)
                use_search = False
                continue
            raise
        _log_usage("ответ", response)
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
    model: str | None = None,
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
                system, messages, max_tokens or CLAUDE_MAX_TOKENS, web_search, model=model
            )
            if edit and stop_reason != "max_tokens" and needs_editing(reply_text):
                reply_text = await edit_draft(reply_text, max_tokens or CLAUDE_MAX_TOKENS)
        except anthropic.APIStatusError as e:
            logger.exception("Anthropic API error")
            if e.status_code == 529 or e.status_code >= 500:
                msg = "Claude сейчас перегружен. Попробуй ещё раз через минуту 🙏"
            elif "credit balance" in str(e).lower():
                msg = "У меня закончился баланс на ИИ 🙏 Попроси Никиту пополнить счёт — и я сразу продолжу."
                _pending_alerts.append("[!] Баланс Claude закончился — бот не может отвечать. Пополни: platform.claude.com → Billing → Buy credits.")
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

        stored = reply_text if len(reply_text) <= 2500 else reply_text[:2500] + "\n…[ответ сокращён в памяти]"
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
    Один раунд (раньше было два — экономим); если что-то идёт не так — возвращаем текст как есть."""
    facts_text = facts.prompt_block(DATA_DIR)
    for round_no in (1,):
        issues = lint.find_issues(text, facts_text)
        if not issues:
            return text
        logger.info("Проверка (раунд %s): %s проблем — %s", round_no, len(issues), [i[:60] for i in issues])
        try:
            resp = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=_system_blocks(FIXER_PROMPT + _editor_facts()),
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
            system=_system_blocks(EDITOR_PROMPT + _editor_facts()),
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
        "Как со мной работать:\n"
        "1️⃣ Напиши «Привет, что по Инстаграму?» — расскажу, сколько подписчиков, что просело и почему\n"
        "2️⃣ Скажи «Давай исправим» — найду ролики, которые залетели у конкурентов, и дам хук и сценарий по их механике (и объясню, почему именно так)\n"
        "3️⃣ Сними и смонтируй ролик, пришли мне готовое видео — проверю перед публикацией: что оставить, что поправить 🎬\n\n"
        "Ещё умею:\n"
        "• Разбираю рилсы и профили по ссылке (Instagram, TikTok, YouTube)\n"
        "• Отвечаю на любые вопросы и ищу свежее в интернете 🔎\n"
        "• Понимаю голосовые и кружки 🎙, читаю фото, скрины статистики, PDF\n\n"
        "/audit — подробный аудит Инстаграма (или /audit @ник)\n"
        "/trends — что сейчас залетает в нише\n"
        "/competitors — аккаунты-ориентиры (добавить: /competitors @ник)\n"
        "/memory — что я о тебе помню (добавить: «Запомни: …»)\n"
        "/new — начать новый диалог\n"
        "/help — подробнее"
    )


HELP_TEXT = (
    "✨ <b>Как со мной работать</b>\n\n"
    "<b>1. Что по Инстаграму</b>\n"
    "• «Привет, что по Инстаграму?» / «я проснулась» — сводка: подписчики, что просело и почему\n"
    "• /audit — подробный аудит аккаунта, /audit @ник — любого открытого\n\n"
    "<b>2. Давай исправим</b>\n"
    "• «Давай исправим», «что снять?», «сценарий как у конкурентов» — найду залетевшие ролики ниши и конкурентов и дам хук + сценарий по их механике\n"
    "• Объясню, что в сценарии нельзя менять, чтобы ролик сработал\n"
    "• /competitors @ник — добавить аккаунт-ориентир (чем точнее ориентиры, тем точнее сценарии)\n\n"
    "<b>3. Проверка перед публикацией 🎬</b>\n"
    "• Сними и смонтируй сама (или монтажёр) → пришли готовое видео\n"
    "• Скажу: можно публиковать / доработать / переснять, по секундам что поправить и какую подпись поставить\n\n"
    "<b>Ещё</b>\n"
    "• Ссылка на рилс или пост — разберу, почему залетел · /trends — что залетает в нише\n"
    "• Скрин статистики — разберу цифры · фото, PDF, сайты\n"
    "• Голосовые и кружки 🎙 · поиск в интернете 🔎 · любые вопросы\n"
    "• «Запомни: …» — запомню факт, /memory — что я помню\n"
    "• /morning — сводка сама каждое утро + итоги недели по понедельникам (/stop_morning — выключить), /week — итоги недели сейчас\n"
    "• /today — план съёмки на сегодня: что просело, сколько рилсов снять, хуки и сценарии (приходит сам в 9:00)\n"
    "• /plan — контент-план на неделю: 7 рилсов + сторис-прогрев к твоим курсам и мероприятиям\n"
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
    if not await _heavy_ok(update):
        return
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

    note = summary[:2000] + f"\n\n[Аудит аккаунта @{handle}]"
    reply = await ask_claude(update, update.effective_chat.id, content, history_note=note, max_tokens=6000, edit=False)
    if reply:
        _mark_mode(update.effective_chat.id, "audit")
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
    for label, data_bytes, mime in await trends.covers(result, limit=3):
        content.append({"type": "text", "text": f"Обложка {label}"})
        content.append(social.image_block(data_bytes, mime))
    topic_line = f"\n\nЗапрос Анны: {topic}" if topic else ""
    content.append({"type": "text", "text": TRENDS_PROMPT.format(topic=topic_line)})

    note = summary[:2000] + "\n\n[Тренды ниши и хуки]" + (f" {topic}" if topic else "")
    reply = await ask_claude(update, update.effective_chat.id, content, history_note=note, max_tokens=6000, edit=False)
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


# Основная модель — только когда просят СОЗДАТЬ или ПЕРЕДЕЛАТЬ контент. Вопросы «что такое прогрев?»,
# «как часто постить?», болтовня и короткие ответы идут в дешёвую модель.
CONTENT_ASK_RE = re.compile(
    r"(напиш|придума|сочин|состав|сгенер|перепиш|переделай|передела|исправ|улучш|сократ|дополн|"
    r"короче|длиннее|ещё вариант|еще вариант|другой вариант|разбер|проанализ|аудит|сценари)",
    re.I,
)
CONTENT_NOUN_RE = re.compile(
    r"(пост|текст|подпис|сторис|stories|рилс|reels|карусел|контент|хук|иде[июяей]|план|стратег|воронк|прогрев|оффер)",
    re.I,
)
CONTENT_REQ_RE = re.compile(r"(дай|давай|предлож|нужн|нужен|хочу|сделай|можешь|помоги|подкин|покажи|накидай)", re.I)


def is_simple_question(chat_id: int, user_text: str) -> bool:
    """Простой вопрос → дешёвая модель. Всё, что похоже на работу с контентом, и любые
    правки сразу после контента — основная модель, чтобы не терять качество."""
    if not FAST_MODEL or FAST_MODEL.lower() in ("off", "0", "none") or FAST_MODEL == CLAUDE_MODEL:
        return False
    text = user_text.strip()
    if len(text) > 400 or CONTENT_ASK_RE.search(text):
        return False
    if CONTENT_NOUN_RE.search(text) and CONTENT_REQ_RE.search(text):
        return False  # «дай идею для поста», «нужен план прогрева» — это работа с контентом
    history = conversations.get(chat_id, [])
    last_bot = next((m for m in reversed(history) if m.get("role") == "assistant"), None)
    if last_bot:
        prev = last_bot.get("content")
        prev = prev if isinstance(prev, str) else ""
        if len(prev) > 1200 or CONTENT_MARKERS_RE.search(prev):
            return False  # только что был пост/сценарий — правки к нему делает основная модель
    return True


async def process_user_text(update: Update, user_text: str) -> None:
    """Общая обработка текста — и набранного, и распознанного из голосового/кружка."""
    chat_id = update.effective_chat.id
    urls = URL_RE.findall(user_text)[:MAX_LINKS_PER_MESSAGE]

    if await handle_design_commands(update, user_text):
        return

    if not urls:
        kind = montage_followup(chat_id, user_text)
        if kind == "edit":
            last = _last_montage[chat_id]
            await run_montage(update, user_text, last["videos"], previous=last["plan"])
            return
        if kind == "new":
            await run_montage(update, user_text, _pending_videos.pop(chat_id, None) or recent_videos(chat_id))
            return
        if HITS_INTENT_RE.search(user_text) or (YES_RE.match(user_text) and _recent_mode(chat_id) in ("brief", "audit")):
            await run_hits(update, "" if YES_RE.match(user_text) else user_text)
            return
        if BRIEF_INTENT_RE.search(user_text) and not AUDIT_INTENT_RE.search(user_text):
            await run_brief(update, user_text)
            return

    to_remember = facts.extract(user_text)
    if to_remember:
        await remember_fact(update, to_remember)
        return

    if not urls and (CAROUSEL_INTENT_RE.search(user_text)
                     or (AI_ART_RE.search(user_text) and recent_photos(update.effective_chat.id))):
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
        await ask_claude(update, chat_id, user_text, web_search=True,
                         model=FAST_MODEL if is_simple_question(chat_id, user_text) else None)
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
# подпись к фото, по которой делаем карусель/креатив (а не просто комментируем фото)
PHOTO_CAROUSEL_RE = re.compile(
    r"(карусел|слайд|креатив|криатив|постик|\bпост\b|оформ|обложк|нарису|сгенерир|нейро|"
    r"сделай[^.!?\n]{0,30}(из|с|по)\s+(этого|этих|этой|эт[ои]м|фото|фотк|снимк))", re.I)
# в тексте есть отсылка к уже присланным фото
REFERS_PHOTO_RE = re.compile(r"(фото|фотк|снимк|из этого|по этому|скинул|прислал|отправил|\bменя\b)", re.I)
# просят ИИ-картинку человека по фото (а не векторный рисунок)
AI_ART_RE = re.compile(
    r"(ии[- ]?(картин|арт|верси|фото|изображ)|ai[- ]?(art|арт)|нейро[- ]?(картин|арт|фото|портрет)|"
    r"сгенерир\w*\s+(меня|её|ее|фото|картин|портрет)|нарису\w*\s+(меня|её|ее|анну|по фото|мой|мою)|"
    r"в стиле\s+(аниме|акварел|мульт|комикс|масл|живопис|картин|арт))", re.I)
RECENT_PHOTOS_TTL = 2 * 3600
_recent_photos: dict = {}


def remember_photos(chat_id: int, photos: list[bytes]) -> None:
    import time as _t
    if photos:
        _recent_photos[chat_id] = {"t": _t.time(), "photos": list(photos)[:MAX_CAROUSEL_PHOTOS]}


def recent_photos(chat_id: int) -> list[bytes]:
    import time as _t
    r = _recent_photos.get(chat_id)
    if r and _t.time() - r["t"] < RECENT_PHOTOS_TTL:
        return list(r["photos"])
    return []


AI_ART_SUFFIX = (
    " Keep the woman's face, identity, hair colour and features recognisably the same as in the reference photo. "
    "Vertical 4:5 composition, leave calm space in the lower third for overlay text. No text, no letters, no logos."
)


async def generate_ai_portraits(photo: bytes, prompt: str, count: int = 1) -> tuple[list[bytes], str]:
    """ИИ-картинки по фото Анны (OpenAI images.edit). Возвращает (картинки, причина_ошибки)."""
    if openai_client is None:
        return [], "нет ключа OpenAI"
    from PIL import Image as _Image, ImageOps as _ImageOps
    im = _Image.open(io.BytesIO(photo))
    try:
        im = _ImageOps.exif_transpose(im)
    except Exception:
        pass
    im = im.convert("RGB")
    im.thumbnail((1536, 1536))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    png = buf.getvalue()
    full = prompt.strip()[:900] + AI_ART_SUFFIX

    async def one(i: int):
        r = await asyncio.wait_for(openai_client.images.edit(
            model=IMAGE_MODEL, image=(f"anna_{i}.png", png, "image/png"), prompt=full, size="1024x1536", n=1),
            timeout=180)
        item = r.data[0]
        if getattr(item, "b64_json", None):
            return base64.b64decode(item.b64_json)
        if getattr(item, "url", None):
            async with httpx.AsyncClient(timeout=60) as h:
                resp = await h.get(item.url)
                resp.raise_for_status()
                return resp.content
        raise RuntimeError("пустой ответ")

    results = await asyncio.gather(*(one(i) for i in range(max(1, min(count, 3)))), return_exceptions=True)
    out = [x for x in results if isinstance(x, (bytes, bytearray))]
    errs = [str(x)[:150] for x in results if isinstance(x, Exception)]
    for e in errs:
        logger.warning("AI portrait failed: %s", e)
    return out, ("; ".join(errs) if not out else "")


# признаки того, что Claude написал на слайды объяснения/отказ вместо контента
META_SLIDE_RE = re.compile(r"(не могу|не вижу|не умею|не подключ|пришли(те)?\s+(мне|ещё|еще)|вариант\s*\d|"
                           r"я\s+(бот|ии|нейросеть)|что выбираешь|какой вариант)", re.I)
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
    """Фото по слайдам. Claude выбирает номера в design.slide_photos; пустые слайды добиваем выбранными
    фото по кругу (с другой кадровкой), чтобы карусель не была «одна обложка и голый текст»."""
    per: list = [None] * n_slides
    if not photos or raw.get("photo_use") == "reference":
        return per
    chosen: list[int] = []
    sp = raw.get("slide_photos")
    if isinstance(sp, list):
        for i, v in enumerate(sp[:n_slides]):
            try:
                k = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= k <= len(photos):
                per[i] = k
                if k not in chosen:
                    chosen.append(k)
    pool = chosen or list(range(1, len(photos) + 1))
    if per[0] is None:
        per[0] = pool[0]
    j = 0
    for i in range(n_slides):
        if per[i] is None:
            per[i] = pool[(j + (1 if len(pool) > 1 else 0)) % len(pool)]
            j += 1
    seen: dict = {}
    out = []
    for k in per:
        rep = seen.get(k, 0)
        seen[k] = rep + 1
        out.append((photos[k - 1], rep))   # rep>0 — то же фото, но другой кадр
    return out


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
    if photos:
        remember_photos(chat_id, photos)
    elif REFERS_PHOTO_RE.search(user_text or "") or AI_ART_RE.search(user_text or ""):
        photos = recent_photos(chat_id)   # «сделай из того фото, что скинул» — берём недавно присланные
        if photos:
            logger.info("Carousel: using %d recent photos from this chat", len(photos))
    wants_art = bool(AI_ART_RE.search(user_text or ""))
    from_insta = False
    if INSTA_PHOTOS_RE.search(user_text or ""):
        try:
            await status.edit_text("📸 Смотрю фото в Instagram Анны… это до минуты ⏳")
        except Exception:
            pass
        insta, err = await fetch_insta_photos()
        logger.info("Insta photos fetched: %d (%s)", len(insta), err or "ok")
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
        if wants_art:
            info += (" Анна просит ИИ-картинки по своему фото — добавь в design поле \"ai_art\" "
                     "(см. пункт 7 инструкции).")
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
        meta_hits = sum(1 for s in data["slides"] if META_SLIDE_RE.search(s.get("title", "") + " " + s.get("text", "")))
        if meta_hits >= 2 or (data["slides"] and META_SLIDE_RE.search(data["slides"][0].get("title", ""))):
            logger.warning("Carousel slides contain meta text (%d), retrying", meta_hits)
            note = ("\n\n[ВАЖНО: в прошлый раз ты написала на слайды объяснения и варианты для Анны. Так нельзя. "
                    "Слайды — только контент для её аудитории по теме запроса. Всё, что можно сделать — сделай: "
                    "фото уже приложены, ИИ-картинки делаются через поле design.ai_art.]")
            retry_content = (content + note) if isinstance(content, str) else content + [{"type": "text", "text": note}]
            reply2, _, _ = await _create(current_system_prompt(), [{"role": "user", "content": retry_content}], 12000, False)
            data = carousel.parse(reply2)
        raw = data.get("design") or {}
        d = carousel.resolve(raw, saved=design_store.saved(DATA_DIR))
        art = raw.get("ai_art") if isinstance(raw.get("ai_art"), dict) else None
        if art is None and wants_art and photos:
            art = {"prompt": "elegant editorial painted portrait of the woman, soft light, luxury lifestyle mood", "count": 2}
        if art and photos:
            try:
                await status.edit_text("🎨 Рисую ИИ-картинки по фото… это до 1–2 минут ⏳")
            except Exception:
                pass
            try:
                src_i = int(art.get("photo", 1))
            except (TypeError, ValueError):
                src_i = 1
            src = photos[src_i - 1] if 1 <= src_i <= len(photos) else photos[0]
            try:
                cnt = int(art.get("count", 2))
            except (TypeError, ValueError):
                cnt = 2
            arts, err = await generate_ai_portraits(src, str(art.get("prompt") or user_text), cnt)
            logger.info("AI portraits: %d (%s)", len(arts), err or "ok")
            if arts:
                photos = arts
                raw = {**raw, "slide_photos": None}
                warn += f"\n🖼 Нарисовала {len(arts)} ИИ-картинк{'у' if len(arts) == 1 else 'и'} по фото."
            else:
                warn += f"\n⚠️ ИИ-картинку нарисовать не получилось ({err[:160]}) — сделала с оригинальными фото."
        slide_photos = _assign_photos(raw, photos, len(data["slides"]))
        n_used = sum(1 for x in slide_photos if x)
        logger.info("Carousel: %d slides, photos given=%d (insta=%s), chosen=%s, on slides=%d",
                    len(data["slides"]), len(photos), from_insta, raw.get("slide_photos"), n_used)
        if photos:
            src = f"взяла {len(photos)} фото из Instagram" if from_insta and len(photos) == len(insta) else f"фото: {len(photos)}"
            warn += f"\n📸 {src}, поставила на {n_used} из {len(data['slides'])} слайдов."
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
    note = summary[:2000] + (f"\n\n{request}" if len(request) >= 3 else "\n\n[Разбери этот пост]")
    await ask_claude(update, update.effective_chat.id, content, history_note=note, edit=False)


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

    if msg.audio and (MUSIC_CAPTION_RE.search(msg.caption or "") or msg.audio.performer or msg.audio.title):
        await save_music(update, context, msg.audio, msg.audio.file_name or "music.mp3")
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


# ---------------------------------------------------------------------------
# «Давай исправим» → сценарии по механике залетевших роликов ниши и конкурентов
# ---------------------------------------------------------------------------
HITS_INTENT_RE = re.compile(
    r"(давай\s+(это\s+)?(исправ|сним|снима|переделыва)|как\s+(это\s+|всё\s+|все\s+)?исправ|что\s+(мне\s+)?(снять|снимать)|"
    r"какой\s+(рилс|ролик)\s+(снять|снимать)|как\s+у\s+конкурент|у\s+конкурентов|залетевш|миллионник|"
    r"сценари\w*\s+(по|как\s+у)\s+(залет|конкурент|хит|тренд|вирус)|что\s+залета\w*\s+у)",
    re.I,
)
YES_RE = re.compile(r"^\s*(да|давай|го|ок|окей|хочу|погнали|конечно|ага|давай исправим|да,?\s*давай)[\s!.,)]*$", re.I)
_last_mode: dict[int, tuple[str, float]] = {}


def _mark_mode(chat_id: int, mode: str) -> None:
    _last_mode[chat_id] = (mode, asyncio.get_event_loop().time())


def _recent_mode(chat_id: int, within: float = 12 * 3600) -> str | None:
    m = _last_mode.get(chat_id)
    if m and asyncio.get_event_loop().time() - m[1] < within:
        return m[0]
    # после перезапуска бота — смотрим, чем закончился последний ответ
    history = conversations.get(chat_id) or []
    if history and history[-1].get("role") == "assistant":
        c = history[-1].get("content")
        text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        if re.search(r"(давай исправ|залетели у конкурентов)", text[-600:], re.I):
            return "brief"
    return None


_ig_cache: dict = {}


async def _ig_insights_text(stats: dict | None = None) -> str:
    """Официальная статистика из Instagram API (если в Railway добавлен IG_ACCESS_TOKEN). Кэш 3 часа."""
    if not iginsights.enabled(DATA_DIR):
        return ""
    now = time.time()
    if _ig_cache.get("t", 0) > now - 3 * 3600 and _ig_cache.get("data"):
        data = _ig_cache["data"]
    else:
        try:
            data = await iginsights.fetch(DATA_DIR)
        except Exception as e:
            logger.warning("Instagram API: %s", e)
            return ""
        _ig_cache.update(t=now, data=data)
    durations = {r["code"]: r["duration"] for r in (stats or {}).get("rows", []) if r.get("code") and r.get("duration")}
    return iginsights.insights_text(data, durations)


async def _owner_stats_text(stats: dict | None = None) -> str:
    """Внутренняя статистика: сначала vidIQ (VIDIQ_API_KEY), иначе официальный ключ Instagram (IG_ACCESS_TOKEN)."""
    if vidiq.enabled():
        try:
            return vidiq.reels_text(await vidiq.fetch_reels(), audit.LOCAL_TZ)
        except Exception as e:
            logger.warning("vidIQ: %s", e)
    return await _ig_insights_text(stats)


_hook_cache: dict[str, str] = {}


async def _hook_transcripts(rows: list[dict], limit: int = 4, seconds: int = 6) -> str:
    """Расшифровка первых секунд залетевших роликов: хук чаще всего — первая сказанная фраза."""
    if openai_client is None:
        return ""
    out = []
    for i, r in enumerate(rows[:8], 1):
        if len(out) >= limit:
            break
        url = r.get("video")
        if not url:
            continue
        if r["url"] in _hook_cache:
            text = _hook_cache[r["url"]]
        else:
            text = ""
            src = os.path.join(MEDIA_DIR, f"hook_{i}.mp4")
            dst = os.path.join(MEDIA_DIR, f"hook_{i}.mp3")
            try:
                os.makedirs(MEDIA_DIR, exist_ok=True)
                data = await social.download_bytes(url, max_bytes=40 * 1024 * 1024)
                with open(src, "wb") as f:
                    f.write(data)
                code, _ = await social._run("ffmpeg", "-y", "-i", src, "-t", str(seconds), "-vn", "-ac", "1",
                                            "-ar", "16000", "-b:a", "48k", dst, timeout=60)
                if code == 0 and os.path.exists(dst):
                    with open(dst, "rb") as f:
                        text = (await transcribe(f.read(), "hook.mp3")).strip()
            except Exception:
                logger.warning("Не удалось расшифровать начало ролика %s", r.get("url"))
            finally:
                for pth in (src, dst):
                    try:
                        os.remove(pth)
                    except OSError:
                        pass
            _hook_cache[r["url"]] = text
        if text:
            out.append(f"№{i} @{r['author']}: «{text[:300]}»")
        else:
            out.append(f"№{i} @{r['author']}: (без речи в первые секунды — хук визуальный или текстом на экране)")
    return "\n".join(out)


async def run_hits(update: Update, topic: str = "") -> None:
    chat_id = update.effective_chat.id
    if not await _heavy_ok(update):
        return
    status = await update.effective_message.reply_text(
        "🔥 Ищу ролики, которые залетели у конкурентов и в нише, и разбираю, почему. 1–3 минуты ⏳"
    )
    try:
        result = await trends.collect(DATA_DIR)
    except trends.TrendsError as e:
        await status.edit_text(f"Не получилось найти залетевшие ролики: {e}")
        return
    except Exception:
        logger.exception("Hits error")
        await status.edit_text("Не получилось найти залетевшие ролики. Попробуй ещё раз через пару минут.")
        return
    try:
        await status.edit_text(f"✅ Нашла {len(result['rows'])} залетевших роликов. Разбираю механику и пишу сценарии…")
    except BadRequest:
        pass
    summary = trends.trends_text(result)
    hooks = await _hook_transcripts(result["rows"])
    if hooks:
        summary += "\n\n[Что говорят в первые секунды залетевших роликов — дословная расшифровка]\n" + hooks
    own = brief.own_hits_text(DATA_DIR)
    if own:
        summary += "\n\n" + own
    ig = await _owner_stats_text()
    if ig:
        summary += "\n\n" + ig
    content: list[dict] = [{"type": "text", "text": summary}]
    for label, data_bytes, mime in await trends.covers(result, limit=3):
        content.append({"type": "text", "text": f"Обложка {label}"})
        content.append(social.image_block(data_bytes, mime))
    topic_line = f"\n\nЗапрос Анны: {topic.strip()[:300]}" if topic.strip() else ""
    content.append({"type": "text", "text": HITS_PROMPT.format(topic=topic_line)})
    note = summary[:2000] + "\n\n[Сценарии по залетевшим роликам]" + (f" {topic.strip()[:200]}" if topic.strip() else "")
    reply = await ask_claude(update, chat_id, content, history_note=note, max_tokens=5000, edit=False)
    if reply:
        _mark_mode(chat_id, "hits")
        m = MEMORY_RE.search(reply)
        if m:
            audit.save_audit(DATA_DIR, "__trends__", m.group(1).strip())


# ---------------------------------------------------------------------------
# Утренняя сводка: «Привет, я проснулась», «какая обстановка в инсте?»
# ---------------------------------------------------------------------------
BRIEF_INTENT_RE = re.compile(
    r"(проснул|доброе утро|обстановк|сводк|"
    r"что\s+(там\s+)?(нового\s+)?(в|по|с)\s+(инст|инстаграм|акк|аккаунт|блог|статистик)|"
    r"как\s+(там\s+)?(дела\s+)?(в|с|у)\s+(инст|инстаграм|акк|аккаунт|блог)|"
    r"как\s+(мой|мои|наш)\s+(инст|акк|аккаунт|блог|рилс|охват|просмотр)|"
    r"что\s+(там\s+)?(с\s+)?(охват|просмотр|подписчик|статистик))",
    re.I,
)
BRIEF_CACHE_SECONDS = 30 * 60
_brief_cache: dict = {}

BRIEF_PROMPT = """Сделай Анне утреннюю сводку по её Instagram — как личный продюсер в Telegram, тепло и по делу.
Выше — реальные цифры, посчитанные кодом (им можно доверять). Её сообщение: «{text}».

Структура (коротко, всего до ~1400 знаков):
1. Приветствие одной строкой — по времени суток и её сообщению.
2. **Что изменилось** с прошлой проверки: подписчики, как растут последние публикации, что выстрелило или
   просело — только цифры из данных. Если это первая сводка — коротко «точка отсчёта».
3. **Что работает** — 1–2 лучшие публикации (дата, тема, цифры) и почему они зашли — через зрителя.
4. **Что не залетело и почему** — 2–3 слабые публикации: дата, тема, цифры (×медианы) и для каждой одна конкретная
   причина (хук, тема, формат, длина, повтор). Это главный блок — Анна должна понять, что именно пошло не так.
5. **Что изменить** — 2–3 конкретных действия на сегодня–завтра. Сценарий здесь НЕ пиши — его бот даст
   на следующем шаге по залетевшим роликам конкурентов.
6. В конце — вопрос-следующий шаг, обязательно такой: «Давай исправим? Найду ролики, которые залетели у конкурентов,
   и сделаю сценарий по их механике». Можно добавить второй короткий вопрос (например, разобрать конкретный провал).

ЖЁСТКИЕ ПРАВИЛА:
- Говори ТОЛЬКО о том, что есть в данных: подписчики, просмотры, лайки, комментарии, даты, подписи.
  Досмотров, skip rate, удержания, охватов, сохранений, репостов и «данных алгоритма» у тебя НЕТ — не упоминай их
  и не ссылайся на «аналитику» или «исследования». Если хочешь сказать про досмотры — предложи прислать скрин статистики.
- Не рассуждай о том, что «видит», «любит» или «наказывает» алгоритм, — это догадки. Объясняй через людей:
  что зритель почувствовал, почему остановился или пролистал.
- Если в данных есть пометка «[!] ДУБЛЬ» — прямо скажи, что ролик выложен повторно, и что частоту накручивают дубли.
- Если с последней публикации прошло 2+ дня — мягко напомни и дай готовый короткий сценарий на 30 секунд
  (хук дословно + 3–4 фразы), чтобы снять сегодня без подготовки.
- Если в сообщении есть «Заметка бота: … не присылала готовое видео на проверку» — одной тёплой строкой напомни:
  «Сняла что-то? Пришли мне перед публикацией — проверю хук и первые секунды».
- Если Анна выбрала «каждый день по одной теме» — после «Что изменить» добавь блок **Тема дня**: тема, хук дословно,
  текст на экране, формат до 60 секунд.
- Если есть блок «Внутренняя статистика Instagram» или «Официальная статистика Instagram» — тогда досмотры,
  «пролистали сразу», репосты и сохранения у тебя ЕСТЬ (запрет выше на них не действует) — опирайся прежде всего на него: какую долю ролика досматривают
  (среднее время просмотра из длины), % сохранений и репостов от охвата. Объясняй «почему просело» через эти цифры
  (например: «смотрят в среднем 4 секунды из 45 — хук не держит»). Если есть вопросы зрителей из комментариев —
  одной строкой предложи снять ролик-ответ на самый частый.
- Если есть «[!] СЛИШКОМ ЧАСТО» — прямо скажи: ролики отбирают охват друг у друга, норма — 1 рилс в день.
- Если у слабых роликов пометка «[!] ДЛИННЫЙ» — скажи, что длинные ролики почти не досматривают, держать до 60 секунд.
- Если есть строка «Язык подписи и просмотры» и один язык заметно сильнее (в 1,5+ раза) — скажи, на каком языке
  её смотрят лучше, и посоветуй чаще снимать на нём. Если разница небольшая — не упоминай.
- Не выдумывай личные истории Анны — где нужна история, пиши [вставь свою реальную историю: …].
Только русский. Без таблиц."""


async def run_brief(update, text: str, daily: bool = False) -> None:
    chat_id = update.effective_chat.id
    _subscribe(chat_id)
    handle = audit.CLIENT_INSTAGRAM
    cached = _brief_cache.get(handle)
    now = asyncio.get_event_loop().time()
    if cached and now - cached["t"] < BRIEF_CACHE_SECONDS:
        stats, prev = cached["stats"], cached["prev"]
        status = None
    else:
        status = await update.effective_message.reply_text("☀️ Смотрю, что происходит в твоём Instagram… 1–2 минуты ⏳")
        await update.effective_chat.send_action("typing")
        try:
            data = await audit.fetch_account(handle, limit=brief.BRIEF_POSTS)
            stats = audit.build_stats(data)
        except audit.AuditError as e:
            await status.edit_text(f"Не получилось заглянуть в Instagram: {e}")
            return
        except Exception:
            logger.exception("Brief fetch error")
            await status.edit_text("Не получилось выгрузить Instagram. Попробуй ещё раз через пару минут.")
            return
        prev = brief.load_snapshot(DATA_DIR)
        try:
            brief.save_snapshot(DATA_DIR, brief.make_snapshot(stats))
            brief.add_history(DATA_DIR, stats)
        except OSError:
            logger.exception("Brief snapshot save error")
        _brief_cache[handle] = {"t": now, "stats": stats, "prev": prev}
    now_local = datetime.datetime.now(audit.LOCAL_TZ)
    facts_text = brief.brief_text(stats, prev, now_local)
    ig = await _owner_stats_text(stats)
    if ig:
        facts_text += "\n\n" + ig
    if daily:
        await _run_daily_plan(update, chat_id, facts_text, text, status)
        return
    if status:
        try:
            await status.edit_text("✅ Посмотрела. Собираю сводку…")
        except BadRequest:
            pass
    content = facts_text + "\n\n" + BRIEF_PROMPT.format(text=text.strip()[:300])
    note = facts_text[:2000] + f"\n\n[Утренняя сводка по запросу Анны: {text.strip()[:200]}]"
    if await ask_claude(update, chat_id, content, history_note=note, max_tokens=2500, edit=False):
        _mark_mode(chat_id, "brief")


DAILY_PROMPT = """Ты — личный продюсер Анны. Сейчас 9 утра, это ежедневный план: что снять СЕГОДНЯ, чтобы аккаунт рос.
Выше — реальные цифры, посчитанные кодом: её публикации, внутренняя статистика (если есть блок: «пролистали сразу»,
досмотр, репосты, сохранения), её хиты и провалы, залетевшие ролики ниши и что в них говорят в первые секунды.
Сообщение / заметки бота: «{text}».

Блок «План прогрева на сегодня» посчитан кодом по календарю её запусков: фаза, сколько рилсов и сторис —
это решение, следуй ему. Если в «Реальных историях и фактах Анны» есть более свежая дата или новый продукт —
верь фактам Анны, а не блоку, и скажи об этом одной строкой.

Структура (весь ответ до ~6000 знаков, без таблиц, только русский в пояснениях):

1. Приветствие одной строкой.

2. **Что происходит** — 3–5 строк по цифрам. Главная проблема прямо сейчас и где теряем людей:
   - пролистывают в первые секунды (выше ~45%) → хук не цепляет;
   - смотрят малую долю ролика → затянуто или слишком длинно;
   - смотрят, но мало репостов/сохранений → нет эмоции или пользы, которой хочется поделиться;
   - мало новых подписчиков → нет причины подписаться: не ясно, что ещё будет в блоге, нет серии и призыва.
   Каждый вывод — с конкретным роликом и цифрами («“Курс Богиня”, 55 с: 66% пролистали сразу, смотрят 6 с»).

3. **Где не уследили** — 1–3 пункта: что сделали не так за последние дни (несколько роликов в один день, длинные
   ролики, прямая реклама курса в рилсах, повторы, язык). Только то, что видно в данных.

4. **Где мы сейчас** — одна строка: «До старта «…» N дней — фаза …, поэтому сегодня: N рилс + N сторис».

5. **Сегодня снять: N рилс(а)** — N из блока «План прогрева» (+1, если она 2+ дня не выкладывала рилсы, максимум 3).
   Выкладываем 1 рилс в день, остальные — в запас. В фазах РАЗОГРЕВ–ДЕДЛАЙН один рилс — на тему-боль главного
   запуска (не реклама!) с кодовым словом запуска в подписи.
   Для каждого рилса:
   **Рилс 1 — тема** · формат (1–4 из блока «Как устроены рилсы Анны») · длина N секунд · язык · когда выложить
   - Почему этот: на какой её хит или залетевший ролик ниши опирается (с цифрами)
   - Хук (первые 2 секунды, дословно — фраза или первый кадр + заголовок): вариант А ⭐ и вариант Б
   - Текст на экране: дословно, по плашкам
   - Что говорить (по секундам, дословно, её словами) — или, для формата без речи, что происходит в кадре и музыка
   - Как снять: одна-две простые сцены из её жизни; реквизит только тот, что есть дома
   - Подпись в её голосе + кодовое слово для комментариев (как она делает: «пиши "…" — надішлю практику»)
   Хотя бы один рилс в день — формат 1 или 2 (скетч/диалог или сцена + текст под музыку).
   Язык рилсов — украинский по умолчанию (так её смотрят в 3,5 раза лучше), пояснения тебе — по-русски.
   Длину выбирай по её цифрам из блока «По длине ролика». Если короткие держат лучше — короткие (10–30 с).
   Длинный (до 60–90 с) — только развитие её самой сильной темы, и хук должен быть особенно сильным.

6. **Прогрев в сторис: N штук** — N из блока «План прогрева», задача — из него же. Для каждой сторис:
   № · что показать (Анна в кадре / её день / экран с текстом) · что сказать или написать — дословно, её голосом ·
   стикер (опрос с вариантами / вопрос / ссылка / «пиши КОДОВОЕ СЛОВО»). Сторис связаны в одну историю:
   зацепка → узнавание → смысл/польза → доказательство (реальный кейс или отзыв) → призыв (в фазах ПРОДАЖА/ДЕДЛАЙН).
   Цифры, цены, даты, кейсы и отзывы — ТОЛЬКО из «ФАКТЫ О ПРОДУКТЕ». Нужного факта нет — пиши [вставь: …].

7. **Цель на сегодня** — 1–2 строки: какие цифры ждём от новых роликов по сравнению с её медианой
   (например: «пролистали сразу ниже 40%, досмотр выше 50%»).

8. Последней строкой: «Сняла — пришли мне видео до публикации, проверю хук и первые секунды 🎬»

ЖЁСТКИЕ ПРАВИЛА:
- Темы только её: деньги, отношения, женская энергия и женственность, вера/духовность в психологии, практики.
  Механику залетевших роликов конкурентов переносишь на её тему, саму тему конкурента не копируешь.
- Не повторяй темы и хуки роликов, которые она уже выложила за последние дни — новый угол.
- Прямую рекламу курсов в рилсах не предлагай — продажи в сторис. Прошедшие мероприятия не предлагай.
- НИЧЕГО НЕ ВЫДУМЫВАЙ: ни учениц, ни отзывов, ни цифр, ни «осталось 3 места», ни бонусов, ни дат, ни историй
  из жизни Анны. Используй только данные выше, профиль, продукты и её факты. Чего нет — [вставь: …].
- Говори только о том, что есть в данных. Если блока внутренней статистики нет — не упоминай досмотры и
  «пролистали сразу», опирайся на просмотры, лайки, комментарии.
- Не рассуждай об «алгоритме» — объясняй через зрителя: что почувствовал, почему остановился или пролистал.
- Не выдумывай личные истории и факты об Анне — пиши [вставь свою реальную историю: …].
- Если она 2+ дня не выкладывала рилсы — мягко скажи об этом одной строкой."""


async def _run_daily_plan(update, chat_id: int, facts_text: str, text: str, status) -> None:
    summary = facts_text
    own = brief.own_hits_text(DATA_DIR)
    if own:
        summary += "\n\n" + own
    try:
        result = await trends.collect(DATA_DIR)  # кэш TRENDS_CACHE_HOURS — повторно Apify не тратим
        summary += "\n\n" + trends.trends_text(result)
        hooks = await _hook_transcripts(result["rows"], limit=3)
        if hooks:
            summary += "\n\n[Что говорят в первые секунды залетевших роликов ниши — дословно]\n" + hooks
    except Exception:
        logger.warning("Daily plan: тренды недоступны, план без них")
    if status:
        try:
            await status.edit_text("✅ Посмотрела цифры. Пишу план съёмки на сегодня…")
        except BadRequest:
            pass
    summary += "\n\n" + launches.today_block(datetime.datetime.now(audit.LOCAL_TZ).date())
    content = summary + "\n\n" + DAILY_PROMPT.format(text=text.strip()[:400])
    note = facts_text[:2000] + "\n\n[План съёмки на сегодня]"
    if await ask_claude(update, chat_id, content, history_note=note, max_tokens=8000, edit=False):
        _mark_mode(chat_id, "hits")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _heavy_ok(update):
        return
    _brief_cache.clear()
    await run_brief(update, "Что снять сегодня?", daily=True)


# ---------------------------------------------------------------------------
# Видео: копим присланные ролики → разбор или автомонтаж
# ---------------------------------------------------------------------------
MEDIA_DIR = os.environ.get("MEDIA_DIR", "/tmp/anna-media")
MEDIA_TTL = int(os.environ.get("MEDIA_TTL", str(3 * 3600)))
VIDEO_BATCH_WAIT = 4.0
MONTAGE_ENABLED = os.environ.get("MONTAGE_ENABLED", "0") == "1"  # монтаж выключен: видео идёт на проверку перед публикацией
MONTAGE_RE = re.compile(
    r"(смонтир|монтаж|монтируй|склей|склеи|нарежь|собери\s+(мне\s+)?(рилс|ролик|видео)|"
    r"(сделай|сделать|собери)\s+(мне\s+)?(из\s+(них|этого|этих|видео)\s+)?(рилс|reels|ролик)|"
    r"субтитр|вырежи\s+пауз)",
    re.I,
)
MONTAGE_EDIT_RE = re.compile(
    r"(короче|длиннее|другой\s+хук|поменяй\s+хук|смени\s+хук|хук\s+|без\s+субтитр|с\s+субтитр|без\s+музык|"
    r"музык|переставь|начни\s+с|убери|оставь\s+только|переделай|ещё\s+вариант|еще\s+вариант|другой\s+вариант|"
    r"быстрее|динамичн|медленн|не\s+вырезай)",
    re.I,
)
MUSIC_CAPTION_RE = re.compile(r"(музык|трек|фон|песн|звук\s+для)", re.I)

_video_bin: dict[int, list[dict]] = defaultdict(list)   # chat -> [{path, name, ts}]
_video_batch: dict[int, dict] = {}                       # копим пачку присланных подряд видео
_pending_videos: dict[int, list[str]] = {}               # пачка, по которой ждём кнопку
_video_inflight: dict[int, int] = defaultdict(int)       # сколько видео ещё скачивается
_music_bin: dict[int, dict] = {}                         # chat -> {path, ts, name}
_last_montage: dict[int, dict] = {}                      # chat -> {videos, plan, ts, request}
_transcript_cache: dict[str, dict] = {}                  # path -> {probe, words, segments}
montage_semaphore = asyncio.Semaphore(1)                 # один монтаж за раз — бережём память


def _now() -> float:
    return asyncio.get_event_loop().time()


def _cleanup_media(chat_id: int) -> None:
    t = _now()
    keep = []
    for v in _video_bin.get(chat_id, []):
        if t - v["ts"] < MEDIA_TTL and os.path.exists(v["path"]):
            keep.append(v)
        else:
            _transcript_cache.pop(v["path"], None)
            try:
                os.remove(v["path"])
            except OSError:
                pass
    _video_bin[chat_id] = keep[-20:]
    m = _music_bin.get(chat_id)
    if m and t - m["ts"] > MEDIA_TTL:
        _music_bin.pop(chat_id, None)


async def _store_media(context, file_id: str, chat_id: int, name: str) -> str:
    folder = os.path.join(MEDIA_DIR, str(chat_id))
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", name)[-60:] or "video.mp4"
    dst = os.path.join(folder, f"{int(_now() * 1000)}_{safe}")
    tg_file = await context.bot.get_file(file_id)
    src = tg_file.file_path
    if src and os.path.isabs(src) and os.path.exists(src):
        try:
            os.link(src, dst)  # без копирования гигабайтов, если диск тот же
            return dst
        except OSError:
            pass
    await save_telegram_file(tg_file, dst)
    return dst


def recent_videos(chat_id: int) -> list[str]:
    _cleanup_media(chat_id)
    return [v["path"] for v in _video_bin.get(chat_id, [])]


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Видео копим пачкой: несколько роликов подряд = один заказ (монтаж или разбор)."""
    msg = update.message
    video = msg.video or msg.document
    if video is None:
        return
    file_size = getattr(video, "file_size", None)
    if file_size and file_size > TELEGRAM_BOT_FILE_LIMIT:
        await msg.reply_text(f"Файл весит {file_size / 1048576:.0f} МБ — это больше лимита 2 ГБ. Пришли покороче.")
        return
    chat_id = update.effective_chat.id
    name = getattr(video, "file_name", None) or f"video_{msg.message_id}.mp4"
    _video_inflight[chat_id] += 1
    try:
        path = await _store_media(context, video.file_id, chat_id, name)
    except Exception:
        logger.exception("Video download error")
        await msg.reply_text("Не получилось скачать видео. Попробуй отправить ещё раз.")
        return
    finally:
        _video_inflight[chat_id] = max(0, _video_inflight[chat_id] - 1)
    _cleanup_media(chat_id)
    _video_bin[chat_id].append({"path": path, "name": name, "ts": _now()})

    caption = (msg.caption or "").strip()
    b = _video_batch.setdefault(chat_id, {"paths": [], "caption": "", "update": update, "task": None})
    b["paths"].append(path)
    if caption and not b["caption"]:
        b["caption"], b["update"] = caption, update
    if b["task"]:
        b["task"].cancel()
    b["task"] = asyncio.create_task(_flush_videos(chat_id))


async def _flush_videos(chat_id: int) -> None:
    try:
        await asyncio.sleep(VIDEO_BATCH_WAIT)
        for _ in range(120):  # ждём, пока докачаются остальные ролики пачки (до ~4 минут)
            if _video_inflight.get(chat_id, 0) <= 0:
                break
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        return
    b = _video_batch.pop(chat_id, None)
    if not b:
        return
    update, caption, paths = b["update"], b["caption"], b["paths"]
    try:
        if caption and MONTAGE_RE.search(caption) and MONTAGE_ENABLED:
            await run_montage(update, caption, paths)
        elif caption or not MONTAGE_ENABLED:
            await review_videos(update, paths, caption)
        else:
            _pending_videos[chat_id] = paths
            n = len(paths)
            what = "видео" if n == 1 else f"{n} видео"
            buttons = [[InlineKeyboardButton("🔍 Разобрать как продюсер", callback_data="vid:r")]]
            if MONTAGE_ENABLED:
                buttons.insert(0, [InlineKeyboardButton("🎬 Смонтировать рилс", callback_data="vid:m")])
            await update.effective_message.reply_text(
                f"Получила {what} 🎬 Что сделать?\n\n"
                "Можно и словами: «смонтируй рилс до 40 секунд», «начни с фразы про деньги», «разбери хук».",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
    except Exception:
        logger.exception("Video batch error")
        await update.effective_message.reply_text("Что-то пошло не так с видео. Попробуй ещё раз.")


async def on_video_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except BadRequest:
        pass
    chat_id = update.effective_chat.id
    paths = [p for p in (_pending_videos.pop(chat_id, None) or recent_videos(chat_id)) if os.path.exists(p)]
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    if not paths:
        await update.effective_message.reply_text("Видео уже удалились из памяти (храню 3 часа). Пришли их ещё раз 🙏")
        return
    if q.data == "vid:m":
        await run_montage(update, "Смонтируй рилс", paths)
    else:
        await review_videos(update, paths, "")


async def review_videos(update: Update, paths: list[str], caption: str) -> None:
    """Продюсерский разбор одного или нескольких роликов (речь + кадры) — одним ответом."""
    if not await _heavy_ok(update):
        return
    _set_state(update.effective_chat.id, "last_video", datetime.datetime.now(datetime.timezone.utc).timestamp())
    status = await update.effective_message.reply_text("🎬 Смотрю и слушаю ролик — проверю перед публикацией. Это займёт до минуты ⏳")
    await update.effective_chat.send_action("typing")
    paths = paths[:6]
    per_clip = max(2, social.MAX_FRAMES // len(paths)) if len(paths) > 1 else social.MAX_FRAMES
    summary_lines, images = [], []
    for i, path in enumerate(paths, 1):
        duration = await social.video_duration(path)
        if duration > VIDEO_MAX_SECONDS:
            summary_lines.append(f"[Видео {i}: {duration / 60:.0f} мин — слишком длинное, пропускаю]")
            continue
        frames = await social.extract_frames(path, count=per_clip)
        if duration >= 3:
            step = (duration - 0.6) / max(per_clip - 1, 1)
            frame_times = [0.3 + k * step for k in range(len(frames))]
        else:
            frame_times = [duration / 2] * len(frames)
        transcript = ""
        if openai_client is not None:
            mp3 = path + ".review.mp3"
            try:
                if await montage.extract_audio(path, mp3) and os.path.getsize(mp3) <= WHISPER_MAX_BYTES:
                    _, segs = await montage.transcribe_words(openai_client, mp3)
                    transcript = "\n".join(f"[{sg['start']:.1f}–{sg['end']:.1f} с] {sg['text']}" for sg in segs)
            except Exception:
                logger.exception("Video transcription error")
            finally:
                try:
                    os.remove(mp3)
                except OSError:
                    pass
        label = f"Видео {i} · {duration:.0f} сек" if len(paths) > 1 else f"Видео от Анны · {duration:.0f} сек"
        summary_lines.append(f"[{label}]\nРасшифровка речи:\n{transcript[:5000]}" if transcript
                             else f"[{label}] Речи нет (или не распознана) — смысл в картинке и тексте на экране.")
        for fr, ft in zip(frames, frame_times):
            images.append((label, fr, ft))
    if not images and not any("Расшифровка" in s for s in summary_lines):
        await status.edit_text("Не получилось разобрать видео — возможно, файл повреждён. Попробуй другой.")
        return
    try:
        await status.edit_text("✅ Посмотрела. Разбираю как продюсер...")
    except BadRequest:
        pass
    summary = "\n\n".join(summary_lines)
    content: list[dict] = [{"type": "text", "text": summary}]
    last_label = None
    for label, fr, ft in images:
        if label != last_label:
            content.append({"type": "text", "text": f"Кадры: {label} (по порядку, первый — хук)"})
            last_label = label
        content.append({"type": "text", "text": f"кадр на {ft:.1f} с"})
        content.append(social.image_block(fr))
    content.append({"type": "text", "text": VIDEO_REVIEW_PROMPT + (f"\n\nПодпись/вопрос Анны к видео: {caption}" if caption else "")})
    note = summary[:2000] + (f"\n\n{caption}" if caption else "\n\n[Проверь моё видео перед публикацией]")
    await ask_claude(update, update.effective_chat.id, content, history_note=note, max_tokens=4000, edit=False)


def _remember_in_history(chat_id: int, user_note: str, bot_note: str) -> None:
    history = conversations.get(chat_id, []) + [
        {"role": "user", "content": user_note},
        {"role": "assistant", "content": bot_note},
    ]
    conversations[chat_id] = _sanitize_history(history[-MAX_HISTORY_MESSAGES:])
    _save_conversations()


async def _prepare_clip(path: str, idx: int) -> montage.Clip:
    cached = _transcript_cache.get(path)
    name = re.sub(r"^\d+_", "", os.path.basename(path))
    if cached:
        info, words, segs = cached["probe"], cached["words"], cached["segments"]
    else:
        info = await montage.probe(path)
        words, segs = [], []
        if info["has_audio"] and openai_client is not None:
            mp3 = path + ".words.mp3"
            try:
                if await montage.extract_audio(path, mp3) and os.path.getsize(mp3) <= WHISPER_MAX_BYTES:
                    words, segs = await montage.transcribe_words(openai_client, mp3)
            except Exception:
                logger.exception("Word-level transcription error")
            finally:
                try:
                    os.remove(mp3)
                except OSError:
                    pass
        _transcript_cache[path] = {"probe": info, "words": words, "segments": segs}
    return montage.Clip(path, name or f"клип {idx}", info["duration"], info["width"], info["height"],
                        info["has_audio"], list(words), list(segs))


async def save_music(update: Update, context, media, name: str) -> None:
    """Трек для фоновой музыки монтажа. Если недавно был монтаж — сразу пересобираю его с музыкой."""
    chat_id = update.effective_chat.id
    try:
        path = await _store_media(context, media.file_id, chat_id, name)
    except Exception:
        logger.exception("Music download error")
        await update.effective_message.reply_text("Не получилось скачать трек. Попробуй ещё раз.")
        return
    old = _music_bin.get(chat_id)
    if old and old["path"] != path:
        try:
            os.remove(old["path"])
        except OSError:
            pass
    _music_bin[chat_id] = {"path": path, "ts": _now(), "name": name}
    last = _last_montage.get(chat_id)
    if last and _now() - last["ts"] < 2 * 3600 and all(os.path.exists(p) for p in last["videos"]):
        await update.effective_message.reply_text("🎵 Трек получила — пересобираю последний рилс с музыкой.")
        await run_montage(update, last.get("request", ""), last["videos"], previous=last["plan"], reuse_plan=True)
    else:
        await update.effective_message.reply_text(
            "🎵 Сохранила трек — добавлю его фоном в следующий монтаж (тихо, под голос). "
            "Чтобы смонтировать без музыки, напиши «без музыки».")


async def run_montage(update: Update, request: str, paths: list[str], previous: dict | None = None,
                      reuse_plan: bool = False) -> None:
    chat_id = update.effective_chat.id
    paths = [p for p in paths if os.path.exists(p)][:12]
    if not paths:
        await update.effective_message.reply_text("Не вижу видео для монтажа. Пришли ролики (можно несколько) 🎬")
        return
    n = len(paths)
    status = await update.effective_message.reply_text(
        f"🎬 Монтирую рилс из {n} видео. Это займёт несколько минут — можно пока заниматься делами ⏳\n\n1/4 · Слушаю речь…"
    )

    async def st(text: str):
        try:
            await status.edit_text(text)
        except BadRequest:
            pass

    if montage_semaphore.locked():
        await st("⏳ Сейчас доделываю другой монтаж — твой следующий в очереди…")
    async with montage_semaphore:
        workdir = tempfile.mkdtemp(dir=MEDIA_DIR if os.path.isdir(MEDIA_DIR) else None)
        typing_task = asyncio.create_task(_keep_typing(update))
        try:
            clips = []
            for i, p in enumerate(paths, 1):
                c = await _prepare_clip(p, i)
                if c.duration > VIDEO_MAX_SECONDS:
                    continue
                clips.append(c)
            if not clips:
                await st("Видео слишком длинные (больше 15 минут) или не читаются. Пришли покороче 🙏")
                return

            low = (request or "").lower()
            subtitles = "без субтитр" not in low
            cut_pauses = "не вырезай" not in low
            if reuse_plan and previous:
                plan = previous
            else:
                await st(f"🎬 Монтирую рилс из {len(clips)} видео ⏳\n\n2/4 · Выбираю лучшие куски и дубли…")
                plan = None
                if any(c.segments for c in clips):
                    try:
                        resp = await client.messages.create(
                            model=CLAUDE_MODEL, max_tokens=3000, system=montage.PLANNER_SYSTEM,
                            messages=[{"role": "user", "content": montage.planner_input(clips, request, previous)}],
                        )
                        _log_usage("монтаж", resp)
                        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                        plan = montage.parse_plan(text, clips)
                    except Exception:
                        logger.exception("Montage planner error")
                if plan is None:
                    plan = montage.fallback_plan(clips)
            pieces = montage.build_pieces(plan, clips, cut_pauses=cut_pauses)

            music = None
            m = _music_bin.get(chat_id)
            if m and os.path.exists(m["path"]) and "без музык" not in low:
                music = m["path"]

            await st(f"🎬 Монтирую рилс ⏳\n\n3/4 · Режу и склеиваю: {len(pieces)} кусков…")

            async def progress(done: int, total: int):
                await st(f"🎬 Монтирую рилс ⏳\n\n3/4 · Режу и склеиваю: {done}/{total}…")

            out, total = await montage.render(clips, pieces, workdir, hook=plan.get("hook", ""),
                                              subtitles=subtitles, music_path=music, progress=progress)
            await st("🎬 Почти готово ⏳\n\n4/4 · Отправляю…")
            lines = [f"🎬 Готово · {total:.0f} сек"]
            if plan.get("hook"):
                lines.append(f"Хук: «{plan['hook']}»")
            if plan.get("notes"):
                lines.append(plan["notes"][:300])
            lines.append(
                "\nЧто поправить? Напиши: «короче», «другой хук», «без субтитров», «начни с фразы про…»"
                + ("" if music else " — или пришли трек, добавлю музыку 🎵")
            )
            with open(out, "rb") as f:
                await update.effective_chat.send_video(
                    video=f, caption="\n".join(lines)[:1000], width=montage.OUT_W, height=montage.OUT_H,
                    duration=int(total), supports_streaming=True, read_timeout=600, write_timeout=600,
                )
            try:
                await status.delete()
            except BadRequest:
                pass
            if plan.get("caption"):
                await send_long(update, "✍️ **Подпись к посту:**\n\n" + plan["caption"])
            _last_montage[chat_id] = {"videos": paths, "plan": plan, "ts": _now(), "request": request}
            _remember_in_history(
                chat_id, f"[Анна прислала {len(clips)} видео и попросила смонтировать рилс] {request}",
                f"[Смонтировала рилс {total:.0f} сек. Хук: «{plan.get('hook', '')}». {plan.get('notes', '')}]\n"
                f"Подпись: {plan.get('caption', '')[:1500]}",
            )
        except montage.MontageError as e:
            await st(f"Не получилось смонтировать: {e}. Попробуй ещё раз или пришли видео покороче.")
        except Exception:
            logger.exception("Montage error")
            await st("Не получилось смонтировать видео 😕 Попробуй ещё раз через пару минут.")
        finally:
            typing_task.cancel()
            shutil.rmtree(workdir, ignore_errors=True)


def montage_followup(chat_id: int, text: str) -> str | None:
    """Распознаёт просьбу смонтировать/поправить монтаж. 'new' | 'edit' | None."""
    if not MONTAGE_ENABLED:
        return None
    last = _last_montage.get(chat_id)
    fresh = last and _now() - last["ts"] < 2 * 3600
    if MONTAGE_RE.search(text) and recent_videos(chat_id):
        return "edit" if fresh and MONTAGE_EDIT_RE.search(text) else "new"
    if fresh and _now() - last["ts"] < 30 * 60 and MONTAGE_EDIT_RE.search(text) and len(text) < 300:
        return "edit"
    return None


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
    remember_photos(update.effective_chat.id, photos)
    try:
        if caption and PHOTO_CAROUSEL_RE.search(caption):
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

    remember_photos(update.effective_chat.id, [data])
    if caption and PHOTO_CAROUSEL_RE.search(caption):
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
    if is_image:
        remember_photos(update.effective_chat.id, [data])
    if is_image and caption and PHOTO_CAROUSEL_RE.search(caption):
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
    if MUSIC_CAPTION_RE.search(update.message.caption or ""):
        await save_music(update, context, doc, doc.file_name or "music.mp3")
        return
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


def _state_path() -> str:
    return os.path.join(DATA_DIR, "chat_state.json")


def _all_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _get_state(chat_id: int) -> dict:
    return _all_state().get(str(chat_id), {})


def _set_state(chat_id: int, key: str, value) -> None:
    d = _all_state()
    d.setdefault(str(chat_id), {})[key] = value
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        logger.exception("chat state save error")


PLAN_PROMPT = """Составь Анне контент-план на неделю (понедельник–воскресенье): 7 рилсов + сторис.
Выше — залетевшие ролики ниши и конкурентов, расшифровка их первых секунд и её собственные хиты и провалы
(если блоки есть). Её продукты, курсы, интенсивы, ретриты и мероприятия — в блоке памяти (если есть).

Распределение: 3 дня — развитие её собственных хитов (тема/формат, новый угол), 3 дня — механики залетевших
роликов конкурентов на её темы, 1 день — ответ на частый вопрос её аудитории.

Для каждого дня одним компактным блоком:
**День — тема**
- Хук (первая фраза дословно, первые 2 секунды) + текст на экране до 6 слов;
- Формат и длина (до 60 секунд), язык (укр/рус — по тому, какой язык у её хитов);
- Сторис дня: 2–3 сторис, которые ведут к её ближайшему продукту или мероприятию (прогрев: польза → история →
  отзыв/результат ученицы → приглашение). Если ближайших продуктов в памяти нет — сторис на доверие и близость.

В конце:
- «Совет: сними 2–3 ролика за один раз — так проще держать ритм 1 рилс в день.»
- Если в памяти нет её ближайших курсов, интенсивов, ретритов и мероприятий с датами — спроси: «Какие у тебя
  курсы, интенсивы и мероприятия в ближайший месяц? Напиши "Запомни: …" — буду вести к ним прогрев в сторис».
Весь ответ — до ~5000 знаков. Не ссылайся на «алгоритм», досмотры и охваты — объясняй через зрителя.
Личные истории не выдумывай — [вставь свою историю: …]. Пояснения на русском."""


async def run_plan(update) -> None:
    chat_id = update.effective_chat.id
    if not await _heavy_ok(update):
        return
    status = await update.effective_message.reply_text("🗓 Собираю контент-план на неделю: смотрю, что залетает у конкурентов и у тебя. 1–3 минуты ⏳")
    try:
        result = await trends.collect(DATA_DIR)
    except Exception:
        logger.exception("Plan trends error")
        await status.edit_text("Не получилось собрать данные для плана. Попробуй ещё раз через пару минут: /plan")
        return
    summary = trends.trends_text(result)
    hooks = await _hook_transcripts(result["rows"])
    if hooks:
        summary += "\n\n[Что говорят в первые секунды залетевших роликов — дословная расшифровка]\n" + hooks
    own = brief.own_hits_text(DATA_DIR)
    if own:
        summary += "\n\n" + own
    try:
        await status.edit_text("✅ Данные собрала. Пишу план…")
    except BadRequest:
        pass
    summary += "\n\n" + launches.today_block(datetime.datetime.now(audit.LOCAL_TZ).date())
    summary += ("\n(Это фаза на сегодня. Для каждого дня недели посчитай фазу сам по датам запусков: 22–45 дн. до старта — "
                "ФОН, 15–21 — РАЗОГРЕВ, 8–14 — ПРОГРЕВ, 3–7 — ПРОДАЖА, 1–2 — ДЕДЛАЙН, 0 — СТАРТ; сторис дня — по фазе.)")
    content = summary + "\n\n" + PLAN_PROMPT
    await ask_claude(update, chat_id, content, history_note="[Контент-план на неделю]", max_tokens=7000, edit=False)


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_plan(update)


async def sunday_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for cid, on in _load_subs().items():
        if not on:
            continue
        mode = _get_state(int(cid)).get("plan")
        try:
            if mode == "weekly":
                await run_plan(_ChatProxy(context.bot, int(cid)))
            elif mode is None:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗓 План на всю неделю (по воскресеньям)", callback_data="plan:w")],
                    [InlineKeyboardButton("🌅 Каждое утро по одной теме", callback_data="plan:d")],
                ])
                await context.bot.send_message(
                    int(cid),
                    "Как тебе удобнее снимать?\n\n• План на неделю: по воскресеньям присылаю 7 тем с хуками и сторис — "
                    "можно снять 2–3 ролика за раз.\n• Или каждое утро по одной теме вместе со сводкой.",
                    reply_markup=kb,
                )
        except Exception:
            logger.exception("sunday job error for %s", cid)


async def on_plan_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except BadRequest:
        pass
    chat_id = update.effective_chat.id
    if q.data == "plan:w":
        _set_state(chat_id, "plan", "weekly")
        await update.effective_message.reply_text("Отлично! Каждое воскресенье пришлю план на неделю. Первый — прямо сейчас 👇")
        await run_plan(update)
    else:
        _set_state(chat_id, "plan", "daily")
        await update.effective_message.reply_text(
            "Договорились: каждое утро вместе со сводкой — одна тема дня с хуком. План на неделю можно попросить в любой момент: /plan"
        )


# ---------------------------------------------------------------------------
# Сам пишет: утренняя сводка каждый день и итоги недели по понедельникам
# ---------------------------------------------------------------------------
MORNING_HOUR = int(os.environ.get("MORNING_HOUR", "9"))  # по времени Анны (audit.LOCAL_TZ)
MORNING_ENABLED = os.environ.get("MORNING_ENABLED", "1") != "0"


def _subs_path() -> str:
    return os.path.join(DATA_DIR, "morning_chats.json")


def _load_subs() -> dict:
    try:
        with open(_subs_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_subs(d: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_subs_path(), "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        logger.exception("morning subs save error")


def _subscribe(chat_id: int) -> None:
    d = _load_subs()
    if str(chat_id) not in d:  # кто однажды попросил сводку — получает её каждое утро (выключить: /stop_morning)
        d[str(chat_id)] = True
        _save_subs(d)


class _ChatProxy:
    """Мини-«Update» для сообщений по расписанию: бот пишет сам, без входящего сообщения."""

    def __init__(self, bot, chat_id: int):
        self.id = chat_id
        self._bot = bot
        self.effective_chat = self
        self.effective_message = self
        self.message = self

    async def send_action(self, action):
        try:
            await self._bot.send_chat_action(self.id, action)
        except Exception:
            pass

    async def reply_text(self, text, **kw):
        kw.pop("reply_to_message_id", None)
        kw.pop("quote", None)
        return await self._bot.send_message(self.id, text, **kw)


async def alerts_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _flush_alerts(context.bot)


async def morning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for cid, on in _load_subs().items():
        if not on:
            continue
        try:
            _brief_cache.clear()
            ask = "Доброе утро! (план пришёл сам, по расписанию)"
            st = _get_state(int(cid))
            last_v = st.get("last_video")
            now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
            if not last_v or now_ts - last_v > 2 * 86400:
                ask += " [Заметка бота: Анна уже 2+ дня не присылала готовое видео на проверку перед публикацией.]"
            if st.get("plan") == "daily":
                ask += " [Анна выбрала режим «каждый день по одной теме».]"
            await run_brief(_ChatProxy(context.bot, int(cid)), ask, daily=True)
        except Exception:
            logger.exception("morning job error for %s", cid)


async def weekly_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await iginsights.refresh_token(DATA_DIR)
    text = brief.weekly_text(DATA_DIR)
    for cid in _owner_chats():
        try:
            await context.bot.send_message(cid, f"Расходы бота на ИИ за неделю: ≈ ${spend_week():.2f}. Подробно: /balance")
        except Exception:
            logger.exception("owner weekly spend error")
    if not text:
        return
    for cid, on in _load_subs().items():
        if on:
            try:
                await context.bot.send_message(int(cid), text, disable_web_page_preview=True)
            except Exception:
                logger.exception("weekly job error for %s", cid)


async def stop_morning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = _load_subs()
    d[str(update.effective_chat.id)] = False
    _save_subs(d)
    await update.message.reply_text("Ок, утренние сводки выключены. Включить снова: /morning")


async def start_morning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = _load_subs()
    d[str(update.effective_chat.id)] = True
    _save_subs(d)
    await update.message.reply_text(
        f"Готово! Каждое утро в {MORNING_HOUR}:00 пришлю план съёмки: что просело и почему, сколько рилсов снять, "
        "хуки и сценарии. По понедельникам — итоги недели. "
        "Выключить: /stop_morning"
    )


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = brief.weekly_text(DATA_DIR)
    await update.message.reply_text(text or "Пока мало данных для недельного отчёта — нужно хотя бы 2 дня сводок.",
                                    disable_web_page_preview=True)


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
    app.add_handler(CommandHandler("morning", guarded(start_morning)))
    app.add_handler(CommandHandler("stop_morning", guarded(stop_morning)))
    app.add_handler(CommandHandler("week", guarded(week_command)))
    app.add_handler(CommandHandler("plan", guarded(plan_command)))
    app.add_handler(CommandHandler("today", guarded(today_command)))
    app.add_handler(CommandHandler("owner", guarded(owner_command)))
    app.add_handler(CommandHandler("balance", guarded(balance_command)))
    app.add_handler(CallbackQueryHandler(guarded(on_plan_button), pattern=r"^plan:"))
    if MORNING_ENABLED and app.job_queue is not None:
        t = datetime.time(hour=MORNING_HOUR, minute=0, tzinfo=audit.LOCAL_TZ)
        app.job_queue.run_daily(morning_job, t, name="morning")
        app.job_queue.run_daily(weekly_job, datetime.time(hour=MORNING_HOUR, minute=5, tzinfo=audit.LOCAL_TZ),
                                days=(1,), name="weekly")  # PTB 21: 0 = воскресенье, 1 = понедельник
        app.job_queue.run_repeating(alerts_job, interval=300, first=60, name="alerts")
        app.job_queue.run_daily(sunday_job, datetime.time(hour=18, minute=0, tzinfo=audit.LOCAL_TZ),
                                days=(0,), name="sunday_plan")
        logger.info("Утренняя сводка: каждый день в %s:00", MORNING_HOUR)
    elif MORNING_ENABLED:
        logger.warning("JobQueue недоступна — установи python-telegram-bot[job-queue]")
    app.add_handler(CommandHandler("help", guarded(help_command)))
    app.add_handler(CommandHandler("memory", guarded(memory_command)))
    app.add_handler(CommandHandler("forget", guarded(forget_command)))
    app.add_handler(CommandHandler("audit", guarded(audit_command)))
    app.add_handler(CommandHandler(["trends", "hooks"], guarded(trends_command)))
    app.add_handler(CommandHandler("competitors", guarded(competitors_command)))
    app.add_handler(CommandHandler(["carousel", "karusel"], guarded(carousel_command)))
    app.add_handler(CommandHandler(["designs", "design"], guarded(designs_command)))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, guarded(handle_video)))
    app.add_handler(CallbackQueryHandler(guarded(on_video_button), pattern=r"^vid:"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, guarded(handle_voice)))
    app.add_handler(MessageHandler(filters.PHOTO, guarded(handle_photo)))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.VIDEO, guarded(handle_document)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guarded(handle_message)))
    app.add_error_handler(on_error)
    logger.info("Bot started (model=%s, access=%s)", CLAUDE_MODEL, "restricted" if ALLOWED_USER_IDS else "open")
    app.run_polling()


if __name__ == "__main__":
    main()
