#!/bin/sh
set -eu
sed \
  -e "s|BOT_TOKEN_PLACEHOLDER|${TELEGRAM_BOT_TOKEN}|g" \
  -e "s|CHAT_ID_PLACEHOLDER|${TELEGRAM_CHAT_ID}|g" \
  /etc/alertmanager/alertmanager.yml.template > /tmp/alertmanager.yml
exec /bin/alertmanager --config.file=/tmp/alertmanager.yml --storage.path=/alertmanager
