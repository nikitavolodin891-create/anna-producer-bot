"""Внутренняя статистика Instagram через vidIQ (ключ VIDIQ_API_KEY в переменных Railway).

Instagram Анны уже подключён к vidIQ как владелец, поэтому vidIQ отдаёт то, что видит только хозяин аккаунта:
сколько людей пролистали ролик в первые секунды (skip rate), какую долю ролика смотрят в среднем,
репосты и сохранения. Ключ vidIQ только читает данные — публиковать или менять что-то через него нельзя.
Запрос статистики владельца в vidIQ стоит 0 кредитов.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
import datetime

import httpx

logger = logging.getLogger(__name__)

MCP_URL = os.environ.get("VIDIQ_MCP_URL", "https://mcp.vidiq.com/mcp")
IG_ACCOUNT = os.environ.get("VIDIQ_IG_ACCOUNT", "28343978111904133")  # platformAccountId @anyalakshmi в vidIQ
CACHE_SECONDS = 2 * 3600
_cache: dict = {}


def enabled() -> bool:
    return bool(os.environ.get("VIDIQ_API_KEY", "").strip())


class VidiqError(RuntimeError):
    pass


def _parse(r: httpx.Response) -> dict:
    """Ответ MCP приходит либо JSON, либо потоком событий (text/event-stream) — берём последнее сообщение с id."""
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        msg: dict = {}
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if "id" in obj:
                    msg = obj
        return msg
    try:
        return r.json()
    except ValueError:
        return {}


async def _call_tool(name: str, arguments: dict) -> dict:
    key = os.environ.get("VIDIQ_API_KEY", "").strip()
    if not key:
        raise VidiqError("нет ключа VIDIQ_API_KEY")
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60) as http:
        init_req = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "anna-producer-bot", "version": "1.0"}},
        }
        r = await http.post(MCP_URL, headers=headers, json=init_req)
        if r.status_code in (401, 403):  # другой способ передать ключ
            alt = {k: v for k, v in headers.items() if k != "Authorization"}
            alt["X-API-Key"] = key
            r2 = await http.post(MCP_URL, headers=alt, json=init_req)
            if r2.status_code < 400:
                headers, r = alt, r2
        if r.status_code in (401, 403):
            raise VidiqError(f"vidIQ не принял ключ (HTTP {r.status_code}) — проверь VIDIQ_API_KEY")
        if r.status_code >= 400:
            raise VidiqError(f"vidIQ initialize HTTP {r.status_code}")
        init = _parse(r)
        sid = r.headers.get("mcp-session-id")
        if sid:
            headers["Mcp-Session-Id"] = sid
        proto = ((init.get("result") or {}).get("protocolVersion")) or "2025-06-18"
        headers["MCP-Protocol-Version"] = proto
        await http.post(MCP_URL, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = await http.post(MCP_URL, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if r.status_code >= 400:
            raise VidiqError(f"vidIQ {name} HTTP {r.status_code}")
        msg = _parse(r)
    if msg.get("error"):
        raise VidiqError(str(msg["error"].get("message", msg["error"]))[:300])
    res = msg.get("result") or {}
    if res.get("structuredContent"):
        return res["structuredContent"]
    for block in res.get("content") or []:
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except (json.JSONDecodeError, TypeError):
                if res.get("isError"):
                    raise VidiqError(block["text"][:300])
    raise VidiqError("vidIQ вернул пустой ответ")


async def fetch_reels(limit: int = 15) -> dict:
    """Рилсы за последние 30 дней со статистикой владельца. Кэш 2 часа."""
    now = time.time()
    if _cache.get("t", 0) > now - CACHE_SECONDS and _cache.get("data"):
        return _cache["data"]
    data = await _call_tool("vidiq_instagram_owner_insights", {
        "platformAccountId": IG_ACCOUNT, "mode": "reels", "limit": limit,
        "userGoal": "daily content plan to grow a women's coaching Instagram account",
    })
    if data.get("reason") == "owner_token_inactive" or data.get("status") not in (None, "available"):
        raise VidiqError(f"доступ vidIQ к Instagram неактивен: {data.get('reason') or data.get('status')}")
    _cache.update(t=now, data=data)
    return data


def _pct(x) -> str:
    return "—" if x is None else f"{x:.0f}%"


def _bucket(d) -> str:
    if not d:
        return "?"
    if d <= 30:
        return "до 30 с"
    if d <= 60:
        return "31–60 с"
    return "длиннее 60 с"


def reels_text(data: dict, tz: datetime.tzinfo | None = None) -> str:
    """Факты для модели: посчитано кодом из внутренней статистики Instagram (через vidIQ)."""
    reels = data.get("reels") or []
    if not reels:
        return ""
    lines = ["[Внутренняя статистика Instagram — видит только владелец (через vidIQ). Цифрам можно доверять]",
             f"Подписчики: {data.get('followersCount', '—')}. Просмотры всех рилсов за 30 дней: "
             f"{(data.get('summary') or {}).get('views', '—')}.",
             "Пролистали сразу = доля зрителей, которые ушли в первые секунды (чем меньше, тем сильнее хук). "
             "Досмотр = какую долю ролика смотрят в среднем.",
             ""]
    for r in reels:
        cap = (r.get("caption") or "").replace("\n", " ")[:90]
        when = (r.get("timestamp") or "")[:16].replace("T", " ") + " UTC"
        try:
            dt = datetime.datetime.fromisoformat((r.get("timestamp") or "").replace("Z", "+00:00"))
            if tz:
                when = dt.astimezone(tz).strftime("%d.%m %H:%M")
        except ValueError:
            pass
        lines.append(
            f"- {when} · {r.get('durationSeconds', '?')} с · просмотры {r.get('playCount', '—')} · "
            f"пролистали сразу {_pct(r.get('skipRate'))} · досмотр {_pct(r.get('avgWatchedPct'))} "
            f"(≈{(r.get('avgWatchTimeMs') or 0) / 1000:.0f} с) · репосты {r.get('shares', '—')} · "
            f"сохранения {r.get('saves', '—')} · комментарии {r.get('commentCount', '—')}"
            + (" · пробный (показан только не подписчикам)" if r.get("isPossibleTrial") else "")
            + f"\n  «{cap}»"
        )
    groups: dict[str, list[dict]] = {}
    for r in reels:
        groups.setdefault(_bucket(r.get("durationSeconds")), []).append(r)
    lines.append("\nПо длине ролика (медианы):")
    for name in ("до 30 с", "31–60 с", "длиннее 60 с"):
        g = groups.get(name)
        if not g:
            continue
        def med(key):
            vals = [x[key] for x in g if x.get(key) is not None]
            return statistics.median(vals) if vals else None
        lines.append(f"- {name}: {len(g)} шт · просмотры {med('playCount'):.0f} · пролистали сразу {_pct(med('skipRate'))}"
                     f" · досмотр {_pct(med('avgWatchedPct'))} · репосты {med('shares') or 0:.0f}")
    skips = [r["skipRate"] for r in reels if r.get("skipRate") is not None]
    if skips:
        best = min(reels, key=lambda r: r.get("skipRate") if r.get("skipRate") is not None else 999)
        lines.append(f"\nМедиана «пролистали сразу»: {statistics.median(skips):.0f}%. Лучший хук: "
                     f"{best.get('skipRate'):.0f}% — «{(best.get('caption') or '')[:60]}».")
    return "\n".join(lines)
