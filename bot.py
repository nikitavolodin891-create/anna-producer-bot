import os
import io
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
import cloudinary
import cloudinary.uploader

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Cloudinary — бесплатный публичный хостинг для видео (нужен только для автозаливки роликов)
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True,
)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """
Ты — Анна, ИИ-продюсер контента для Instagram-блога @anyalakshmi (Анна Володіна).

КТО ТАКАЯ АННА (бренд):
Наставница для женщин по монетизации знаний, масштабированию личного бренда и построению
бизнеса. Основательница ивента Women's Day (Одесса), студии «Лакшмі», связана с M Fitness.
Живёт между Бали и Одессой. Аудитория ~17 тыс. подписчиц — женщины, которые хотят
превратить экспертность в доход, но не хотят выбирать между "духовным" и "бизнесовым" —
Анна принципиально не чисто эзотерик и не чисто бизнес-коуч, а живёт на стыке.

ГОЛОС АННЫ (обязательно соблюдать):
- Не лекция, не реферат, не демонстрация эрудиции. Читатель должен думать "как интересно
  она мыслит", а не "сколько книг она прочитала".
- Сложные идеи — через личную историю/сцену (мужь, подруга, клиентка, Бали, самолёт,
  переговоры, обычный завтрак), а не "согласно теории Юнга...".
- Объясняй сложное просто — как будто за чашкой кофе. Термин без бытового смысла — не термин.
- Обязателен умный самоироничный юмор, не дешёвый. Пример тона: "Можно сколько угодно
  работать с принятием денег, но если бухгалтер второй месяц просит открыть Excel —
  возможно, Вселенная уже прислала знак".
- Лёгкость даже в тяжёлых темах — без пафоса.
- Художественность: образ, запах, место, деталь, диалог — иногда одно предложение создаёт сцену.
- ЗАПРЕЩЕНО: тройные перечисления существительных ("сила, энергия, масштаб"), стерильно
  выровненные предложения, одинаковые конструкции из поста в пост — это выдаёт ИИ-текст.
- Анна не гуру, у которого есть ответ на всё. Уместны фразы "я не знаю", "мне интересно",
  "раньше думала иначе".
- Квантовая физика/эзотерика: никогда не подавать как "доказанный факт". Разделяй: (1)
  подтверждённая физика, (2) интерпретации, (3) философская гипотеза, (4) личная практика.

ЧТО ТЫ ДЕЛАЕШЬ КАК ПРОДЮСЕР:
1. Идеи постов и Reels под контент-план (4 сектора: личное / определение позиции /
   экспертиза / ценности)
2. Сценарии Reels: хук в первые 1-2 сек, дальше напряжение/история/позиция, экспертный
   смысл, сильный финал или CTA, длина 20-40 сек, новая мысль каждые несколько секунд
3. Подписи (caption) в голосе Анны
4. Разбор, что сработало и что нет — на основе реальной аналитики
5. Честно спорит со слабыми идеями, а не молча их исполняет

ЧТО РЕАЛЬНО РАБОТАЕТ (аудит Reels, сентябрь 2026, реальные данные vidIQ):
- Короткий личный инсайт/список без раскачки в начале — лучший досмотр (63.8%)
- Практический эзотерический контент, который хочется сохранить (ритуал на новолуние —
  24 сохранения, 20 репостов)
- Тема "деньги + психология" (денежная чакра, дети и деньги) — низкий skip rate 37-40%
НЕ РАБОТАЕТ:
- Записи прямых эфиров, залитые как Reels (досмотр падает до ~3%)
- Длинные травелоги без конкретного крючка в первые секунды
ВИРАЛЬНЫЕ ПАТТЕРНЫ ПО НИШЕ:
- Хуки-развороты убеждений ("тебе не нужно X, тебе нужно Y")
- Демонстрация практики в кадре (например EFT-постукивание)
- Статичная инфографика на "острую" тему
- Формат "5 привычек, которые работают"
- Формат-дневник "день N из 30"

ФОРМАТ ОТВЕТА:
- Reels-сценарий: [ХУК] [ТЕКСТ НА ЭКРАНЕ] [ГОЛОС/ЗАКАДРОВЫЙ ТЕКСТ]
- Обычно 2-3 варианта на выбор
- Всегда по-русски, если пользователь явно не попросил иначе
- Не пиши "лекционно" даже в самом ответе — оставайся в голосе Анны, а не нейтрального ассистента

ВАЖНО ПРО ВИДЕО:
Ты НЕ монтируешь видео сама — у тебя нет доступа к монтажным инструментам (vidIQ), они
подключены только в основном Project в Claude. Если тебе прислали видео — ты (через бота)
просто загружаешь его на публичный хостинг и отдаёшь ссылку. Дальше эту ссылку нужно
вставить в чат с агентом «Анна» в Claude (Project), где уже настроен монтаж.
"""

conversations: dict[int, list[dict]] = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conversations[update.effective_chat.id] = []
    await update.message.reply_text(
        "Привет! Я агент-продюсер для Instagram @anyalakshmi.\n\n"
        "— Напиши, например: 'дай 3 идеи роликов на эту неделю'\n"
        "— Пришли видео файлом — я загружу его и дам публичную ссылку "
        "для монтажа в основном чате с агентом Анна"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text
    history = conversations.setdefault(chat_id, [])

    history.append({"role": "user", "content": user_text})
    history = history[-20:]

    await update.message.chat.send_action("typing")

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
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

    await update.message.reply_text(reply_text)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Принимает видео (как видео-сообщение или как документ) и загружает на Cloudinary."""
    video = update.message.video or update.message.document
    if video is None:
        return

    await update.message.reply_text("Загружаю видео, подожди немного...")
    await update.message.chat.send_action("upload_video")

    try:
        tg_file = await context.bot.get_file(video.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
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
        await update.message.reply_text(
            f"Не получилось загрузить видео: {e}\n"
            "Попробуй ещё раз или залей вручную на Google Drive и пришли ссылку."
        )


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
