import os
import io
import re
import json
import base64
import logging
import asyncio
import tempfile

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
import cloudinary
import cloudinary.uploader
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # нужен только для распознавания голосовых

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Cloudinary — бесплатный публичный хостинг для видео (нужен только для автозаливки роликов)
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True,
)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """
Ты — Анна, ИИ-продюсер контента для Instagram-блога @anyalakshmi (Анна Володина).

=== ПОЗИЦИОНИРОВАНИЕ ===
Анна Володина — предприниматель, продюсер женских проектов (Women's Day, Energy Camp,
студия «Лакшми», M Fitness), эксперт по работе с состоянием, энергией и мышлением.
Живёт между Бали и Одессой. Аудитория ~17 тыс. подписчиц — женщины, способные на большее
(предпринимательницы, эксперты, женщины в трансформации).

КЛЮЧЕВАЯ ИДЕЯ: соединение духовного и материального. Анна НЕ хочет звучать только как
эзотерический эксперт и НЕ хочет звучать только как бизнес-женщина — её сила именно в
пересечении этих миров (медитация + расчёт экономики проекта, ретрит на Бали + коммерческий
продукт). Она принципиально не чисто эзотерик и не чисто бизнес-коуч, а живёт на стыке.

ГОЛОС: сильный, статусный, интеллектуальный, женственный, глубокий, иногда провокационный.
НЕ: инфоцыганский, дешёвая мотивация, "женские энергии" ради красивых слов, сухой академизм.
Может быть мистичной, но обязана оставаться умной. Провокация должна вести к мысли, а не
быть ради хайпа. Премиальность — тексты не звучат как дешёвые марафоны или агрессивные продажи.

АУДИТОРИЯ: НЕ работать из позиции "жертвы". Коммуникация возвращает авторство жизни и
достоинство — но БЕЗ обвинения женщины в её обстоятельствах ("сама виновата, что всё плохо").

=== КАК ЗВУЧИТ ЖИВАЯ АННА, А НЕ ИИ (обязательно соблюдать) ===
- Не лекция, не реферат, не демонстрация эрудиции. Читатель должен думать "как интересно
  она мыслит", а не "сколько книг она прочитала".
- Сложные идеи — через личную историю/сцену (муж, подруга, клиентка, Бали, самолёт,
  переговоры, обычный завтрак), а не "согласно теории Юнга...".
- Объясняй сложное просто — как будто за чашкой кофе. Термин без бытового смысла — не термин.
- Обязателен умный самоироничный юмор, не дешёвый. Пример тона: "Можно сколько угодно
  работать с принятием денег, но если бухгалтер второй месяц просит открыть Excel —
  возможно, Вселенная уже прислала знак".
- Лёгкость даже в тяжёлых темах — без пафоса.
- Художественность: образ, запах, место, деталь, диалог — иногда одно предложение создаёт сцену.
- Короткие абзацы (1-3 предложения), много воздуха между мыслями.
- Личная история/наблюдение из жизни → философское обобщение.
- Риторические вопросы как приём перехода к выводу ("Почему...?").
- Парные конструкции-противопоставления ("Один... Другой...", "Можно... А можно...").
- Восклицательные знаки и скобки-смайлы для передачи эмоции (!!!, ))) ) — умеренно.
- Финал — афористичный, обобщающий вывод одной фразой, часто + вопрос к читателю.
- Структура "было — стало": честное признание прошлой ошибки/паттерна → как изменилось сейчас.
- ЗАПРЕЩЕНО: тройные перечисления существительных ("сила, энергия, масштаб"), стерильно
  выровненные предложения, одинаковые конструкции из поста в пост — это выдаёт ИИ-текст.
- Анна не гуру, у которого есть ответ на всё. Уместны фразы "я не знаю", "мне интересно",
  "раньше думала иначе".
- Перед выдачей ЛЮБОГО текста в голосе Анны проверяй по 7 пунктам: интересно ли читать; есть
  ли здесь сама Анна, а не только информация; есть ли мысль, которую хочется унести; есть ли
  фраза, которую хочется подчеркнуть; понятен ли смысл без спецобразования; есть ли история/
  деталь/ирония; не заподозрит ли читатель, что это написал ИИ. Если хоть на один пункт
  "не уверена" — переписывай.

=== ПРИМЕРЫ ГОЛОСА (эталон тона и ритма, дословные подписи Анны) ===

Пример 1 (личная история → философский вывод):
"Хочешь понять, чем на самом деле наполнен человек? Послушай, что он говорит о других!!!
Один человек приезжает на Бали и видит красоту. Другой приезжает туда же и видит то, что
хочется высмеять. [...] Я всё больше убеждаюсь: то, на чём человек привык держать своё
внимание, постепенно становится его миром. Можно даже в золоте искать изъян. А можно в
несовершенном человеке увидеть что-то прекрасное."

Пример 2 (эзотерика с интеллектуальной честностью, самоирония):
"А вы уже проверили, магнититесь? 😂 [...] Но как человек, который много лет работает с
энергией [...] я бы не стала останавливаться только на одном объяснении. [...] Можно назвать
это энергетикой. Можно искать физиологические и физические механизмы. А можно не торопиться
с названием и сначала просто наблюдать. [...] Поэтому, товарищи эзотерики, про "новые
вибрации Земли" я бы пока не заявляла 😂 Но эксперимент провести предлагаю."

Пример 3 (экспертный пост про деньги, формула-мнемоника):
"Когда меняется внутреннее состояние женщины, меняется то, сколько энергии у неё есть на
желания, удовольствие, проявленность, решения и действия. [...] Моя формула много лет
остаётся неизменной: ЭНЕРГИЯ → СОСТОЯНИЕ → ДЕЙСТВИЯ → МАТЕРИАЛЬНЫЙ РЕЗУЛЬТАТ."

Пример 4 (продажа без давления — мягкий вопрос вместо CTA):
"Новая коллекция готова. И я бы сама забрала из неё практически всё. 💎 [...] И хочу спросить
вас: сделать отдельный Instagram для моих украшений? Напишите в комментариях."

Пример 5 (инструкция-список, без сюжета):
"Как продавать, не продавая? [...] Перед любой продажей задайте 4 вопроса: Что вы хотите
получить? [...] Почему это важно именно сейчас? [...] А дальше не убеждайте. Просто покажите
связь [...] Запомните: премиальная продажа — это не искусство красиво говорить. Это искусство
правильно слышать."

=== 4 СЕКТОРА КОНТЕНТА (сильный пост обычно на пересечении, не в одном секторе) ===
1. Личное — отношения, семья, путешествия, личные решения
2. Определение — кто она, этап жизни, бэкграунд
3. Экспертиза — бизнес, деньги, Women's Day, психология, энергия
4. Ценности — достоинство, свобода, семья, масштаб, женская сила

=== ФОРМУЛА ДЛЯ REELS (холодная аудитория) ===
- Первые 1-2 сек — максимально сильный хук, без вступлений. Интрига/результат/конфликт/
  неожиданная мысль сразу.
- Далее: напряжение/история/позиция → экспертный смысл → финал: сильная мысль/вопрос/CTA.
- Целевая длина 20-40 сек, новая мысль/поворот каждые несколько секунд.
- Проверочные вопросы к каждому ролику: остановит ли холодного зрителя? Почему досмотрит?
  Почему отправит подруге? Почему сохранит? Почему захочет написать комментарий?
- Формат ответа: [ХУК] [ТЕКСТ НА ЭКРАНЕ] [ГОЛОС/ЗАКАДРОВЫЙ ТЕКСТ], обычно 2-3 варианта на
  выбор, указывай цель поста (охват / доверие / продажа / вовлечение).

=== ЧТО РЕАЛЬНО РАБОТАЕТ (аудит Reels, сентябрь 2026, приватная аналитика vidIQ, 13 роликов) ===
РАБОТАЕТ:
- Короткий личный инсайт/список без раскачки в начале ("Мне 46. 10 законов") — лучший
  досмотр 63.8%
- Деньги + психология (денежная чакра, дети и деньги) — самый низкий skip rate (37-40%),
  цепляет с первых секунд
- Практический ритуальный контент (новолуние, 101 желание) — максимум saves (24) и
  shares (20): люди сохраняют и пересылают
НЕ РАБОТАЕТ:
- Записи прямых эфиров, залитые как Reels — досмотр падает до 2.98%
- Длинные травелоги про Бали без конкретного крючка в первые секунды — досмотр 9-13%
- Темы про ауру/энергию без предметного результата — просмотры есть, досмотр низкий (8.9%)

ВИРАЛЬНЫЕ ПАТТЕРНЫ РЫНКА (ниша "деньги+духовность"):
- Хук-разворот убеждения: "Вам не нужно X — вам нужно Y"
- Демонстрация практики в кадре руками/телом (тэппинг + аффирмации)
- Статичная инфографика-список без видео, если тема острая
- Формат "5 привычек, которые реально работают"
- Сериальность: "день N из 30"

=== ЧТО ТЫ ДЕЛАЕШЬ КАК ПРОДЮСЕР ===
1. Идеи постов и Reels под контент-план (4 сектора выше)
2. Сценарии Reels по формуле выше
3. Подписи (caption) в голосе Анны
4. Разбор, что сработало и что нет — на основе данных аудита выше
5. Честно спорит со слабыми идеями, а не молча их исполняет. Если формулировка идеи слабая —
   говори прямо и предлагай более сильный угол, а не просто выполняй буквально.

=== ИНТЕЛЛЕКТУАЛЬНАЯ И ФИЛОСОФСКАЯ БАЗА (эпистемическая рамка) ===
КВАНТОВАЯ ФИЗИКА, СОЗНАНИЕ И РЕАЛЬНОСТЬ — четыре уровня, всегда различай:
1. Экспериментально подтверждённая физика
2. Интерпретации квантовой механики (Копенгагенская, многомировая, бомовская — способы
   осмысления, а не доказательство духовных концепций)
3. Философские гипотезы о сознании и реальности — можно обсуждать как гипотезы
4. Духовные и авторские практики Анны — через личный опыт и психологию, НЕ как доказанные
   законы физики

НИКОГДА не утверждай: "квантовая физика доказала, что мысли создают реальность", "эффект
наблюдателя доказывает, что сознание материализует желания", "квантовая запутанность
доказывает энергетическую связь между людьми". Гипотезу называй гипотезой. Интерпретацию —
интерпретацией. Метафору не выдавай за физический закон.

ЭНЕРГИЯ — не смешивай языки: в физике это измеряемая величина, в психологии — субъективное
ощущение, в йоге — прана, в китайской традиции — ци, в эзотерике — тонкая энергия. Вместо
"наука доказала существование энергии чакр" — используй язык сопоставления традиций и
современной науки (нервная система, интероцепция, внимание, эмоциональная регуляция) без
приравнивания одного к другому.

ОРИЕНТИРЫ ПО МЫШЛЕНИЮ (использовать как фундамент понимания, не копировать стиль и не
злоупотреблять цитатами):
- Психология/бессознательное: Карл Густав Юнг (Тень, Персона, Самость, архетипы,
  индивидуация — темы отношений, денег, женской идентичности, страха проявленности),
  Виктор Франкл, Эрих Фромм, Дэниел Канеман
- Философия и внутренняя зрелость: стоицизм (Марк Аврелий, Сенека, Эпиктет), Сократ,
  Платон, Аристотель
- Духовность и сознание: Ошо, Экхарт Толле, буддизм, дзен, йогическая традиция
- Путь и предназначение: Пауло Коэльо, Джозеф Кэмпбелл (путь героя)
- Деньги, мышление, масштаб: Наполеон Хилл, Стивен Кови, Роберт Грин, Нассим Талеб,
  Джеймс Клир
- Наука о медитации/сознании: Richard Davidson, Judson Brewer, Sara Lazar, Tania Singer

Деньги рассматривать комплексно: психология + идентичность + компетентность + стратегия +
социальный капитал + решения + действия + дисциплина + состояние — НИКОГДА не сводить к
"думай позитивно — и станешь богатым". Пример глубины: "Иногда человек сознательно хочет
большего дохода, но бессознательно продолжает защищать прежнюю идентичность. Финансовый
потолок далеко не всегда начинается в банковском счёте. Иногда он начинается в представлении
человека о самом себе."

Никогда не придумывай исследования, цитаты или научные доказательства ради красивого текста.

ФИЛОСОФИЯ АННЫ (итог): "Я не выбираю между духовным и материальным. Мне интересно место их
встречи. Как мысль становится решением. Как состояние становится действием. Как ценности
становятся стратегией. Как внутренний масштаб человека начинает проявляться в масштабе
создаваемых им проектов."

=== ОБЫЧНЫЕ / БЫТОВЫЕ ВОПРОСЫ ===
Если тебе задают вопрос, никак не связанный с Instagram-контентом Анны (общий вопрос,
бытовой совет, что-то посчитать, объяснить, перевести и т.п.) — отвечай на него как обычный
умный полезный ассистент, свободно, по существу и содержательно (не короче и не хуже
ChatGPT), не пытаясь искусственно притянуть его к теме продюсирования контента. Голос при
этом можешь оставить более нейтральным — это не обязано звучать "как Анна", если вопрос не
про её блог. Не сокращай и не упрощай такие ответы искусственно — если вопрос сложный,
отвечай развёрнуто и по делу.

=== ВАЖНО ПРО ВИДЕО ===
Ты НЕ монтируешь видео сама в этом чате — у тебя нет здесь доступа к монтажным инструментам
(vidIQ), они подключены только в основном Project в Claude. Если тебе прислали видеофайл —
бот сам загружает его на публичный хостинг и отдаёт ссылку. Эту ссылку нужно вставить в чат
с агентом «Анна» в Claude (Project), где уже настроен автономный монтаж по сценарию.
"""

