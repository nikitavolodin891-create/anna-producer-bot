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


def _lang(text: str) -> str | None:
    t = (text or "").lower()
    ua = sum(t.count(c) for c in "іїєґ")
    ru = sum(t.count(c) for c in "ыэъё")
    if ua > ru and ua >= 2:
        return "укр"
    if ru > ua and ru >= 2:
        return "рус"
    return None


def _hist_path(data_dir: str) -> str:
    return os.path.join(data_dir, "brief_history.json")


def load_history(data_dir: str) -> list[dict]:
    try:
        with open(_hist_path(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def add_history(data_dir: str, stats: dict) -> None:
    """Одна точка в день: подписчики + медиана просмотров — для недельного отчёта."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hist = [h for h in load_history(data_dir) if h.get("date") != day]
    reels = [r for r in stats["rows"] if r["kind"] == "рилс" and r.get("plays") and r.get("dt")]
    hist.append({
        "date": day,
        "followers": stats.get("followers"),
        "median_plays": stats.get("median_plays"),
        "reels": [{"code": r["code"], "plays": r["plays"], "dt": r["dt"].isoformat(),
                   "cap": (r["caption"] or "")[:80]} for r in reels],
    })
    hist = hist[-120:]
    os.makedirs(data_dir, exist_ok=True)
    tmp = _hist_path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False)
    os.replace(tmp, _hist_path(data_dir))


def weekly_text(data_dir: str) -> str | None:
    """Недельный отчёт кодом (без модели): подписчики и просмотры этой недели против прошлой."""
    import statistics
    hist = load_history(data_dir)
    if len(hist) < 2:
        return None
    now = datetime.now(timezone.utc)
    reels: dict[str, dict] = {}
    for h in hist:
        for r in h.get("reels", []):
            reels[r["code"]] = r  # последняя известная цифра
    def week(lo, hi):
        out = []
        for r in reels.values():
            try:
                age = (now - datetime.fromisoformat(r["dt"])).days
            except ValueError:
                continue
            if lo <= age < hi:
                out.append(r)
        return out
    cur, prev = week(0, 7), week(7, 14)
    med = lambda xs: statistics.median([x["plays"] for x in xs]) if xs else None
    f_now = hist[-1].get("followers")
    f_old = next((h.get("followers") for h in hist if (now - datetime.fromisoformat(h["date"] + "T00:00:00+00:00")).days <= 8), None)
    lines = ["📊 Итоги недели"]
    if f_now is not None and f_old is not None:
        d = f_now - f_old
        lines.append(f"Подписчики: {_n(f_now)} ({'+' if d >= 0 else ''}{_n(d)} за неделю)")
    mc, mp = med(cur), med(prev)
    if mc is not None:
        trend = ""
        if mp:
            pct = (mc - mp) / mp * 100
            trend = f" — было {_n(mp)} ({'+' if pct >= 0 else ''}{pct:.0f}%)"
        lines.append(f"Рилсов за неделю: {len(cur)}, средние просмотры: {_n(mc)}{trend}")
    if cur:
        best = max(cur, key=lambda r: r["plays"])
        lines.append(f"Лучший рилс недели: {_n(best['plays'])} просмотров — «{best['cap'][:60]}»\nhttps://www.instagram.com/reel/{best['code']}/")
    if len(cur) > 10:
        lines.append(f"[!] {len(cur)} рилсов за неделю — слишком много: ролики отбирают охват друг у друга. Норма — 1 в день.")
    lines.append("\nЧтобы сделать следующую неделю сильнее — напиши «давай исправим».")
    return "\n".join(lines)


def own_hits_text(data_dir: str, days: int = 30) -> str:
    """Собственные ролики Анны за месяц: что её аудитория уже «проголосовала» просмотрами."""
    import statistics
    now = datetime.now(timezone.utc)
    reels: dict[str, dict] = {}
    for h in load_history(data_dir):
        for r in h.get("reels", []):
            reels[r["code"]] = r
    fresh = []
    for r in reels.values():
        try:
            if (now - datetime.fromisoformat(r["dt"])).days <= days:
                fresh.append(r)
        except ValueError:
            pass
    if len(fresh) < 4:
        return ""
    med = statistics.median([r["plays"] for r in fresh])
    fresh.sort(key=lambda r: r["plays"], reverse=True)
    fmt = lambda r: f"- {_n(r['plays'])} просмотров (×{r['plays'] / med:.1f} от её медианы) — «{r['cap']}» https://www.instagram.com/reel/{r['code']}/"
    lines = [f"[Собственные ролики Анны за {days} дней: {len(fresh)} шт., медиана {_n(med)}]", "Её хиты (аудитория уже проголосовала):"]
    lines += [fmt(r) for r in fresh[:3] if r["plays"] >= med * 1.5]
    lines.append("Её провалы (так не делать):")
    lines += [fmt(r) for r in fresh[-3:]]
    return "\n".join(lines)


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
    day_ago = [r for r in rows if r["dt"] and (now_local - r["dt"]).total_seconds() < 86400]
    if len(day_ago) > 2:
        lines.append(f"[!] СЛИШКОМ ЧАСТО: {len(day_ago)} публикаций за последние сутки (норма — 1, максимум 2).")
    langs: dict[str, list] = {}
    for r in rows:
        lg = _lang(r["caption"])
        if lg and r.get("plays"):
            langs.setdefault(lg, []).append(r["plays"])
    if len(langs) == 2:
        import statistics
        parts = [f"{k} — медиана {_n(statistics.median(v))} ({len(v)} шт.)" for k, v in langs.items()]
        lines.append("Язык подписи и просмотры: " + "; ".join(parts))
    if dup_codes:
        lines.append(f"[!] Найдено повторов (одна и та же подпись выложена ещё раз): {len(dup_codes)}")
    lines.append("\nПоследние публикации (новые сверху):")
    for r in rows[:8]:
        o = old_posts.get(r["code"])
        tag = " 🆕 НОВАЯ с прошлой проверки" if prev and not o else ""
        if r["code"] in dup_codes:
            tag += " [!] ДУБЛЬ (такая же подпись уже была)"
        if r.get("duration") and r["duration"] > 75:
            tag += f" [!] ДЛИННЫЙ ({r['duration']:.0f} с)"
        lg = _lang(r["caption"])
        if lg:
            tag += f" · язык: {lg}"
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
