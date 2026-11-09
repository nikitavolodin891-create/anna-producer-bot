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
import base64
import hashlib
import secrets
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

MCP_URL = os.environ.get("VIDIQ_MCP_URL", "https://mcp.vidiq.com/mcp")
IG_ACCOUNT = os.environ.get("VIDIQ_IG_ACCOUNT", "28343978111904133")  # platformAccountId @anyalakshmi в vidIQ
CACHE_SECONDS = 2 * 3600
_cache: dict = {}

# Статистика владельца Instagram в vidIQ доступна только через вход в vidIQ (OAuth), API-ключ её не открывает.
# Владелец бота один раз входит по ссылке из /vidiq — бот хранит ключ входа на своём диске и сам его продлевает.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
OAUTH_FILE = os.path.join(DATA_DIR, "vidiq_oauth.json")
AUTH_BASE = "https://mcp.vidiq.com/mcp"
REDIRECT_URI = "http://localhost:8765/callback"
RESOURCE = "https://mcp.vidiq.com/mcp"


def _load_oauth() -> dict:
    try:
        with open(OAUTH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_oauth(d: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OAUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f)


def connected() -> bool:
    return bool(_load_oauth().get("refresh_token") or _load_oauth().get("access_token"))


def enabled() -> bool:
    return connected() or bool(os.environ.get("VIDIQ_API_KEY", "").strip())


async def start_oauth() -> str:
    """Регистрирует бота в vidIQ и возвращает ссылку для входа."""
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(f"{AUTH_BASE}/register", json={
            "client_name": "Anna producer bot",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
        if r.status_code >= 400:
            raise VidiqError(f"vidIQ не дал зарегистрироваться (HTTP {r.status_code}): {r.text[:200]}")
        client_id = r.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    d = _load_oauth()
    d["pending"] = {"client_id": client_id, "verifier": verifier, "state": state, "ts": time.time()}
    _save_oauth(d)
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "scope": "api", "resource": RESOURCE,
    })
    return f"{AUTH_BASE}/authorize?{q}"


async def finish_oauth(text: str) -> None:
    """Принимает адрес, на который vidIQ перекинул после входа (localhost…?code=…), и получает ключ входа."""
    d = _load_oauth()
    pend = d.get("pending")
    if not pend:
        raise VidiqError("сначала получи ссылку: /vidiq")
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(text.strip()).query)
    code = (qs.get("code") or [""])[0]
    if not code:
        raise VidiqError("в адресе нет code=… — скопируй адрес целиком из строки браузера")
    if qs.get("state") and qs["state"][0] != pend["state"]:
        raise VidiqError("ссылка устарела — получи новую: /vidiq")
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(f"{AUTH_BASE}/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
            "client_id": pend["client_id"], "code_verifier": pend["verifier"], "resource": RESOURCE,
        })
    if r.status_code >= 400:
        raise VidiqError(f"vidIQ не принял код (HTTP {r.status_code}) — получи новую ссылку: /vidiq")
    tok = r.json()
    _save_oauth({"client_id": pend["client_id"], "access_token": tok.get("access_token"),
                 "refresh_token": tok.get("refresh_token"),
                 "expires_at": time.time() + float(tok.get("expires_in") or 3600) - 120})
    _cache.clear()


async def _refresh(d: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(f"{AUTH_BASE}/token", data={
            "grant_type": "refresh_token", "refresh_token": d["refresh_token"],
            "client_id": d["client_id"], "resource": RESOURCE,
        })
    if r.status_code >= 400:
        raise VidiqError(f"vidIQ: вход устарел (HTTP {r.status_code}) — владелец бота: /vidiq")
    tok = r.json()
    d.update(access_token=tok.get("access_token"),
             refresh_token=tok.get("refresh_token") or d["refresh_token"],
             expires_at=time.time() + float(tok.get("expires_in") or 3600) - 120)
    _save_oauth(d)
    return d


async def _bearer(force_refresh: bool = False) -> str:
    d = _load_oauth()
    if d.get("access_token"):
        if (force_refresh or d.get("expires_at", 0) < time.time()) and d.get("refresh_token"):
            d = await _refresh(d)
        return d["access_token"]
    key = os.environ.get("VIDIQ_API_KEY", "").strip()
    if not key:
        raise VidiqError("vidIQ не подключён — владелец бота: /vidiq")
    return key


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


async def _call_tool(name: str, arguments: dict, _retry: bool = True) -> dict:
    try:
        return await _call_tool_once(name, arguments, await _bearer())
    except VidiqError as e:
        if _retry and connected() and "HTTP 401" in str(e):
            return await _call_tool_once(name, arguments, await _bearer(force_refresh=True))
        raise


async def _call_tool_once(name: str, arguments: dict, key: str) -> dict:
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
        if r.status_code in (401, 403) and not connected():  # другой способ передать API-ключ
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