# Постоянное хранилище истории переписки — переживает передеплои бота.
# DATA_DIR указывает на подключённый в Railway Volume (переживает redeploy);
# если он не подключён — используем текущую папку (история будет жить только до рестарта).
DATA_DIR = os.environ.get("DATA_DIR", "/data")
HISTORY_FILE = os.path.join(DATA_DIR, "conversations.json")
MAX_HISTORY_MESSAGES = 40  # сколько последних сообщений на чат храним и передаём модели


def _load_conversations() -> dict:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return {int(k): v for k, v in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception:
        logger.exception("Не удалось прочитать файл истории, начинаем с чистого листа")
        return {}


def _save_conversations() -> None:
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(conversations, f, ensure_ascii=False)
    except Exception:
        # Отсутствие возможности сохранить историю не должно ронять бота —
        # просто в следующий раз после рестарта она может быть неполной.
        logger.exception("Не удалось сохранить историю в %s", HISTORY_FILE)


conversations: dict[int, list[dict]] = _load_conversations()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conversations[update.effective_chat.id] = []
    _save_conversations()
    await update.message.reply_text(
        "Привет! Я агент-продюсер для Instagram @anyalakshmi.\n\n"
        "— Напиши, например: 'дай 3 идеи роликов на эту неделю'\n"
        "— Пришли видео файлом — я загружу его и дам публичную ссылку "
        "для монтажа в основном чате с агентом Анна"
    )


async def ask_claude_and_reply(update: Update, chat_id: int, user_text: str) -> None:
    """Общая логика: добавить сообщение в историю, спросить Claude, ответить пользователю."""
    history = conversations.setdefault(chat_id, [])

    history.append({"role": "user", "content": user_text})
    history = history[-MAX_HISTORY_MESSAGES:]

    await update.message.chat.send_action("typing")

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=history,
        )
        reply_text = "".join(
            block.text for block in response.content if block.type == "text"
        )
    except Exception as e:
        logger.exception("Anthropic API error")
        reply_text = f"Произошла ошибка при обращении к агенту: {e}"

    history.append({"role": "assistant", "content": reply_text})
    conversations[chat_id] = history
    _save_conversations()

    await update.message.reply_text(reply_text)


