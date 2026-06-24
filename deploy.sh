#!/usr/bin/env bash
# Запускается НА СЕРВЕРЕ. Подтягивает последнюю версию с GitHub и перезапускает бота.
set -euo pipefail

REPO_DIR="/opt/wc-bot"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

cd "$REPO_DIR"
git fetch --quiet origin
git reset --hard origin/main          # сервер = точная копия GitHub (тут НЕ редактируем!)

# venv + зависимости
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt

# обновить unit, если менялся, и перезапустить
$SUDO cp wc-bot.service /etc/systemd/system/wc-bot.service
$SUDO systemctl daemon-reload
$SUDO systemctl restart wc-bot

echo "deployed: $(git rev-parse --short HEAD)"
