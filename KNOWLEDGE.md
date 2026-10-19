# LinkBot — база знаний

Бот @link_parcer_bot: присылаешь ссылку на Instagram/TikTok/YouTube — скачивает видео (или фото с музыкой),
отправляет его и дописывает в подпись выжимку от Gemini. Для обычных ссылок на статьи — только выжимка.

## Устройство
- Один файл `main.py`, aiogram 3, yt-dlp, ffmpeg, Gemini (`google-genai`).
- `get_media` → `fetch` (yt-dlp) → `make_telegram_friendly` (H.264 + faststart, иначе Telegram играет только звук).
- Instagram: cookies из профиля Chromium контейнера `browser` (`BROWSER_PROFILE`), при ошибке повтор без cookies.
- Посты только с фото: картинка + музыка из Instagram API → `photo_with_music` склеивает видео.
- Лимит Telegram для ботов — 50 МБ; больше — только выжимка.
- `process_link` — общая логика для нового сообщения и для повтора из очереди.

## Очередь при сбоях сети (с 2026-10-18)
- `is_network_error` смотрит всю цепочку исключений (таймауты, DNS, curl 6/7/28/35/56, TelegramNetworkError).
- Если ошибка сетевая или `internet_ok()` = False — ссылка пишется в `/data/pending.json` (том `~/linkbot-data`).
- `retry_pending` раз в минуту: есть сеть → `retry_one` докачивает и отвечает на исходное сообщение.
  Статус-сообщение переиспользуется. Через 24 ч без сети — сдаётся.
- Закрытые/удалённые посты в очередь не попадают.

## Деплой
- Стек Portainer `link_bot`, файлы стека: `/var/lib/docker/volumes/portainer_data/_data/compose/1`, секреты в `stack.env`.
- Сервисы: `bot` (наш код) и `browser` (lscr.io/linuxserver/chromium, порт 127.0.0.1:3000, для входа в Instagram).
- Тома: `/home/server/linkbot-browser:/browser:ro`, `/home/server/linkbot-data:/data`.

## Логи
- Статистика для админ-бота: `stat(result, source)` пишет `event: "stat", stat: "links"` с `result` (video/photo/summary/failed/private/too_big/queued) и `source`.
- JSON в stdout (`logsetup.py`), поля: ts, level, bot=link-bot, logger, msg, where, update_id, chat_id, user_id, text, traceback.
- yt-dlp пишет через `YdlLogger` в root-логгер; ожидаемые предупреждения для фото-постов отфильтрованы.

## Инциденты
- **2026-10-18 07:25–07:28 UTC** — «Не удалось скачать видео». Причина: на сервере пропадали сеть/DNS
  (systemd-resolved, DNS Tailscale 100.100.100.100 в degraded-режиме), таймауты к Telegram, instagram.com, cdninstagram, tiktok.
  Код ни при чём. Решение: очередь повторов (см. выше). Упавшие до фикса ссылки не восстановить — chat_id не логировался.

## Полезное
- Проверить скачивание вручную: `docker exec link_bot-bot-1 yt-dlp -s <url>`.
- `all bot.py` — пустой файл, лежит в репозитории исторически.
