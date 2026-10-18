"""Аудит Instagram-аккаунта: выгрузка публикаций через Apify + статистика для Claude."""
import os
import re
import json
import asyncio
import logging
import statistics
from datetime import datetime, timezone, timedelta

import httpx

import social

logger = logging.getLogger(__name__)

AUDIT_POSTS = int(os.environ.get("AUDIT_POSTS", "30"))
CLIENT_INSTAGRAM = os.environ.get("CLIENT_INSTAGRAM", "anyalakshmi").lstrip("@")
LOCAL_TZ = timezone(timedelta(hours=int(os.environ.get("CLIENT_UTC_OFFSET", "8"))))  # Бали
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

PROFILE_RE = re.compile(r"https?://(?:www\.|m\.)?instagram\.com/([A-Za-z0-9_.]{1,30})/?(?:\?[^\s]*)?$", re.I)
NOT_PROFILES = {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct"}
HANDLE_RE = re.compile(r"@?([A-Za-z0-9_.]{1,30})")


class AuditError(Exception):
    """Понятная пользователю ошибка."""


def detect_profile(url: str) -> str | None:
    """Ссылка на профиль Instagram → ник (или None)."""
    m = PROFILE_RE.match(url.strip())
    if not m:
        return None
    handle = m.group(1)
    return None if handle.lower() in NOT_PROFILES else handle


def normalize_handle(text: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    if "instagram.com" in text:
        return detect_profile(text)
    m = HANDLE_RE.fullmatch(text)
    return m.group(1) if m else None


async def _apify(payload: dict, timeout: int = 300) -> list[dict]:
    if not social.APIFY_TOKEN:
        raise AuditError("для анализа аккаунта нужен Apify — добавь APIFY_TOKEN в настройки бота (Railway → Variables)")
    endpoint = f"https://api.apify.com/v2/acts/{social.APIFY_IG_ACTOR}/run-sync-get-dataset-items"
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.post(endpoint, params={"token": social.APIFY_TOKEN}, json=payload)
    if resp.status_code == 401:
        raise AuditError("Apify не принял токен — проверь APIFY_TOKEN")
    if resp.status_code == 402:
        raise AuditError("на Apify закончились кредиты")
    if resp.status_code == 408:
        raise AuditError("Apify не успел выгрузить аккаунт — попробуй ещё раз")
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


async def fetch_account(handle: str) -> dict:
    """Профиль + последние публикации."""
    url = f"https://www.instagram.com/{handle}/"
    details_task = _apify({"directUrls": [url], "resultsType": "details", "resultsLimit": 1, "addParentData": False})
    posts_task = _apify({"directUrls": [url], "resultsType": "posts", "resultsLimit": AUDIT_POSTS, "addParentData": False})
    details, posts = await asyncio.gather(details_task, posts_task, return_exceptions=True)
    if isinstance(posts, Exception):
        raise posts if isinstance(posts, AuditError) else AuditError(f"не получилось выгрузить публикации: {str(posts)[:150]}")
    profile = details[0] if isinstance(details, list) and details else {}
    if profile.get("error"):
        profile = {}
    posts = [p for p in posts if not p.get("error") and p.get("shortCode")]
    if not posts:
        if profile.get("private") or profile.get("isPrivate"):
            raise AuditError("это закрытый аккаунт — его не выгрузить")
        raise AuditError(f"не нашла публикаций у @{handle} (ник верный? аккаунт открытый?)")
    return {"profile": profile, "posts": posts}


def _post_row(p: dict) -> dict:
    ts = p.get("timestamp")
    dt = None
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
        except ValueError:
            dt = None
    kind = {"Video": "рилс", "Sidecar": "карусель", "Image": "фото"}.get(p.get("type"), p.get("type") or "?")
    plays = p.get("videoPlayCount") or p.get("videoViewCount")
    likes = max(p.get("likesCount") or 0, 0)  # -1, если лайки скрыты
    comments = p.get("commentsCount") or 0
    return {
        "code": p.get("shortCode"),
        "url": p.get("url") or f"https://www.instagram.com/p/{p.get('shortCode')}/",
        "kind": kind,
        "plays": plays,
        "likes": likes,
        "comments": comments,
        "shares": p.get("sharesCount"),
        "duration": p.get("videoDuration"),
        "dt": dt,
        "caption": (p.get("caption") or "").strip(),
        "cover": p.get("displayUrl"),
        "pinned": bool(p.get("isPinned")),
    }


def build_stats(data: dict) -> dict:
    """Считает цифры в коде (а не «на глаз» моделью): медианы, топы, форматы, время."""
    profile = data["profile"]
    rows = [_post_row(p) for p in data["posts"]]
    followers = profile.get("followersCount")
    reels = [r for r in rows if r["kind"] == "рилс" and r["plays"]]

    def er(r):  # вовлечённость относительно просмотров (для рилсов) или подписчиков
        base = r["plays"] or followers or 0
        return (r["likes"] + r["comments"] * 3) / base if base else 0

    for r in rows:
        r["er"] = er(r)

    median_plays = statistics.median([r["plays"] for r in reels]) if reels else None
    for r in reels:
        r["x_median"] = r["plays"] / median_plays if median_plays else None

    by_kind: dict[str, dict] = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], {"count": 0, "likes": [], "comments": [], "plays": []})
        k["count"] += 1
        k["likes"].append(r["likes"])
        k["comments"].append(r["comments"])
        if r["plays"]:
            k["plays"].append(r["plays"])
    kinds = {
        name: {
            "count": v["count"],
            "median_likes": statistics.median(v["likes"]) if v["likes"] else 0,
            "median_comments": statistics.median(v["comments"]) if v["comments"] else 0,
            "median_plays": statistics.median(v["plays"]) if v["plays"] else None,
        }
        for name, v in by_kind.items()
    }

    dated = sorted([r for r in rows if r["dt"]], key=lambda r: r["dt"])
    per_week = None
    if len(dated) >= 2:
        span_days = max((dated[-1]["dt"] - dated[0]["dt"]).days, 1)
        per_week = round(len(dated) / span_days * 7, 1)

    weekday: dict[str, list[float]] = {}
    for r in reels:
        if r["dt"]:
            weekday.setdefault(WEEKDAYS[r["dt"].weekday()], []).append(r["plays"])
    weekday_median = {d: int(statistics.median(v)) for d, v in weekday.items()}

    ranked = sorted(reels, key=lambda r: r["plays"], reverse=True)
    top = ranked[:5]
    bottom = [r for r in ranked[-5:] if r not in top][::-1] if len(ranked) > 5 else []

    def dur_median(items):
        d = [r["duration"] for r in items if r.get("duration")]
        return round(statistics.median(d)) if d else None

    return {
        "handle": profile.get("username"),
        "full_name": profile.get("fullName"),
        "bio": profile.get("biography"),
        "followers": followers,
        "posts_total": profile.get("postsCount"),
        "rows": rows,
        "reels_count": len(reels),
        "median_plays": median_plays,
        "kinds": kinds,
        "per_week": per_week,
        "weekday_median": weekday_median,
        "top": top,
        "bottom": bottom,
        "top_duration": dur_median(top),
        "bottom_duration": dur_median(bottom),
        "period": (dated[0]["dt"].date().isoformat(), dated[-1]["dt"].date().isoformat()) if dated else None,
    }


