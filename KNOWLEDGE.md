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

## Протухшие cookies Instagram (с 2026-11-20)
- `cookies.py`: если скачивание с cookies упало не из-за сети, а без cookies прошло — cookies считаются мёртвыми (`mark_dead`).
- `cookies.watch` раз в минуту: шлёт владельцу (`SCAN_CHAT_ID`) «🔑 Instagram разлогинил бота…» со ссылкой на браузер (`BROWSER_URL`), напоминает раз в сутки.
- Когда меняется файл `Cookies` профиля (человек залогинился), не чаще раза в 5 мин проверяет cookies на упавшей ссылке
  (`instagram_cookies_work`); работают — пишет «✅ Вход в Instagram сохранён». Также оживляет любая удачная закачка с cookies.
- Cookies живут в профиле Chromium (`~/linkbot-browser`), бот читает их при каждой закачке — после входа сохранять ничего не надо.
- Браузер снаружи: `tailscale serve --bg --https=3443 http://127.0.0.1:3000` → https://server.tail637fbe.ts.net:3443/
  (только внутри tailnet: iPhone и MacBook владельца). Вход — BROWSER_USER/BROWSER_PASSWORD из stack.env.
- `SCAN_CHAT_ID` и `BROWSER_URL` заданы в `stack.env` (дефолт SCAN_CHAT_ID в compose — 0).

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
  «Error decoding log file \x00») — пересоздание контейнера даёт новый лог. Фикс: дочитка пропущенных постов (см. выше), 7 ссылок обработаны.
- **2026-11-20 10:47 UTC** — `[Instagram] Df8Z9NJtJ6P: Video info extraction failed: HTTP Error 400` (рилс из лички и канала).
  Причина: снова деградация сети — всю ночь таймауты к Telegram, из контейнера `yt-dlp -s` к instagram.com падает с curl 28
  (connect timeout 20 с). Попытка с cookies получила 400 от Instagram API, повтор без cookies упёрся в таймаут → ссылка ушла в очередь
  (`No network, queued`). Код отработал штатно, изменений нет. Если 400 с cookies повторится при живой сети — проверить вход в Instagram в `browser`.
  Дополнение: при живой сети 400 с cookies повторялся стабильно, без cookies рилс открывался — вход протух (cookies от 2026-10-01). Фикс: детектор протухших cookies (см. выше). Обновление yt-dlp до 2026.10.26 отложено: PyPI с сервера не качался.
- **2026-11-20 12:32 UTC** — снова `[Instagram] Df8Z9NJtJ6P: Video info extraction failed: HTTP Error 400` при повторе из очереди.
  Причина: cookies по-прежнему протухшие (с cookies 400, без них рилс находится и качается), но сеть медленная (~50 КБ/с):
  повтор без cookies обрывался по `DOWNLOAD_TIMEOUT` (`SlowDownload`) и молча возвращался в очередь. `mark_dead` вызывался
  только после удачной закачки, поэтому владелец не получал «🔑 Instagram разлогинил бота».
  Фикс: `SlowDownload` при повторе без cookies тоже помечает cookies мёртвыми, коммит 178d70e. Нужен вход в Instagram в `browser`.
- **2026-11-20 13:3x–14:05 UTC** — `[Instagram] Df8Z9NJtJ6P: Video info extraction failed: HTTP Error 400` при повторе из очереди.
  Причина: сеть сервера лежала ~30 мин (141 попытка `getUpdates`, DNS `Name or service not known` к api.telegram.org);
  после восстановления повтор с cookies снова получил 400 — cookies всё ещё протухшие. Код отработал штатно: повтор без cookies
  прошёл, `mark_dead` сработал, рилс отправлен (14:04:59). Изменений кода нет. Нужен вход в Instagram в `browser`.
- **2026-11-20 17:15–19:05 UTC** — `[Instagram] <id>: Video info extraction failed: HTTP Error 400` на каждом новом рилсе
  (DfpWrUKt1r6, DftzWbaBB5w, De4JcibgPc-, Df9sJz-IBMg). Причина та же: вход в Instagram в `browser` так и не обновлён, cookies
  протухшие (`dead` с 14:04, уведомление владельцу ушло, повтор — раз в сутки). Повтор без cookies каждый раз успешен, все рилсы
  отправлены. Изменений кода нет. Ошибка будет повторяться на каждой ссылке Instagram, пока владелец не войдёт в `browser`.

## Полезное
- Если `docker logs` пуст после аварийного выключения — читать файл напрямую: `sudo cat $(docker inspect -f {{.LogPath}} link_bot-bot-1) | tr -d '\000'`.
- Проверить скачивание вручную: `docker exec link_bot-bot-1 yt-dlp -s <url>`.
- `all bot.py` — пустой файл, лежит в репозитории исторически.
