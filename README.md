# World Cup Polymarket → Telegram bot

Один Python-файл, который следит за сегодняшними матчами ЧМ-2026 на Polymarket
и шлёт уведомления в Telegram. Редактируешь на GitHub — сервер подтягивает к себе.

## Как устроен деплой
- **GitHub** — источник правды (тут редактируешь файл).
- **VPS** — держит клон репозитория в `/opt/wc-bot` и запускает бота через systemd.
- **Секреты** (токен бота, chat_id) лежат ТОЛЬКО на сервере в `/etc/wc-bot.env`
  и в git не попадают.
- **Деплой** = `git pull` + перезапуск сервиса (скрипт `deploy.sh`).

---

## 1. Первичная настройка сервера (один раз)

```bash
# на VPS, под root (или через sudo)
apt update && apt install -y git python3-venv

# клонируем твой репозиторий в /opt/wc-bot
git clone https://github.com/USERNAME/REPO.git /opt/wc-bot
cd /opt/wc-bot

# секреты — создаём env-файл (в репозиторий он НЕ входит)
cp wc-bot.env.example /etc/wc-bot.env
nano /etc/wc-bot.env        # впиши TG_BOT_TOKEN и TG_CHAT_ID

# окружение + зависимости + сервис
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp wc-bot.service /etc/systemd/system/wc-bot.service
systemctl daemon-reload
systemctl enable --now wc-bot

# смотрим логи (тут видно, какие игры распознались)
journalctl -u wc-bot -f
```

---

## 2. Как обновлять (выбери ОДИН способ)

### Способ A — вручную (самый простой)
Отредактировал файл на GitHub → на сервере:
```bash
/opt/wc-bot/deploy.sh
```

### Способ B — авто-деплой при push (GitHub Actions)
Файл `.github/workflows/deploy.yml` уже в репозитории.
В GitHub: **Settings → Secrets and variables → Actions** добавь:
- `VPS_HOST` — ip/домен сервера
- `VPS_USER` — ssh-пользователь (например `root`)
- `VPS_SSH_KEY` — приватный ssh-ключ (публичный положи в `~/.ssh/authorized_keys` на сервере)

Теперь каждый push в `main` сам зайдёт на сервер и выполнит `deploy.sh`.

### Способ C — сервер сам опрашивает GitHub (без Actions, без секретов)
```bash
cp /opt/wc-bot/wc-bot-autopull.* /etc/systemd/system/
systemctl enable --now wc-bot-autopull.timer
```
Каждые 2 минуты сервер делает pull и перезапускается, если что-то изменилось.

---

## Важно
- На сервере **ничего не редактируй руками** — `deploy.sh` делает `git reset --hard`
  и затрёт локальные правки. Все изменения только через GitHub.
- Ветка по умолчанию — `main`. Если у тебя `master`, поправь `deploy.sh` и `deploy.yml`.
- Сменил пороги/токен — токен меняется в `/etc/wc-bot.env` на сервере, пороги в коде на GitHub.