def _n(x) -> str:
    return social._fmt_num(x)


def stats_text(s: dict) -> str:
    """Компактная сводка для Claude."""
    lines = [
        f"[Аудит Instagram @{s['handle']}" + (f" — {s['full_name']}" if s.get("full_name") else "") + "]",
        f"Подписчиков: {_n(s['followers'])} · Всего публикаций: {_n(s['posts_total'])}",
    ]
    if s.get("bio"):
        lines.append(f"Шапка профиля: {s['bio']}")
    if s.get("period"):
        lines.append(f"Проанализировано {len(s['rows'])} последних публикаций за период {s['period'][0]} — {s['period'][1]}")
    if s.get("per_week"):
        lines.append(f"Частота: ~{s['per_week']} публикаций в неделю")
    lines.append("Форматы (медианы):")
    for name, k in s["kinds"].items():
        lines.append(
            f"- {name}: {k['count']} шт · просмотры {_n(k['median_plays'])} · лайки {_n(k['median_likes'])} · комментарии {_n(k['median_comments'])}"
        )
    if s["median_plays"]:
        lines.append(f"Медиана просмотров рилсов: {_n(s['median_plays'])}")
    if s["weekday_median"]:
        lines.append("Медиана просмотров рилсов по дням недели: " + ", ".join(f"{d} {_n(v)}" for d, v in s["weekday_median"].items()))
    if s.get("top_duration") or s.get("bottom_duration"):
        lines.append(f"Длительность: топ-рилсы ~{s.get('top_duration') or '—'} сек, слабые ~{s.get('bottom_duration') or '—'} сек")

    def post_line(i, r):
        x = f" ({r['x_median']:.1f}× медианы)" if r.get("x_median") else ""
        when = f'{r["dt"].strftime("%d.%m")} {WEEKDAYS[r["dt"].weekday()]} {r["dt"].strftime("%H:%M")}' if r["dt"] else ""
        dur = f" · {r['duration']:.0f}с" if r.get("duration") else ""
        cap = r["caption"].replace("\n", " ")[:220]
        return (
            f"{i}. {r['kind']}{dur} · {when} · просмотры {_n(r['plays'])}{x} · лайки {_n(r['likes'])} · "
            f"комм. {_n(r['comments'])}" + (f" · репосты {_n(r['shares'])}" if r.get('shares') is not None else "") + f"{' · закреп' if r['pinned'] else ''}\n   {r['url']}\n   Подпись: {cap or '(нет)'}"
        )

    if s["top"]:
        lines.append("\nТОП рилсов по просмотрам:")
        lines += [post_line(i + 1, r) for i, r in enumerate(s["top"])]
    if s["bottom"]:
        lines.append("\nСАМЫЕ СЛАБЫЕ рилсы:")
        lines += [post_line(i + 1, r) for i, r in enumerate(s["bottom"])]
    others = [r for r in s["rows"] if r not in s["top"] and r not in s["bottom"]]
    if others:
        lines.append("\nОстальные публикации (кратко):")
        for r in others[:25]:
            cap = r["caption"].replace("\n", " ")[:90]
            lines.append(
                f"- {r['kind']} · {r['dt'].strftime('%d.%m') if r['dt'] else ''} · просм. {_n(r['plays'])} · "
                f"лайки {_n(r['likes'])} · комм. {_n(r['comments'])} · «{cap}»"
            )
    return "\n".join(lines)


