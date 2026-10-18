"""Тренды ниши: что залетает прямо сейчас.

Два источника (оба через Apify):
1. Хэштеги ниши — самые просматриваемые свежие рилсы.
2. Аккаунты-ориентиры (конкуренты/референсы, Анна добавляет сама) — «залёты»: ролики,
   набравшие в 2+ раза больше обычного для этого автора. Это самый надёжный сигнал тренда.
"""
import os
import re
import json
import time
import asyncio
import logging
import statistics
from datetime import datetime, timezone, timedelta

import httpx

import social
import audit

logger = logging.getLogger(__name__)

APIFY_HASHTAG_ACTOR = os.environ.get("APIFY_HASHTAG_ACTOR", "apify~instagram-hashtag-scraper")
DEFAULT_HASHTAGS = [
    h.strip().lstrip("#")
    for h in os.environ.get(
        "NICHE_HASHTAGS",
        "деньги,психологияденег,денежноемышление,женскаяэнергия,предпринимательница,женскийбизнес",
    ).split(",")
    if h.strip()
]
POSTS_PER_HASHTAG = int(os.environ.get("TRENDS_PER_HASHTAG", "25"))
POSTS_PER_ACCOUNT = int(os.environ.get("TRENDS_PER_ACCOUNT", "15"))
FRESH_DAYS = int(os.environ.get("TRENDS_FRESH_DAYS", "14"))
CACHE_HOURS = float(os.environ.get("TRENDS_CACHE_HOURS", "6"))
OUTLIER_X = 2.0
MIN_TAG_PLAYS = int(os.environ.get("TRENDS_MIN_PLAYS", "5000"))  # слабее — не тренд
MAX_ACCOUNTS = 10


class TrendsError(Exception):
    """Понятная пользователю ошибка."""


# ---------------------------------------------------------------------------
# Список аккаунтов-ориентиров (хранится на диске)
# ---------------------------------------------------------------------------
def _path(data_dir: str, name: str) -> str:
    return os.path.join(data_dir, name)


