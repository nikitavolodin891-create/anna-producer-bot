"""Официальная статистика Instagram через API Meta (ключ IG_ACCESS_TOKEN в переменных Railway).

Только чтение: профиль, публикации, статистика роликов (охват, просмотры, сохранения, репосты,
среднее время просмотра) и комментарии. Ключ Instagram живёт 60 дней — раз в неделю продлеваем
его сами и храним свежий в DATA_DIR/ig_token.json.
"""
from __future__ import annotations

import json
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

IG_USER_ID = os.environ.get("IG_USER_ID", "")  # нужен только для ключа Facebook (EAA…)
API_VERSION = os.environ.get("IG_API_VERSION", "v23.0")
REEL_METRICS = ["reach", "views", "saved", "shares", "total_interactions", "ig_reels_avg_watch_time"]
POST_METRICS = ["reach", "views", "saved", "shares", "total_interactions"]


def _tok_path(data_dir: str) -> str:
    return os.path.join(data_dir, "ig_token.json")


def get_token(data_dir: str) -> str:
    env = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    try:
        with open(_tok_path(data_dir), encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("env_hint") == env[-6:] and saved.get("token"):
            return saved["token"]  # продлённый ключ от того же исходного
    except (OSError, json.JSONDecodeError):
        pass
    return env


def enabled(data_dir: str) -> bool:
    return bool(get_token(data_dir))


def _base(token: str) -> tuple[str, str]:
    if token.startswith("EAA"):  # ключ Facebook Login → graph.facebook.com/{ig_user_id}
        return f"https://graph.facebook.com/{API_VERSION}", IG_USER_ID or "me"
    return f"https://graph.instagram.com/{API_VERSION}", "me"


async def _get(http: httpx.AsyncClient, url: str, params: dict) -> dict:
    r = await http.get(url, params=params)
    data = r.json()
    if r.status_code != 200 or "error" in data:
        raise RuntimeError((data.get("error") or {}).get("message", f"HTTP {r.status_code}"))
    return data


async def refresh_token(data_dir: str) -> bool:
    """Продлить ключ Instagram ещё на 60 дней (раз в неделю достаточно)."""
    env = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    token = get_token(data_dir)
    if not token or token.startswith("EAA"):
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            data = await _get(http, "https://graph.instagram.com/refresh_access_token",
                              {"grant_type": "ig_refresh_token", "access_token": token})
        os.makedirs(data_dir, exist_ok=True)
        with open(_tok_path(data_dir), "w", encoding="utf-8") as f:
            json.dump({"token": data["access_token"], "env_hint": env[-6:], "ts": time.time()}, f)
        return True
    except Exception as e:
        logger.warning("Не удалось продлить ключ Instagram: %s", e)
        return False


async def _media_insights(http, base: str, token: str, m: dict) -> dict:
    is_reel = m.get("media_product_type") == "REELS"
    metrics = REEL_METRICS if is_reel else POST_METRICS
    out: dict = {}
    try:
        data = await _get(http, f"{base}/{m['id']}/insights", {"metric": ",".join(metrics), "access_token": token})
        for it in data.get("data", []):
            vals = it.get("values") or [{}]
            out[it["name"]] = vals[0].get("value", it.get("total_value", {}).get("value"))
    except Exception:
        for metric in metrics:  # какая-то метрика недоступна — берём по одной
            try:
                data = await _get(http, f"{base}/{m['id']}/insights", {"metric": metric, "access_token": token})
                it = data["data"][0]
                out[metric] = (it.get("values") or [{}])[0].get("value")
            except Exception:
                pass
    return out


async def fetch(data_dir: str, limit: int = 12, comments_for: int = 5) -> dict:
    """Профиль + последние публикации со статистикой + свежие комментарии."""
    token = get_token(data_dir)
    if not token:
        raise RuntimeError("нет ключа IG_ACCESS_TOKEN")
    base, uid = _base(token)
    async with httpx.AsyncClient(timeout=40) as http:
        prof = await _get(http, f"{base}/{uid}", {
            "fields": "username,followers_count,media_count", "access_token": token})
        media = await _get(http, f"{base}/{uid}/media", {
            "fields": "id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count",
            "limit": limit, "access_token": token})
        items = media.get("data", [])
        for m in items:
            m["insights"] = await _media_insights(http, base, token, m)
        comments = []
        for m in items[:comments_for]:
            if not m.get("comments_count"):
                continue
            try:
                c = await _get(http, f"{base}/{m['id']}/comments", {
                    "fields": "text,timestamp,username", "limit": 30, "access_token": token})
                for x in c.get("data", []):
                    comments.append({"text": x.get("text", ""), "reel": m.get("permalink"),
                                     "cap": (m.get("caption") or "")[:60]})
            except Exception:
                pass
    return {"profile": prof, "media": items, "comments": comments}


def _n(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f} млн"
    if x >= 1000:
        return f"{x / 1000:.1f} тыс."
    return f"{x:.0f}"


def insights_text(data: dict, durations: dict[str, float] | None = None) -> str:
    """Факты для модели: внутренняя статистика по каждому ролику — посчитано кодом."""
    durations = durations or {}
    lines = ["[Официальная статистика Instagram (видит только владелец аккаунта)]",
             f"Подписчики: {_n(data['profile'].get('followers_count'))}"]
    for m in data["media"]:
        ins = m.get("insights", {})
        reach = ins.get("reach") or 0
        cap = (m.get("caption") or "").replace("\n", " ")[:70]
        when = (m.get("timestamp") or "")[:10]
        parts = [f"охват {_n(reach)}", f"просмотры {_n(ins.get('views'))}"]
        if reach:
            if ins.get("saved") is not None:
                parts.append(f"сохранения {_n(ins['saved'])} ({ins['saved'] / reach * 100:.1f}% охвата)")
            if ins.get("shares") is not None:
                parts.append(f"репосты {_n(ins['shares'])} ({ins['shares'] / reach * 100:.1f}%)")
        aw = ins.get("ig_reels_avg_watch_time")
        if aw:
            sec = aw / 1000
            dur = durations.get((m.get("permalink") or "").rstrip("/").split("/")[-1])
            parts.append(f"смотрят в среднем {sec:.1f} с" + (f" из {dur:.0f} с ({sec / dur * 100:.0f}%)" if dur else ""))
        kind = "рилс" if m.get("media_product_type") == "REELS" else "пост"
        lines.append(f"- {when} · {kind} · " + ", ".join(parts) + f"\n  «{cap}» {m.get('permalink', '')}")
    qs = [c for c in data.get("comments", []) if "?" in c["text"] or len(c["text"]) > 60]
    if qs:
        lines.append("\nВопросы и развёрнутые комментарии зрителей (идеи для роликов-ответов):")
        for c in qs[:12]:
            lines.append(f"- «{c['text'][:200]}» (под роликом «{c['cap']}»)")
    return "\n".join(lines)
