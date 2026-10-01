# Запуск бота на Linux

Инструкция для того, кто разворачивает бота (человек или Claude). Бот — один файл `main.py`:
Telegram-бот на aiogram (long polling), качает видео через yt-dlp, перекодирует ffmpeg,
делает выжимку через Gemini.

## 1. Системные пакеты

```bash
# Debian / Ubuntu
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg git curl unzip
```

- **Python 3.12+** (разработка велась на 3.14; код использует `str | None`, поэтому минимум 3.10).
- **ffmpeg и ffprobe** — обязательны, вызываются через `subprocess` (`probe()`, `make_telegram_friendly()`,
  нарезка музыки). Проверка: `ffmpeg -version && ffprobe -version`.
- **Deno** — JS-рантайм, который нужен yt-dlp для YouTube (в коде включено `remote_components: ['ejs:github']`,
  сам скрипт скачивается с GitHub, но исполнять его нужно через deno). Без него YouTube не будет работать:

  ```bash
  curl -fsSL https://deno.land/install.sh | sh
  # добавить ~/.deno/bin в PATH того пользователя, от которого запускается бот
  ```

## 2. Python-зависимости

```bash
cd /path/to/Bot_project
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

`curl_cffi` нужен yt-dlp для имитации браузера (TikTok/Instagram).

## 3. Переменные окружения

Скопировать `.env.example` в `.env` и заполнить:

| Переменная       | Обязательна | Описание                                         |
|------------------|-------------|--------------------------------------------------|
| `BOT_TOKEN`      | да          | токен бота от @BotFather                         |
| `GEMINI_API_KEY` | да          | ключ Google AI Studio                            |
| `GEMINI_MODEL`   | нет         | модель Gemini, по умолчанию `gemini-flash-latest` |

`.env` в git не коммитить (он в `.gitignore`).

## 4. Instagram: вход через браузер на сервере

Instagram блокирует аккаунт (`checkpoint_required`), если cookies с домашнего ПК начинают
использоваться с IP сервера. Поэтому входить в Instagram нужно с самого сервера — через
контейнер `browser` (Chromium с доступом через веб), бот читает cookies из его профиля.

1. В `.env` задать `BROWSER_PASSWORD` (логин — `admin` или `BROWSER_USER`).
2. `mkdir -p /home/server/linkbot-browser && sudo chown 1000:1000 /home/server/linkbot-browser`
3. `docker compose up -d --build`
4. Порт браузера открыт только на localhost сервера. С компьютера пробросить туннель:
   `ssh -L 3000:localhost:3000 server@<ip-сервера>` и открыть http://localhost:3000
5. В открывшемся Chromium войти в Instagram (лучше отдельный аккаунт для бота).
6. Готово: бот берёт cookies из `/home/server/linkbot-browser/.config/chromium/Default`.
   Браузер можно оставить запущенным или остановить: `docker compose stop browser`.

Если профиля нет или cookies не работают, бот качает без входа: публичные посты скачиваются,
у фото-постов не будет музыки, закрытые и 18+ посты не скачаются.

## 5. Запуск

Проверочный запуск:

```bash
.venv/bin/python main.py
```

Постоянная работа — через systemd, `/etc/systemd/system/tgbot.service`:

```ini
[Unit]
Description=Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=botuser
WorkingDirectory=/path/to/Bot_project
Environment=PATH=/home/botuser/.deno/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/path/to/Bot_project/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tgbot
journalctl -u tgbot -f   # логи
```

Бот должен быть запущен в одном экземпляре: два процесса с одним токеном конфликтуют
(`TelegramConflictError`), поэтому локальный запуск надо остановить.

## 6. Обслуживание

- yt-dlp часто ломается из-за изменений на сайтах — при ошибках скачивания первым делом
  `.venv/bin/pip install -U yt-dlp`.
- Временные файлы создаются через `tempfile`, в `/tmp` должно быть место под видео (до ~50 МБ+ на запрос).