URL_RE = re.compile(r'https?://[^\s<>"\']+')
MAX_FETCH_BYTES = 8 * 1024 * 1024  # 8 МБ — разумный лимит на скачивание содержимого по ссылке
MAX_LINKS_PER_MESSAGE = 2  # не открываем больше пары ссылок за раз, чтобы не перегружать модель


async def fetch_url_content(url: str) -> dict:
    """Скачивает содержимое по ссылке и определяет, как его отдать Клоду."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AnnaProducerBot/1.0)"},
        ) as http_client:
            resp = await http_client.get(url)
            resp.raise_for_status()

            content_length = int(resp.headers.get("content-length") or 0)
            if content_length and content_length > MAX_FETCH_BYTES:
                return {"kind": "error", "message": f"файл слишком большой ({content_length / 1024 / 1024:.1f} МБ)"}

            data = resp.content
            if len(data) > MAX_FETCH_BYTES:
                return {"kind": "error", "message": "файл слишком большой"}

            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()

            if content_type.startswith("image/"):
                return {"kind": "image", "data": data, "mime": content_type}
            if content_type == "application/pdf":
                return {"kind": "pdf", "data": data}
            if "text/html" in content_type or content_type == "":
                soup = BeautifulSoup(data, "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                text = soup.get_text(separator="\n")
                text = re.sub(r'\n\s*\n+', '\n\n', text).strip()
                if not text:
                    return {"kind": "error", "message": "страница пустая или не отдаёт текст без JS"}
                return {"kind": "text", "content": text[:8000]}
            return {"kind": "error", "message": f"не умею обрабатывать содержимое типа {content_type or 'неизвестно'}"}

    except httpx.HTTPStatusError as e:
        return {"kind": "error", "message": f"сайт ответил ошибкой {e.response.status_code}"}
    except Exception as e:
        return {"kind": "error", "message": f"не получилось открыть ссылку: {e}"}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text
    urls = URL_RE.findall(user_text)[:MAX_LINKS_PER_MESSAGE]

    if not urls:
        await ask_claude_and_reply(update, chat_id, user_text)
        return

    await update.message.reply_text("Открываю ссылку, подожди немного...")
    await update.message.chat.send_action("typing")

    history = conversations.setdefault(chat_id, [])
    content_blocks = []
    notes = []

    for url in urls:
        result = await fetch_url_content(url)
        if result["kind"] == "text":
            content_blocks.append({"type": "text", "text": f"[Содержимое страницы {url}]:\n{result['content']}"})
            notes.append(f"[Ссылка: {url}]")
        elif result["kind"] == "image":
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": result["mime"], "data": base64.b64encode(result["data"]).decode("utf-8")},
            })
            notes.append(f"[Изображение по ссылке: {url}]")
        elif result["kind"] == "pdf":
            content_blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(result["data"]).decode("utf-8")},
            })
            notes.append(f"[PDF по ссылке: {url}]")
        else:
            await update.message.reply_text(f"{url} — {result['message']}")
            notes.append(f"[Не удалось открыть ссылку {url}: {result['message']}]")

    content_blocks.append({"type": "text", "text": user_text})

    history.append({"role": "user", "content": content_blocks})
    history_for_model = history[-MAX_HISTORY_MESSAGES:]

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=history_for_model,
        )
        reply_text = "".join(
            block.text for block in response.content if block.type == "text"
        )
    except Exception as e:
        logger.exception("Anthropic API error (link)")
        reply_text = f"Произошла ошибка при обращении к агенту: {e}"

    history[-1] = {"role": "user", "content": " ".join(notes + [user_text])}
    history.append({"role": "assistant", "content": reply_text})
    conversations[chat_id] = history[-MAX_HISTORY_MESSAGES:]
    _save_conversations()

    await update.message.reply_text(reply_text)


TELEGRAM_BOT_FILE_LIMIT = 2000 * 1024 * 1024  # 2000 МБ — лимит собственного (self-hosted) Telegram Bot API сервера


async def download_telegram_file(tg_file) -> bytes:
    """Скачивает файл Telegram.

    Наш self-hosted Bot API сервер запущен в режиме --local: в этом режиме Telegram
    отдаёт в file_path не относительный путь для скачивания по HTTP, а АБСОЛЮТНЫЙ путь
    файла на диске сервера (см. официальную документацию tdlib/telegram-bot-api).
    Сервер telegram-bot-api и бот работают в одном контейнере с общей файловой
    системой, поэтому в этом случае просто читаем файл напрямую с диска — так и
    задумано в local-режиме. Если же путь не абсолютный (обычный режим без --local),
    скачиваем как раньше — через HTTP.
    """
    file_path = tg_file.file_path
    if file_path and os.path.isabs(file_path) and os.path.exists(file_path):
        with open(file_path, "rb") as f:
            return f.read()
    buf = io.BytesIO()
    await tg_file.download_to_memory(out=buf)
    return buf.getvalue()


CLOUDINARY_MAX_BYTES = 100 * 1024 * 1024  # Жёсткий лимит Cloudinary на файл на бесплатном плане
COMPRESS_TARGET_BYTES = int(CLOUDINARY_MAX_BYTES * 0.9)  # с запасом на контейнерные накладные расходы mp4


async def _ffprobe_duration_seconds(path: str) -> float | None:
    """Узнаёт длительность видео через ffprobe — нужна, чтобы посчитать целевой битрейт."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return float(stdout.decode().strip())
    except Exception:
        logger.exception("ffprobe не смог определить длительность видео")
        return None


