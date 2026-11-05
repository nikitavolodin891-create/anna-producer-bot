"""Утренняя сводка по Instagram Анны: что изменилось с прошлой проверки.

Каждая сводка сохраняет «снимок» аккаунта (подписчики + цифры последних публикаций) на Volume.
Следующая сводка сравнивает свежие цифры со снимком — так видно прирост подписчиков,
новые публикации и как набирают просмотры последние ролики.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import social

BRIEF_POSTS = int(os.environ.get("BRIEF_POSTS", "12"))


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, "brief_snapshot.json")


def load_snapshot(data_dir: str) -> dict | None:
    try:
        with open(_path(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def make_snapshot(stats: dict) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "followers": stats.get("followers"),
        "posts": {
            r["code"]: {"plays": r["plays"], "likes": r["likes"], "comments": r["comments"], "kind": r["kind"]}
            for r in stats["rows"] if r.get("code")
        },
    }


def save_snapshot(data_dir: str, snap: dict) -> None:
    os.makedirs(data_dir, exist_ok=True)
    tmp = _path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    os.replace(tmp, _path(data_dir))


def _n(x) -> str:
    return social._fmt_num(x) if x is not None else "—"


def _delta(new, old) -> str:
    if new is None or old is None:
        return ""
    d = new - old
    return f" ({'+' if d >= 0 else ''}{_n(d)})" if d else " (без изменений)"


def brief_text(stats: dict, prev: dict | None, now_local: datetime) -> str:
    """Факты для модели — все цифры посчитаны кодом."""
    lines = [f"[Данные для утренней сводки · сейчас {now_local:%d.%m %H:%M} по времени Анны]"]
    old_posts = (prev or {}).get("posts", {})
    if prev:
        try:
            hours = (datetime.now(timezone.utc) - datetime.fromisoformat(prev["ts"])).total_seconds() / 3600
            lines.append(f"Прошлая проверка: {hours:.0f} ч назад.")
        except (KeyError, ValueError):
            pass
        lines.append(f"Подписчики: {_n(stats.get('followers'))}{_delta(stats.get('followers'), prev.get('followers'))}")
    else:
        lines.append(f"Подписчики: {_n(stats.get('followers'))} — это первая сводка, сравнивать пока не с чем.")
    if stats.get("median_plays"):
        lines.append(f"Медиана просмотров рилсов (последние {stats.get('reels_count')}): {_n(stats['median_plays'])}")
    if stats.get("per_week"):
        lines.append(f"Частота публикаций: ~{stats['per_week']} в неделю")

    rows = [r for r in stats["rows"] if not r.get("pinned")]
    rows.sort(key=lambda r: r["dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    # дубли: одинаковое начало подписи у разных публикаций
    seen: dict[str, str] = {}
    dup_codes = set()
    for r in sorted(rows, key=lambda r: r["dt"] or datetime.min.replace(tzinfo=timezone.utc)):
        key = " ".join((r["caption"] or "").lower().split())[:80]
        if len(key) >= 25:
            if key in seen:
                dup_codes.add(r["code"])
            else:
                seen[key] = r["code"]
    if dup_codes:
        lines.append(f"[!] Найдено повторов (одна и та же подпись выложена ещё раз): {len(dup_codes)}")
    lines.append("\nПоследние публикации (новые сверху):")
    for r in rows[:8]:
        o = old_posts.get(r["code"])
        tag = " 🆕 НОВАЯ с прошлой проверки" if prev and not o else ""
        if r["code"] in dup_codes:
            tag += " [!] ДУБЛЬ (такая же подпись уже была)"
        when = r["dt"].strftime("%d.%m %H:%M") if r["dt"] else "?"
        cap = (r["caption"] or "").replace("\n", " ")[:90]
        plays = f"просмотры {_n(r['plays'])}{_delta(r['plays'], (o or {}).get('plays')) if o else ''}, " if r["plays"] else ""
        xm = f" ×{r['x_median']:.1f} медианы" if r.get("x_median") else ""
        lines.append(
            f"- {when} · {r['kind']} · {plays}лайки {_n(r['likes'])}{_delta(r['likes'], (o or {}).get('likes')) if o else ''}, "
            f"комменты {_n(r['comments'])}{_delta(r['comments'], (o or {}).get('comments')) if o else ''}{xm}{tag}\n  «{cap}»"
        )
    if rows and rows[0]["dt"]:
        days = (now_local - rows[0]["dt"]).total_seconds() / 86400
        lines.append(f"\nС последней публикации прошло: {days:.1f} дн.")
    return "\n".join(lines)