async def covers(s: dict) -> list[tuple[str, bytes, str]]:
    """Обложки топ-3 и худших-2 рилсов — чтобы Claude увидел визуальный хук."""
    picks = [("ТОП", r) for r in s["top"][:3]] + [("СЛАБЫЙ", r) for r in s["bottom"][:2]]
    out: list[tuple[str, bytes, str]] = []
    for label, r in picks:
        if not r.get("cover"):
            continue
        try:
            data = await social.download_bytes(r["cover"], max_bytes=5 * 1024 * 1024)
            mime = social._sniff_mime(data)
            if mime:
                out.append((f"{label}: {_n(r['plays'])} просмотров — {r['url']}", data, mime))
        except Exception:
            logger.warning("Не удалось скачать обложку %s", r.get("url"))
    return out


# ---------------------------------------------------------------------------
# Память о последнем аудите — подмешивается в промпт, чтобы бот знал аккаунт
# ---------------------------------------------------------------------------
def _audit_path(data_dir: str) -> str:
    return os.path.join(data_dir, "audit.json")


def save_audit(data_dir: str, handle: str, summary: str) -> None:
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = _audit_path(data_dir)
        try:
            with open(path, encoding="utf-8") as f:
                store = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            store = {}
        store[handle.lower()] = {"date": datetime.now(LOCAL_TZ).strftime("%d.%m.%Y"), "summary": summary[:6000]}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        logger.exception("Не удалось сохранить аудит")


def load_client_audit(data_dir: str) -> dict | None:
    return load_memory(data_dir, CLIENT_INSTAGRAM)


def load_memory(data_dir: str, key: str) -> dict | None:
    try:
        with open(_audit_path(data_dir), encoding="utf-8") as f:
            return json.load(f).get(key.lower())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception:
        logger.exception("Не удалось прочитать аудит")
        return None