def load_competitors(data_dir: str) -> list[str]:
    try:
        with open(_path(data_dir, "competitors.json"), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        env = os.environ.get("NICHE_ACCOUNTS", "")
        return [a.strip().lstrip("@") for a in env.split(",") if a.strip()]


def save_competitors(data_dir: str, handles: list[str]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    with open(_path(data_dir, "competitors.json"), "w", encoding="utf-8") as f:
        json.dump(handles[:MAX_ACCOUNTS], f, ensure_ascii=False)


def parse_handles(text: str) -> list[str]:
    out = []
    for token in re.split(r"[\s,]+", text or ""):
        h = audit.normalize_handle(token)
        if h and h.lower() not in {x.lower() for x in out}:
            out.append(h)
    return out


# ---------------------------------------------------------------------------
# Сбор данных
# ---------------------------------------------------------------------------
async def _run_actor(actor: str, payload: dict, timeout: int = 300) -> list[dict]:
    if not social.APIFY_TOKEN:
        raise TrendsError("для трендов нужен Apify — добавь APIFY_TOKEN в настройки бота (Railway → Variables)")
    endpoint = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.post(endpoint, params={"token": social.APIFY_TOKEN}, json=payload)
    if resp.status_code == 401:
        raise TrendsError("Apify не принял токен — проверь APIFY_TOKEN")
    if resp.status_code == 402:
        raise TrendsError("на Apify закончились кредиты")
    if resp.status_code == 408:
        raise TrendsError("Apify не успел собрать данные — попробуй ещё раз")
    resp.raise_for_status()
    data = resp.json()
    return [x for x in data if isinstance(x, dict) and not x.get("error")] if isinstance(data, list) else []


def _ts(p: dict) -> datetime | None:
    try:
        return datetime.fromisoformat((p.get("timestamp") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _row(p: dict, source: str) -> dict:
    plays = p.get("videoPlayCount") or p.get("videoViewCount") or 0
    return {
        "source": source,
        "url": p.get("url") or f"https://www.instagram.com/reel/{p.get('shortCode')}/",
        "author": p.get("ownerUsername"),
        "plays": plays,
        "likes": max(p.get("likesCount") or 0, 0),
        "comments": p.get("commentsCount") or 0,
        "shares": p.get("sharesCount"),
        "duration": p.get("videoDuration"),
        "posted": _ts(p),
        "caption": (p.get("caption") or "").strip(),
        "cover": p.get("displayUrl"),
        "x_author": None,
    }


async def fetch_hashtags(hashtags: list[str]) -> list[dict]:
    items = await _run_actor(APIFY_HASHTAG_ACTOR, {
        "hashtags": hashtags,
        "resultsType": "reels",
        "resultsLimit": POSTS_PER_HASHTAG,
    })
    return [_row(p, "хэштег #" + ((p.get("inputUrl") or "").rstrip("/").split("/")[-1] or "ниши")) for p in items]


async def fetch_accounts(handles: list[str]) -> list[dict]:
    """Последние ролики аккаунтов-ориентиров + во сколько раз каждый выше медианы автора."""
    items = await _run_actor(social.APIFY_IG_ACTOR, {
        "directUrls": [f"https://www.instagram.com/{h}/" for h in handles],
        "resultsType": "posts",
        "resultsLimit": POSTS_PER_ACCOUNT,
        "addParentData": False,
    })
    by_author: dict[str, list[dict]] = {}
    for p in items:
        if p.get("type") != "Video":
            continue
        r = _row(p, "ориентир")
        if r["plays"]:
            by_author.setdefault((r["author"] or "?").lower(), []).append(r)
    rows = []
    for author_rows in by_author.values():
        med = statistics.median([r["plays"] for r in author_rows]) if author_rows else 0
        for r in author_rows:
            r["x_author"] = r["plays"] / med if med else None
            rows.append(r)
    return rows


def select_trends(rows: list[dict], limit: int = 12) -> list[dict]:
    """Свежие (за FRESH_DAYS) ролики: сначала залёты ориентиров, потом топ хэштегов."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)
    fresh = [r for r in rows if r["plays"] and (r["posted"] is None or r["posted"] >= cutoff)]
    seen, uniq = set(), []
    for r in fresh:
        if r["url"] not in seen:
            seen.add(r["url"])
            uniq.append(r)
    outliers = sorted(
        [r for r in uniq if r["source"] == "ориентир" and (r["x_author"] or 0) >= OUTLIER_X],
        key=lambda r: r["x_author"], reverse=True,
    )
    tags = sorted(
        [r for r in uniq if r["source"] != "ориентир" and r["plays"] >= MIN_TAG_PLAYS],
        key=lambda r: r["plays"], reverse=True,
    )
    picked = outliers[: limit // 2]
    picked += tags[: limit - len(picked)]
    if len(picked) < limit:
        picked += [r for r in outliers[limit // 2:] if r not in picked][: limit - len(picked)]
    return picked


async def collect(data_dir: str, hashtags: list[str] | None = None, force: bool = False) -> dict:
    """Собирает тренды (с кешем на CACHE_HOURS, чтобы не тратить кредиты Apify на повторы)."""
    hashtags = hashtags or DEFAULT_HASHTAGS
    competitors = load_competitors(data_dir)
    key = json.dumps({"h": sorted(hashtags), "c": sorted(competitors)}, ensure_ascii=False)
    cache_file = _path(data_dir, "trends_cache.json")
    if not force:
        try:
            with open(cache_file, encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("key") == key and time.time() - cache.get("at", 0) < CACHE_HOURS * 3600:
                rows = cache["rows"]
                for r in rows:
                    r["posted"] = datetime.fromisoformat(r["posted"]) if r.get("posted") else None
                return {"rows": rows, "hashtags": hashtags, "competitors": competitors, "cached": True}
        except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
            pass

    tasks = [fetch_hashtags(hashtags)]
    if competitors:
        tasks.append(fetch_accounts(competitors))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    rows: list[dict] = []
    errors: list[str] = []
    for res in results:
        if isinstance(res, TrendsError):
            raise res
        if isinstance(res, Exception):
            logger.exception("Trends source failed", exc_info=res)
            errors.append(str(res)[:150])
        else:
            rows += res
    picked = select_trends(rows)
    if not picked:
        raise TrendsError("не нашла свежих залетевших рилсов" + (f" ({'; '.join(errors)})" if errors else ""))

    try:
        os.makedirs(data_dir, exist_ok=True)
        dump = [{**r, "posted": r["posted"].isoformat() if r["posted"] else None} for r in picked]
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"key": key, "at": time.time(), "rows": dump}, f, ensure_ascii=False)
    except Exception:
        logger.exception("Не удалось сохранить кеш трендов")
    return {"rows": picked, "hashtags": hashtags, "competitors": competitors, "cached": False}


def trends_text(result: dict) -> str:
    n = social._fmt_num
    lines = [
        f"[Тренды ниши за последние {FRESH_DAYS} дней]",
        "Хэштеги: " + ", ".join("#" + h for h in result["hashtags"]),
        "Аккаунты-ориентиры: " + (", ".join("@" + c for c in result["competitors"]) or "не заданы"),
        "",
    ]
    for i, r in enumerate(result["rows"], 1):
        x = f" · {r['x_author']:.1f}× от обычного у автора" if r.get("x_author") else ""
        shares = f" · репосты {n(r['shares'])}" if r.get("shares") is not None else ""
        dur = f" · {r['duration']:.0f}с" if r.get("duration") else ""
        when = r["posted"].strftime("%d.%m") if r.get("posted") else ""
        cap = r["caption"].replace("\n", " ")[:260]
        lines.append(
            f"{i}. @{r['author']} ({r['source']}) · {when}{dur} · просмотры {n(r['plays'])}{x} · "
            f"лайки {n(r['likes'])} · комм. {n(r['comments'])}{shares}\n   {r['url']}\n   Подпись: {cap or '(нет)'}"
        )
    return "\n".join(lines)


async def covers(result: dict, limit: int = 6) -> list[tuple[str, bytes, str]]:
    out = []
    for i, r in enumerate(result["rows"][:limit], 1):
        if not r.get("cover"):
            continue
        try:
            data = await social.download_bytes(r["cover"], max_bytes=5 * 1024 * 1024)
            mime = social._sniff_mime(data)
            if mime:
                out.append((f"#{i} @{r['author']} — {social._fmt_num(r['plays'])} просмотров", data, mime))
        except Exception:
            logger.warning("Не удалось скачать обложку тренда %s", r.get("url"))
    return out