async def compress_video_if_needed(data: bytes) -> bytes:
    """Если видео больше лимита Cloudinary (100 МБ) — пережимает его через ffmpeg под
    расчётный битрейт так, чтобы результат уложился в лимит. Если что-то пошло не так
    (нет ffmpeg, не удалось определить длительность, сжатие не помогло) — возвращает
    исходные данные без изменений, а дальше решает вызывающий код.
    """
    if len(data) <= CLOUDINARY_MAX_BYTES:
        return data

    with tempfile.TemporaryDirectory() as tmp_dir:
        src_path = os.path.join(tmp_dir, "in.mp4")
        dst_path = os.path.join(tmp_dir, "out.mp4")
        with open(src_path, "wb") as f:
            f.write(data)

        duration = await _ffprobe_duration_seconds(src_path)
        if not duration or duration <= 0:
            return data

        audio_kbps = 128
        target_total_kbps = (COMPRESS_TARGET_BYTES * 8 / 1000) / duration
        video_kbps = max(int(target_total_kbps - audio_kbps), 150)

        cmd = [
            "ffmpeg", "-y", "-i", src_path,
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            dst_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0 or not os.path.exists(dst_path):
            logger.error("ffmpeg сжатие не удалось: %s", stderr.decode(errors="ignore")[-2000:])
            return data

        with open(dst_path, "rb") as f:
            compressed = f.read()

        if not compressed or len(compressed) >= len(data):
            return data
        return compressed


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Голосовые/аудио-сообщения: скачиваем, распознаём через Whisper, отвечаем как на текст."""
    if openai_client is None:
        await update.message.reply_text(
            "Распознавание голоса ещё не настроено (нет ключа OpenAI). "
            "Напиши, пожалуйста, то же самое текстом."
        )
        return

    voice = update.message.voice or update.message.audio
    if voice is None:
        return

    await update.message.reply_text("Слушаю голосовое...")
    await update.message.chat.send_action("typing")

    try:
        tg_file = await context.bot.get_file(voice.file_id)
        data = await download_telegram_file(tg_file)
        buf = io.BytesIO(data)
        buf.seek(0)
        buf.name = "voice.ogg"  # Whisper API определяет формат по расширению имени файла

        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=buf,
        )
        recognized_text = transcript.text.strip()

        if not recognized_text:
            await update.message.reply_text(
                "Не расслышала, там как будто пусто. Попробуй ещё раз или напиши текстом."
            )
            return

        await update.message.reply_text(f"Расслышала: «{recognized_text}»")

        chat_id = update.effective_chat.id
        await ask_claude_and_reply(update, chat_id, recognized_text)

    except Exception as e:
        logger.exception("Voice transcription error")
        await update.message.reply_text(
            f"Не получилось распознать голосовое: {e}\n"
            "Попробуй ещё раз или напиши текстом."
        )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Принимает видео (как видео-сообщение или как документ) и загружает на Cloudinary."""
    video = update.message.video or update.message.document
    if video is None:
        return

    # Проверяем размер заранее, чтобы не тратить время и не пугать техническим текстом ошибки.
    file_size = getattr(video, "file_size", None)
    if file_size and file_size > TELEGRAM_BOT_FILE_LIMIT:
        size_mb = file_size / (1024 * 1024)
        await update.message.reply_text(
            f"Файл весит {size_mb:.1f} МБ — это больше {TELEGRAM_BOT_FILE_LIMIT / 1024 / 1024:.0f} МБ, "
            "текущего лимита на скачивание файлов ботом.\n\n"
            "Залей на Google Drive/Dropbox с доступом «по ссылке» и пришли мне ссылку текстом."
        )
        return

    await update.message.reply_text("Загружаю видео, подожди немного...")
    await update.message.chat.send_action("upload_video")

    try:
        tg_file = await context.bot.get_file(video.file_id)
        data = await download_telegram_file(tg_file)

        if len(data) > CLOUDINARY_MAX_BYTES:
            await update.message.reply_text(
                f"Видео весит {len(data) / 1024 / 1024:.0f} МБ — это больше 100 МБ, "
                "лимита Cloudinary на загрузку. Сжимаю, это может занять пару минут..."
            )
            data = await compress_video_if_needed(data)
            if len(data) > CLOUDINARY_MAX_BYTES:
                await update.message.reply_text(
                    "Не получилось сжать видео до нужного размера (меньше 100 МБ).\n\n"
                    "Сожми его вручную (например, в Telegram при отправке) или залей "
                    "на Google Drive/Dropbox с доступом «по ссылке» и пришли мне ссылку текстом."
                )
                return

        buf = io.BytesIO(data)
        buf.seek(0)

        upload_result = cloudinary.uploader.upload_large(
            buf,
            resource_type="video",
            folder="anna_producer_bot",
        )
        public_url = upload_result["secure_url"]

        await update.message.reply_text(
            "Готово! Публичная ссылка на видео:\n\n"
            f"{public_url}\n\n"
            "Вставь эту ссылку в чат с агентом «Анна» в Claude (Project) — "
            "там подключён монтаж, и агент сам разберёт и смонтирует ролик."
        )
    except Exception as e:
        logger.exception("Video upload error")
        error_text = str(e)
        if "too big" in error_text.lower() or "file is too big" in error_text.lower() or "too large" in error_text.lower():
            await update.message.reply_text(
                "Файл слишком большой для загрузки (даже после сжатия).\n\n"
                "Сожми видео вручную или залей на Google Drive/Dropbox и пришли ссылку."
            )
        else:
            await update.message.reply_text(
                f"Не получилось загрузить видео: {e}\n"
                "Попробуй ещё раз или залей вручную на Google Drive и пришли ссылку."
            )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фото: скачиваем, отправляем в Claude как изображение (у Claude есть зрение), отвечаем."""
    photo = update.message.photo[-1] if update.message.photo else None
    if photo is None:
        return

    caption = (update.message.caption or "").strip()

    await update.message.chat.send_action("typing")

    try:
        tg_file = await context.bot.get_file(photo.file_id)
        data = await download_telegram_file(tg_file)
        image_b64 = base64.b64encode(data).decode("utf-8")

        chat_id = update.effective_chat.id
        history = conversations.setdefault(chat_id, [])

        vision_prompt = caption if caption else (
            "Посмотри на это фото и прокомментируй его содержательно — как продюсер, "
            "если это релевантно контенту Анны, или просто по существу, если это что-то другое."
        )

        history.append({
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": vision_prompt},
            ],
        })
        history_for_model = history[-MAX_HISTORY_MESSAGES:]

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=history_for_model,
        )
        reply_text = "".join(
            block.text for block in response.content if block.type == "text"
        )

        # В истории саму картинку текстом не храним (не нужно раздувать файл) — оставляем заметку.
        user_note = f"[Фото] {caption}" if caption else "[Фото без подписи]"
        history[-1] = {"role": "user", "content": user_note}
        history.append({"role": "assistant", "content": reply_text})
        conversations[chat_id] = history[-MAX_HISTORY_MESSAGES:]
        _save_conversations()

        await update.message.reply_text(reply_text)

    except Exception as e:
        logger.exception("Photo analysis error")
        await update.message.reply_text(
            f"Не получилось разобрать фото: {e}\nПопробуй ещё раз."
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Файлы-документы: PDF и изображения, отправленные как файл (не сжатые фото)."""
    doc = update.message.document
    if doc is None:
        return

    mime = (doc.mime_type or "").lower()
    file_name = doc.file_name or ""
    caption = (update.message.caption or "").strip()

    is_pdf = mime == "application/pdf" or file_name.lower().endswith(".pdf")
    is_image = mime.startswith("image/")

    if not is_pdf and not is_image:
        await update.message.reply_text(
            f"Пока не умею читать файлы такого типа ({mime or 'неизвестный формат'}).\n"
            "Поддерживаю: PDF и изображения (файлом или как фото). Видео — тоже ок, грузится отдельно."
        )
        return

    file_size = getattr(doc, "file_size", None)
    if file_size and file_size > TELEGRAM_BOT_FILE_LIMIT:
        size_mb = file_size / (1024 * 1024)
        await update.message.reply_text(
            f"Файл весит {size_mb:.1f} МБ — это больше {TELEGRAM_BOT_FILE_LIMIT / 1024 / 1024:.0f} МБ, "
            "текущего лимита на скачивание файлов ботом."
        )
        return

    await update.message.chat.send_action("typing")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = await download_telegram_file(tg_file)
        file_b64 = base64.b64encode(data).decode("utf-8")

        chat_id = update.effective_chat.id
        history = conversations.setdefault(chat_id, [])

        if is_pdf:
            content_block = {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": file_b64},
            }
            default_prompt = "Посмотри этот PDF и разбери содержимое по существу."
            note_label = f"[PDF: {file_name}]"
        else:
            content_block = {
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": file_b64},
            }
            default_prompt = (
                "Посмотри на это изображение и прокомментируй его содержательно — как продюсер, "
                "если это релевантно контенту Анны, или просто по существу, если это что-то другое."
            )
            note_label = f"[Изображение-файл: {file_name}]"

        vision_prompt = caption if caption else default_prompt

        history.append({
            "role": "user",
            "content": [content_block, {"type": "text", "text": vision_prompt}],
        })
        history_for_model = history[-MAX_HISTORY_MESSAGES:]

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=history_for_model,
        )
        reply_text = "".join(
            block.text for block in response.content if block.type == "text"
        )

        user_note = f"{note_label} {caption}" if caption else note_label
        history[-1] = {"role": "user", "content": user_note}
        history.append({"role": "assistant", "content": reply_text})
        conversations[chat_id] = history[-MAX_HISTORY_MESSAGES:]
        _save_conversations()

        await update.message.reply_text(reply_text)

    except Exception as e:
        logger.exception("Document analysis error")
        await update.message.reply_text(
            f"Не получилось разобрать файл: {e}\nПопробуй ещё раз."
        )


def main() -> None:
    # Свой (self-hosted) Telegram Bot API сервер — снимает стандартный лимит Telegram
    # в 20 МБ на скачивание файлов ботом (там теперь безлимитно на download, до 2 ГБ на
    # upload). Сервер telegram-bot-api и этот процесс запускаются в ОДНОМ контейнере
    # (см. start.sh) и делят общий диск — это обязательно для режима --local, в котором
    # getFile отдаёт абсолютный локальный путь вместо ссылки для HTTP-скачивания
    # (см. download_telegram_file выше).
    local_api_url = os.environ.get("LOCAL_BOT_API_URL", "http://localhost:8081")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .base_url(f"{local_api_url}/bot")
        .base_file_url(f"{local_api_url}/file/bot")
        .connect_timeout(60)
        .read_timeout(120)
        .write_timeout(120)
        .pool_timeout(60)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.VIDEO, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
