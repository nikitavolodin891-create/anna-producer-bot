# Собираем в одном контейнере self-hosted Telegram Bot API сервер (--local режим,
# без лимита на скачивание и до 2 ГБ на загрузку) и самого Python-бота. Это обязательно:
# в режиме --local сервер отдаёт файлы как абсолютные пути на диске, а не по HTTP —
# значит боту и серверу нужна общая файловая система, то есть один контейнер.
#
# Бинарник telegram-bot-api берём готовым из официального образа aiogram/telegram-bot-api
# (тот же tdlib/telegram-bot-api, собранный на Alpine) — это быстро и не требует часовой
# компиляции C++ при каждом деплое.
FROM aiogram/telegram-bot-api:latest AS botapi

FROM python:3.13-alpine

# Рантайм-зависимости бинарника telegram-bot-api (собран на Alpine/musl) + bash
# (нужен start.sh для `wait -n`, которого нет в стандартном ash/busybox).
RUN apk add --no-cache openssl libstdc++ ca-certificates bash

COPY --from=botapi /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

# Временные build-инструменты на случай, если для какого-то пакета нет готового
# musllinux-колеса и pip придётся собирать его из исходников.
RUN apk add --no-cache --virtual .build-deps gcc musl-dev libffi-dev
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apk del .build-deps

COPY bot.py .
COPY start.sh .
RUN chmod +x start.sh

ENV TELEGRAM_WORK_DIR=/var/lib/telegram-bot-api \
    TELEGRAM_TEMP_DIR=/tmp/telegram-bot-api
RUN mkdir -p /var/lib/telegram-bot-api /tmp/telegram-bot-api

CMD ["./start.sh"]
