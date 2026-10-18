"""Банк реальных историй и фактов Анны — то, на чём бот пишет живые сценарии.

Анна пишет или надиктовывает: «Запомни: …» — бот сохраняет навсегда (переживает перезапуски).
/memory — посмотреть, /forget N — удалить запись.
"""
import os
import re
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

LOCAL_TZ = timezone(timedelta(hours=int(os.environ.get("CLIENT_UTC_OFFSET", "8"))))
MAX_ITEMS = 200
MAX_PROMPT_CHARS = 9000  # сколько фактов максимум подмешиваем в промпт (свежие — в приоритете)

TRIGGER_RE = re.compile(
    r"^\s*(запомни|сохрани|добавь в память|история для контента|моя история|факт обо мне|факт)\b"
    r"(\s+(историю|это|факт|себе|что)\b)?\s*[:\-—,.]?\s*",
    re.I,
)


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, "facts.json")


def load(data_dir: str) -> list[dict]:
    try:
        with open(_path(data_dir), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception:
        logger.exception("Не удалось прочитать facts.json")
        return []


def _save(data_dir: str, items: list[dict]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    tmp = _path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items[-MAX_ITEMS:], f, ensure_ascii=False)
    os.replace(tmp, _path(data_dir))


def extract(text: str) -> str | None:
    """Если сообщение — просьба запомнить, возвращает что именно запомнить."""
    m = TRIGGER_RE.match(text or "")
    if not m:
        return None
    rest = text[m.end():].strip()
    if re.search(r"https?://", rest):  # «сохрани этот рилс <ссылка>» — это не факт о себе
        return None
    return rest if len(rest) >= 15 else None


def add(data_dir: str, text: str) -> int:
    items = load(data_dir)
    items.append({"text": text.strip()[:4000], "date": datetime.now(LOCAL_TZ).strftime("%d.%m.%Y")})
    _save(data_dir, items)
    return len(items)


def remove(data_dir: str, index: int) -> dict | None:
    items = load(data_dir)
    if 1 <= index <= len(items):
        removed = items.pop(index - 1)
        _save(data_dir, items)
        return removed
    return None


def prompt_block(data_dir: str) -> str:
    """Блок для промпта: реальные истории и факты, свежие в приоритете."""
    items = load(data_dir)
    if not items:
        return ""
    lines, total = [], 0
    for i, it in enumerate(reversed(items)):
        line = f"- ({it.get('date', '')}) {it['text']}"
        if total + len(line) > MAX_PROMPT_CHARS:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(reversed(lines))
