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

## 4. ⚠️ Instagram: cookies

В `get_media()` для Instagram стоит `ydl_opts['cookiesfrombrowser'] = ('chrome',)` — cookies берутся
из локального Chrome. **На сервере без Chrome это упадёт.** Варианты:

1. Экспортировать cookies instagram.com из браузера в формате Netscape (`cookies.txt`, например расширением
   «Get cookies.txt LOCALLY»), положить на сервер и заменить строку на
   `ydl_opts['cookiefile'] = 'cookies.txt'` (путь лучше вынести в переменную окружения).
2. Если залогиненный Chrome на сервере есть — оставить как есть.

`cookies.txt` тоже нельзя коммитить (добавить в `.gitignore`).

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
