"""Автоматическая проверка готового текста — то, что модель упускает даже с правилами.

Находит конкретные места и отдаёт их на точечное исправление:
1. Выдуманные эпизоды биографии: первое лицо + маркер конкретного времени/события
   («Несколько лет назад я была уверена…», «Однажды я…», «В 20 лет я…»).
2. Цепочки повторов: одно и то же значимое слово 3+ раз в абзаце
   («Приехали за отдыхом — уехали… Приехали разобраться…»), одинаковое начало 3+ предложений.
"""
import re

STORY_MARKERS = re.compile(
    r"(несколько\s+(лет|месяцев|недель)\s+назад|пару\s+лет\s+назад|год\s+назад|однажды|когда-то|"
    r"в\s+\d{1,2}\s+(лет|года)|в\s+(19|20)\d\d\s*(году|г\.)?|в\s+тот\s+(день|момент|вечер)|помню,?\s+как|"
    r"как-то\s+раз|на\s+днях|вчера|прошлым\s+летом|прошлой\s+зимой)",
    re.I,
)
PAST_VERB = re.compile(r"\b[А-Яа-яЁё]{2,}(ла|ли|лась|лся|лись|ло)\b", re.I)
FIRST_PERSON = re.compile(r"\b(я|мне|меня|мной|мой|моя|моё|мои|мы|нас|нам)\b", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")
WORD = re.compile(r"[А-Яа-яЁё]{6,}")
# слова, которые естественно повторяются в тексте про контент — не считаем повтором
STOP = {
    "которые", "который", "которая", "потому", "сейчас", "только", "просто", "всегда", "ретрит", "ретрите",
    "ретрита", "женщины", "женщин", "женщина", "сценарий", "сценария", "кадров", "текстом", "экране",
    "подпись", "голоса", "голосом", "музыка", "камеру", "ролики", "ролика", "роликов", "просмотров",
    "аккаунт", "аккаунта", "контент", "контента", "охваты", "охват",
}


# Противопоставления «не X, а Y» во всех формах
CONTRAST_PATTERNS = [
    re.compile(r"\bне\s+потому,?\s+что\b", re.I),                          # «Не потому что… (а/просто)…»
    re.compile(r"\bне\b[^.!?\n]{1,70}?[,—–-]\s*а\s", re.I),                   # «не X, а Y» / «не X — а Y»
    re.compile(r"(?:^|[.!?»]\s+)не\s[^.!?\n]{1,50}?\s[—–]\s(?!а\s)[а-яё]", re.I),  # «Не как проект — как партнёр»
    re.compile(r"\b(это|дело|проблема|потолок|сила)\s+не\s+(в|про|о|об)\b[^.!?\n]{0,60}[.!?]\s*(это|а|дело|он|она|потолок|сила)?\s*(в|про)\b", re.I),
]
MAX_CONTRASTS = 2


def find_contrasts(text: str) -> list[str]:
    """Предложения с противопоставлением, по порядку."""
    out: list[str] = []
    NEG = re.compile(r"(^|\s)не\s", re.I)
    NEXT = re.compile(r"^(а\s|это\s+(про|в|о)\b|просто\s)", re.I)
    for para in re.split(r"\n{2,}", _strip_placeholders(text)):
        sents = [x.strip() for x in SENT_SPLIT.split(para.replace("\n", " ")) if x.strip()]
        for i, s in enumerate(sents):
            if any(p.search(s) for p in CONTRAST_PATTERNS):
                out.append(s)
            elif i + 1 < len(sents) and NEG.search(s) and NEXT.search(sents[i + 1]):
                out.append(f"{s} {sents[i + 1]}")  # «Не X. А Y.» / «Это не про X. Это про Y.»
    return out


def _strip_placeholders(text: str) -> str:
    return re.sub(r"\[[^\]]*\]", " ", text)


def find_issues(text: str, facts_text: str = "") -> list[str]:
    issues: list[str] = []
    facts_low = (facts_text or "").lower()
    for para in re.split(r"\n\s*\n", text):
        clean = _strip_placeholders(para).strip()
        if not clean or clean.startswith(("#", "**[", "[")) and len(clean) < 40:
            continue
        sentences = [s.strip() for s in SENT_SPLIT.split(clean) if s.strip()]

        # 1. Эпизоды биографии
        for s in sentences:
            if STORY_MARKERS.search(s) and FIRST_PERSON.search(s) and PAST_VERB.search(s):
                # если эпизод опирается на банк реальных историй — не трогаем
                key = [w.lower() for w in WORD.findall(s)]
                if facts_low and sum(1 for w in key if w in facts_low) >= 3:
                    continue
                issues.append(f"ВЫДУМАННЫЙ ЭПИЗОД: «{s[:220]}»")

        # 2a. Одинаковое начало 3+ предложений в абзаце
        starts: dict[str, int] = {}
        for s in sentences:
            first = re.sub(r"^[^А-Яа-яЁёA-Za-z]+", "", s).split(" ")[0].lower().strip(",:—-")
            if len(first) >= 2:
                starts[first] = starts.get(first, 0) + 1
        for w, n in starts.items():
            if n >= 3 and w not in {"и", "а", "но", "это", "я", "ты"}:
                issues.append(f"ЦЕПОЧКА ({n} предложения начинаются с «{w}»): «{clean[:220]}»")

        # 2b. Одно значимое слово (по корню) 3+ раз в абзаце
        if len(clean) < 900:
            counts: dict[str, int] = {}
            for w in WORD.findall(clean.lower()):
                if w in STOP:
                    continue
                root = w[:6]
                counts[root] = counts.get(root, 0) + 1
            for root, n in counts.items():
                if n >= 3:
                    issues.append(f"ПОВТОР («{root}…» {n} раза в одном абзаце): «{clean[:220]}»")
                    break
    # 3. Противопоставлений больше двух на весь ответ — лишние на переписывание
    contrasts = find_contrasts(text)
    if len(contrasts) > MAX_CONTRASTS:
        for c in contrasts[MAX_CONTRASTS:]:
            issues.append(f"ЛИШНЕЕ ПРОТИВОПОСТАВЛЕНИЕ (их {len(contrasts)}, допустимо {MAX_CONTRASTS}) — перепиши прямо, без «не…, а…»: «{c[:200]}»")

    # без дублей, не больше 12 — чтобы фиксер работал точечно
    seen, out = set(), []
    for i in issues:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out[:14]
