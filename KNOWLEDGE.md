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
- Медленная сеть (с 2026-11-03): закачка дольше `DOWNLOAD_TIMEOUT` (120 с) обрывается через progress hook
  (`SlowDownload`, «operation too slow») и ссылка уходит в очередь; `socket_timeout` 20 с.
  При сетевой ошибке повтор без cookies не делается. Очередь повторяется, только если `network_fast()`:
  1 МБ с speed.cloudflare.com качается со скоростью ≥ `MIN_SPEED` (200 КБ/с).

## Дочитка пропущенных постов (с 2026-11-19)
- `history.py`, состояние в `/data/channels.json`: по каждому каналу `last` (последний виденный id), `done` (обработанные), `sent` (что отправил сам бот; пишет request-middleware `RememberSent`).
- При старте через 30 с `catch_up` проходит каждый канал с `last-19` вперёд до 30 пропусков подряд.
  Bot API не умеет читать историю, поэтому пост пересылается в личку владельцу (`SCAN_CHAT_ID`, задаётся переменной окружения, без неё дочитка не работает) без звука и сразу удаляется.
- Отвеченность без состояния: после поста со ссылкой должен идти ответ бота («📝 Выжимка», «Не удалось», «📡»…), иначе ссылка обрабатывается (`_answered`).
- `claim`/`release` не дают живому обработчику и дочитке ответить на один пост дважды.
- Подключить канал, которого нет в `channels.json`: переслать боту в личку пост из канала (только из `SCAN_CHAT_ID`) — проверка пойдёт с этого поста.
  Новый канал также регистрируется сам по `my_chat_member` и первому посту.
- Обрабатываются все ссылки в сообщении и ссылки в подписях к фото/видео.

## Выжимки: Gemini → Claude
- Сначала Gemini (`gemini-flash-latest`, потом запасные). Если все упёрлись в 429/500/503 — Claude.
- Claude идёт через хост: бот кладёт `job.json` + 8 кадров ролика в `/data/claude/<id>/` (на хосте `~/linkbot-data/claude`),
  systemd-сервис `linkbot-claude` (`claude_worker.py`, юнит в репо `linkbot-claude.service`) запускает `claude -p` под логином пользователя server и пишет `result.json`.
  Журнал: `journalctl -u linkbot-claude`. Таймаут ожидания в боте 420 с.

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
- **2026-10-21 22:52–23:10 UTC** — `[download] Got error: Connection broken: BrokenPipeError` при повторе из очереди.
  Причина: снова деградация сети до Instagram (DNS-ошибки, read timeout к cdninstagram, curl 28; из контейнера instagram.com
  не отвечал). Очередь сработала: рилс DeW0nZUowzl из двух каналов в `pending.json`. Дыра: «broken pipe»/«connection broken»
  не входили в `NETWORK_MARKERS` — если бы это была последняя ошибка при живом `internet_ok()`, ссылка ушла бы в failed.
  Фикс: добавлены маркеры, коммит 0fcbc1d.
- **2026-11-03 ~20:40–21:20 UTC** — ссылки обрабатывались по 2.5–12 минут, два рилса ушли в очередь.
  Причина: на сервере почти не работает интернет — Wi-Fi (wlp3s0, сигнал −76 dBm), скорость ~16–20 КБ/с,
  IPv6 не качает вообще, DNS отвечает 0.5–1.5 с, временами `Could not resolve host`. Ping при этом нормальный.
  Код ни при чём: медленная закачка с cdninstagram → read timeout → повтор без cookies → DNS-ошибка → очередь.
  Фикс: обрыв медленной закачки и очередь (см. «Очередь»).
  Проверка: `curl -4 -so /dev/null -w "%{speed_download}\n" "https://speed.cloudflare.com/__down?bytes=10000000"`.

- **2026-11-05 23:04 → 2026-11-19 10:21 UTC** — сервер был выключен 2 недели. После включения бот не ответил на 7 ссылок:
  Telegram хранит неполученные апдейты только 24 ч. Также json-лог Docker испорчен нулями (`docker logs` молчал, ошибка
  «Error decoding log file \x00») — пересоздание контейнера даёт новый лог. Фикс: дочитка пропущенных постов (см. выше), 7 ссылок обработаны.- **2026-11-20 10:47 UTC** — `[Instagram] Df8Z9NJtJ6P: Video info extraction failed: HTTP Error 400` (рилс из лички и канала).
  Причина: снова деградация сети — всю ночь таймауты к Telegram, из контейнера `yt-dlp -s` к instagram.com падает с curl 28
  (connect timeout 20 с). Попытка с cookies получила 400 от Instagram API, повтор без cookies упёрся в таймаут → ссылка ушла в очередь
  (`No network, queued`). Код отработал штатно, изменений нет. Если 400 с cookies повторится при живой сети — проверить вход в Instagram в `browser`.

## Полезное
- Если `docker logs` пуст после аварийного выключения — читать файл напрямую: `sudo cat $(docker inspect -f {{.LogPath}} link_bot-bot-1) | tr -d '\000'`.
- Проверить скачивание вручную: `docker exec link_bot-bot-1 yt-dlp -s <url>`.
- `all bot.py` — пустой файл, лежит в репозитории исторически.
