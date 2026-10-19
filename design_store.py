"""Сохранённые дизайны каруселей («запомни этот дизайн как …») и последний дизайн в каждом чате.

Лежит в DATA_DIR/designs.json (Railway Volume — переживает redeploy).
"""
import json
import os
import threading

_lock = threading.Lock()


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, "designs.json")


def _load(data_dir: str) -> dict:
    try:
        with open(_path(data_dir), encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("saved", {})
                d.setdefault("last", {})
                return d
    except (OSError, json.JSONDecodeError):
        pass
    return {"saved": {}, "last": {}}


def _save(data_dir: str, d: dict) -> None:
    os.makedirs(data_dir, exist_ok=True)
    tmp = _path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(data_dir))


def norm(name: str) -> str:
    return " ".join(str(name).strip().strip("«»\"'.!").lower().split())[:40]


def saved(data_dir: str) -> dict:
    with _lock:
        return _load(data_dir)["saved"]


def save_design(data_dir: str, name: str, design: dict) -> str:
    key = norm(name)
    with _lock:
        d = _load(data_dir)
        d["saved"][key] = design
        _save(data_dir, d)
    return key


def delete(data_dir: str, name: str) -> bool:
    key = norm(name)
    with _lock:
        d = _load(data_dir)
        if key not in d["saved"]:
            return False
        del d["saved"][key]
        _save(data_dir, d)
    return True


def last(data_dir: str, chat_id: int) -> dict | None:
    with _lock:
        return _load(data_dir)["last"].get(str(chat_id))


def set_last(data_dir: str, chat_id: int, design: dict) -> None:
    with _lock:
        d = _load(data_dir)
        d["last"][str(chat_id)] = design
        _save(data_dir, d)


def prompt_block(data_dir: str) -> str:
    items = saved(data_dir)
    if not items:
        return "Сохранённых дизайнов пока нет."
    lines = [f"- «{k}»: {json.dumps(v, ensure_ascii=False)}" for k, v in items.items()]
    return "Сохранённые дизайны Анны (используй {\"saved\": \"имя\"}):\n" + "\n".join(lines)
