#!/bin/bash
# Запускает в одном контейнере два процесса, которым нужен общий диск:
# 1) self-hosted Telegram Bot API сервер (режим --local: без лимита на скачивание,
#    до 2 ГБ на загрузку, getFile отдаёт локальный путь файла вместо HTTP-ссылки);
# 2) самого Python-бота, который читает файлы напрямую с этого диска.
set -euo pipefail

: "${TELEGRAM_API_ID:?TELEGRAM_API_ID is required (см. my.telegram.org)}"
: "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH is required (см. my.telegram.org)}"

telegram-bot-api \
  --local \
  --api-id="$TELEGRAM_API_ID" \
  --api-hash="$TELEGRAM_API_HASH" \
  --http-port=8081 \
  --dir="${TELEGRAM_WORK_DIR:-/var/lib/telegram-bot-api}" \
  --temp-dir="${TELEGRAM_TEMP_DIR:-/tmp/telegram-bot-api}" &
BOTAPI_PID=$!

echo "[start.sh] Waiting for local Telegram Bot API server on :8081..."
python3 -c "
import socket, time, sys
for _ in range(60):
    try:
        socket.create_connection(('localhost', 8081), timeout=1).close()
        sys.exit(0)
    except OSError:
        time.sleep(1)
sys.exit(1)
" || { echo '[start.sh] Local Bot API server did not come up in time'; exit 1; }
echo "[start.sh] Local Bot API server is up."

python3 bot.py &
BOT_PID=$!

# Если любой из двух процессов упадёт — останавливаем контейнер целиком,
# чтобы Railway перезапустил оба заново в чистом состоянии.
wait -n "$BOTAPI_PID" "$BOT_PID"
exit_code=$?
echo "[start.sh] One of the processes exited (code $exit_code), stopping."
exit "$exit_code"
