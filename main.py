import yt_dlp
from yt_dlp.extractor.instagram import _id_to_pk

import asyncio
import html
import json
import logging
import re
import subprocess
import tempfile
import time
from os import getenv
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from dotenv import load_dotenv

from logsetup import aiogram_middleware, setup_logging, watch_loop

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message, ReplyParameters
from google import genai
from google.genai import errors, types

MAX_SIZE = 50 * 1024 * 1024
MAX_TEXT = 4096
MAX_CAPTION = 1024

PENDING_FILE = Path(getenv("PENDING_FILE", "/data/pending.json"))
RETRY_EVERY = 60
GIVE_UP_AFTER = 24 * 3600
NETWORK_MARKERS = (
    "timed out", "timeout", "could not resolve", "name resolution", "connection reset",
    "connection refused", "connection aborted", "network is unreachable", "operation too slow",
    "failed to establish", "connect call failed", "cannot connect to host", "connection broken", "broken pipe",
    "curl: (6)", "curl: (7)", "curl: (28)", "curl: (35)", "curl: (56)",
)

INSTAGRAM_POST_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv)/")

SOURCES = {
    "instagram": ("instagram.com",),
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
}

SUMMARY_PROMPT = """\
Ты делаешь выжимку для Telegram-канала. Пиши на русском, даже если оригинал на другом языке.

1. Сначала определи, ЧТО это: фрагмент сериала, фильма, мультфильма, аниме, шоу, игра, \
музыка/клип, обзор товара, рецепт, лайфхак, обучение, новость, юмор, блог и т.п. \
Если это отрывок из известного произведения — узнай, из какого именно \
(по кадрам, актёрам, персонажам, названию и описанию ролика), и проверь через поиск.

2. Структура ответа:
<b>ЭМОДЗИ Название</b> — эмодзи по типу (🎬 фильм, 📺 сериал, 🎮 игра, 🎵 музыка, \
🍳 рецепт, 💡 лайфхак, 📰 новость, 🛍 товар, 😂 юмор, 📚 обучение). Для произведений — \
название на русском и в скобках оригинальное; для остального — короткий заголовок сути.
<i>Тип · жанр</i>
1–2 предложения: о чём само произведение/материал и что происходит в этом ролике.

Затем полезные факты пунктами «• <b>Поле:</b> значение», только подходящие по контексту:
- фильм/сериал/аниме: год, сезон и серия (если можно определить), жанр, режиссёр, \
главные актёры, рейтинг IMDb/Кинопоиск, где посмотреть;
- игра: год, разработчик, платформы, рейтинг (Metacritic/Steam);
- музыка: исполнитель, трек, альбом, год;
- рецепт: ингредиенты, время, основные шаги;
- лайфхак/обучение: сами шаги или советы, чтобы можно было применить без просмотра;
- товар: название, модель, примерная цена, плюсы и минусы;
- новость: кто, что, где, когда, почему важно.
Можно закончить строкой «💬 <i>…</i>» — главный вывод или почему стоит посмотреть.

3. Правила:
- Факты (рейтинги, годы, сезоны, актёры) — только если уверен или проверил через поиск. \
Не знаешь — пропусти поле, не выдумывай и не пиши «неизвестно».
- Формат — Telegram HTML, разрешены только теги <b>, <i>, <u>, <code>. \
Символы <, >, & вне тегов пиши как &lt; &gt; &amp;. Без Markdown, без вступления и ссылок-сносок.
- Всего не больше {max_chars} символов.
"""


def detect_source(url: str) -> str | None:
    for source, domains in SOURCES.items():
        if any(domain in url for domain in domains):
            return source
    return None


def probe(path: Path) -> dict:
    """Кодек, розміри і тривалість відео через ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt,width,height:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    stream = data["streams"][0]
    return {
        "codec": stream.get("codec_name"),
        "pix_fmt": stream.get("pix_fmt"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "duration": round(float(data.get("format", {}).get("duration", 0))),
    }


def make_telegram_friendly(path: Path) -> Path:
    """Telegram нормально показує лише H.264 — інакше (VP9, AV1, HEVC) грає тільки звук."""
    info = probe(path)
    out = path.with_name(f"tg_{path.name}")
    if info["codec"] == "h264" and info["pix_fmt"] == "yuv420p":
        args = ["-c", "copy"]
    else:
        logging.info("Re-encode %s -> h264", info["codec"])
        args = [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-vf", "scale='if(gt(iw,ih),-2,min(720,iw))':'if(gt(iw,ih),min(720,ih),-2)'",
            "-c:a", "aac", "-b:a", "128k",
        ]
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(path), *args, "-movflags", "+faststart", str(out)],
        check=True,
    )
    return out


class YdlLogger:
    """Передає попередження yt-dlp у logging, крім очікуваних для постів з фото."""
    EXPECTED = ("There is no video in this post", "No video formats found", "Requested format is not available")

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        if not any(text in msg for text in self.EXPECTED):
            logging.warning(msg)

    def error(self, msg: str) -> None:
        logging.error(msg)


def fetch(ydl_opts: dict, url: str, source: str, folder: str) -> tuple[dict, list, Path | None, tuple | None]:
    """Качає відео, а для поста з фото — картинку і музику до неї."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        entries = info.get('entries') or [info]
        videos = [entry for entry in entries if entry.get('formats')]
        if videos:
            ydl.process_ie_result(videos[0], download=True)
            image = music = None
        else:
            image_url = next((entry['thumbnail'] for entry in entries if entry.get('thumbnail')), None)
            if image_url is None:
                raise RuntimeError("В посте нет ни видео, ни фото")
            image = download(ydl, image_url, Path(folder) / "image")
            music = get_instagram_music(ydl, url, folder) if source == "instagram" else None
    return info, videos, image, music


def get_media(url: str, source: str, folder: str) -> tuple[Path, str, dict | None]:
    """Качає відео з поста. Для поста лише з фото повертає картинку і None замість даних відео."""
    ydl_opts = {
        'format': 'bv*+ba/b',
        'format_sort': ['vcodec:h264', 'res:720', 'acodec:aac'],
        'merge_output_format': 'mp4',
        'outtmpl': f'{folder}/%(id)s.%(ext)s',
        'noplaylist': True,
        'quiet': True,
        'noprogress': True,
        'logger': YdlLogger(),
        'remote_components': ['ejs:github'],
        'ignore_no_formats_error': True,
    }
    if source == "instagram":
        browser_profile = getenv("BROWSER_PROFILE")
        if browser_profile:
            ydl_opts['cookiesfrombrowser'] = ('chromium', browser_profile, 'BASICTEXT', None)
        else:
            ydl_opts['cookiesfrombrowser'] = ('chrome',)

    try:
        info, videos, image, music = fetch(ydl_opts, url, source, folder)
    except Exception:
        if 'cookiesfrombrowser' not in ydl_opts:
            raise
        logging.warning("Download with cookies failed, retrying without them")
        ydl_opts.pop('cookiesfrombrowser', None)
        info, videos, image, music = fetch(ydl_opts, url, source, folder)

    meta = "\n".join(
        f"{label}: {info[key]}"
        for label, key in (("Название", "title"), ("Автор", "uploader"), ("Описание", "description"))
        if info.get(key)
    )
    if videos:
        path = make_telegram_friendly(next(Path(folder).glob("*.mp4")))
    elif music:
        audio, start, duration, track = music
        meta += f"\nМузыка в посте: {track}"
        path = photo_with_music(image, audio, start, duration)
    else:
        return image, meta, None
    return path, meta, probe(path)


def download(ydl: yt_dlp.YoutubeDL, url: str, stem: Path) -> Path:
    """Качає файл з кукі yt-dlp, розширення бере з Content-Type."""
    with ydl.urlopen(url) as response:
        content_type = response.headers.get("Content-Type", "")
        ext = next((ext for ext in ("png", "webp", "mp4", "m4a") if ext in content_type), "jpg")
        path = stem.with_suffix(f".{ext}")
        path.write_bytes(response.read())
    return path


def get_instagram_music(ydl: yt_dlp.YoutubeDL, url: str, folder: str) -> tuple[Path, float, float, str] | None:
    """Музика до фото-поста: файл, з якої секунди грає, скільки і назва треку.
    Instagram віддає її тільки через API для залогіненого акаунта."""
    shortcode = re.search(r"/(?:p|reels?|tv)/([\w-]+)", url)
    if shortcode is None:
        return None
    ie = ydl.get_info_extractor("Instagram")
    data = ie._download_json(
        f"https://i.instagram.com/api/v1/media/{_id_to_pk(shortcode[1])}/info/", shortcode[1],
        headers=ie._api_headers, fatal=False,
    )
    music = ((data or {}).get("items") or [{}])[0].get("music_metadata") or {}
    info = music.get("music_info") or {}
    asset = info.get("music_asset_info") or {}
    audio_url = asset.get("progressive_download_url") or asset.get("fast_start_progressive_download_url")
    if not audio_url:
        return None
    timing = info.get("music_consumption_info") or {}
    start = (timing.get("audio_asset_start_time_in_ms") or 0) / 1000
    duration = (timing.get("overlap_duration_in_ms") or 30000) / 1000
    track = " — ".join(filter(None, (asset.get("display_artist"), asset.get("title"))))
    return download(ydl, audio_url, Path(folder) / "music"), start, duration, track


def photo_with_music(image: Path, audio: Path, start: float, duration: float) -> Path:
    """Склеює фото і шматок треку у відео, як його показує Instagram."""
    out = image.with_name("photo_music.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-loop", "1", "-framerate", "1", "-i", str(image),
         "-ss", str(start), "-t", str(duration), "-i", str(audio),
         "-map", "0:v", "-map", "1:a", "-shortest", "-t", str(duration),
         "-c:v", "libx264", "-tune", "stillimage", "-preset", "veryfast", "-r", "25",
         "-vf", "scale='trunc(min(1080,iw)/2)*2':-2:out_range=tv,format=yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
        check=True,
    )
    return out


load_dotenv()
TOKEN = getenv("BOT_TOKEN")
GEMINI_MODEL = getenv("GEMINI_MODEL", "gemini-flash-latest")
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-flash-lite-latest"]

gemini = genai.Client(api_key=getenv("GEMINI_API_KEY"))
dp = Dispatcher()


async def generate(contents, tools=None) -> str:
    config = types.GenerateContentConfig(
        tools=tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    last_error = None
    for model in [GEMINI_MODEL, *FALLBACK_MODELS]:
        for attempt in range(2):
            try:
                response = await gemini.aio.models.generate_content(
                    model=model, contents=contents, config=config,
                )
                return response.text
            except errors.APIError as e:
                if e.code not in (429, 500, 503):
                    raise
                logging.warning("Gemini %s busy (%s), attempt %d", model, e.code, attempt + 1)
                last_error = e
                if e.code == 429:
                    break
                await asyncio.sleep(5)
    raise last_error


SEARCH = types.Tool(google_search=types.GoogleSearch())


async def summarize_media(path: Path, meta: str) -> str:
    file = await gemini.aio.files.upload(file=path)
    try:
        while file.state == types.FileState.PROCESSING:
            await asyncio.sleep(2)
            file = await gemini.aio.files.get(name=file.name)
        if file.state == types.FileState.FAILED:
            raise RuntimeError("Gemini не смог обработать файл")

        prompt = SUMMARY_PROMPT.format(max_chars=MAX_CAPTION - 200)
        return await generate(
            [file, f"{prompt}\nДанные поста с сайта:\n{meta[:3000]}"],
            tools=[SEARCH],
        )
    finally:
        await gemini.aio.files.delete(name=file.name)


async def summarize_url(url: str) -> str:
    prompt = SUMMARY_PROMPT.format(max_chars=MAX_TEXT - 500)
    return await generate(
        f"{prompt}\n\n{url}",
        tools=[types.Tool(url_context=types.UrlContext()), SEARCH],
    )


async def show_summary(target: Message, summary_coro) -> None:
    """Дописує вижимку в підпис до відео чи фото або в текст повідомлення."""
    if target.video or target.photo:
        edit, field, limit = target.edit_caption, "caption", MAX_CAPTION
    else:
        edit, field, limit = target.edit_text, "text", MAX_TEXT

    async def safe_edit(value: str) -> None:
        try:
            await edit(**{field: value[:limit]})
        except TelegramBadRequest as e:
            logging.warning("Can't edit summary message: %s", e.message)

    try:
        summary = await summary_coro
    except Exception as e:
        logging.exception("Summary failed")
        await safe_edit(f"Не удалось сделать выжимку: {e}")
        return

    body = f"📝 <b>Выжимка</b>\n\n<blockquote>{summary.strip()}</blockquote>"
    if len(body) <= limit:
        try:
            await edit(**{field: body}, parse_mode="HTML")
            return
        except TelegramBadRequest as e:
            logging.warning("HTML edit failed (%s), sending plain text", e.message)

    await safe_edit(html.unescape(re.sub(r"<[^>]+>", "", body)))


@dp.message(Command("start"))
async def command_start_handler(message: Message) -> None:
    await message.answer(
        "Пришли ссылку на видео из Instagram, TikTok или YouTube, "
        "или на любую статью, и я сделаю выжимку."
    )


def find_url(message: Message) -> str | None:
    """Бере посилання з розмітки Telegram — він сам розпізнає справжні лінки."""
    for entity in message.entities or []:
        if entity.type == "text_link":
            url = entity.url
        elif entity.type == "url":
            url = entity.extract_from(message.text)
        else:
            continue
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        host = urlparse(url).hostname or ""
        if "." in host:
            return url
    return None


async def is_reachable(url: str) -> bool:
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                return response.status not in (404, 410) and response.status < 500
    except Exception:
        return False


async def delete_quietly(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest as e:
        logging.warning("Can't delete status message: %s", e.message)


def stat(result: str, source: str | None) -> None:
    logging.info("Link %s", result, extra={"event": "stat", "stat": "links", "result": result, "source": source or "web"})


def is_network_error(error: BaseException | None) -> bool:
    """Сбой сети, а не проблема с самой ссылкой: смотрим всю цепочку исключений."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (TimeoutError, ConnectionError, aiohttp.ClientConnectionError, TelegramNetworkError)):
            return True
        if any(marker in str(error).lower() for marker in NETWORK_MARKERS):
            return True
        error = error.__cause__ or error.__context__
    return False


async def internet_ok() -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            for url in ("https://api.telegram.org", "https://www.google.com"):
                async with session.head(url, timeout=aiohttp.ClientTimeout(total=10)):
                    pass
        return True
    except Exception:
        return False


def load_pending() -> list[dict]:
    try:
        return json.loads(PENDING_FILE.read_text())
    except FileNotFoundError:
        return []
    except Exception:
        logging.exception("Can't read %s", PENDING_FILE)
        return []


def save_pending(items: list[dict]) -> None:
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    tmp.replace(PENDING_FILE)


def add_pending(chat_id: int, message_id: int, url: str, status_id: int | None) -> None:
    items = [i for i in load_pending() if (i["chat_id"], i["message_id"]) != (chat_id, message_id)]
    items.append({"chat_id": chat_id, "message_id": message_id, "url": url,
                  "status_id": status_id, "added": time.time()})
    save_pending(items)


def drop_pending(chat_id: int, message_id: int) -> None:
    save_pending([i for i in load_pending() if (i["chat_id"], i["message_id"]) != (chat_id, message_id)])


async def process_link(bot: Bot, chat_id: int, message_id: int, url: str, source: str, status: Message) -> None:
    """Качает видео по ссылке и отвечает им на исходное сообщение.
    Сбой сети пробрасывает наружу, чтобы ссылку поставили в очередь."""
    reply_to = ReplyParameters(message_id=message_id, allow_sending_without_reply=True)

    with tempfile.TemporaryDirectory() as folder:
        try:
            path, meta, video = await asyncio.to_thread(get_media, url, source, folder)
        except Exception as e:
            if is_network_error(e) or not await internet_ok():
                raise
            logging.exception("Download failed")
            if "login" in str(e).lower() or "empty media response" in str(e):
                stat("private", source)
                await status.edit_text("Instagram не отдаёт этот пост: он закрытый, удалён или нужен вход в аккаунт.")
            else:
                stat("failed", source)
                await status.edit_text("Не удалось скачать видео по этой ссылке.")
            return

        if video is None:
            target = await bot.send_photo(chat_id, FSInputFile(path), caption="⏳ Делаю выжимку...",
                                          reply_parameters=reply_to)
            await delete_quietly(status)
            stat("photo", source)
        elif path.stat().st_size > MAX_SIZE:
            stat("too_big", source)
            target = status
            await target.edit_text(
                "Видео больше 50 МБ, Telegram не даёт боту его отправить.\n"
                "⏳ Делаю выжимку..."
            )
        else:
            target = await bot.send_video(
                chat_id, FSInputFile(path), caption="⏳ Делаю выжимку...", supports_streaming=True,
                width=video["width"], height=video["height"], duration=video["duration"],
                reply_parameters=reply_to,
            )
            await delete_quietly(status)
            stat("video", source)

        try:
            await show_summary(target, summarize_media(path, meta))
        except Exception:
            logging.exception("Summary failed after video was sent")


async def queue_link(chat_id: int, message_id: int, url: str, status: Message | None) -> None:
    logging.warning("No network, queued %s", url)
    stat("queued", detect_source(url))
    add_pending(chat_id, message_id, url, status.message_id if status else None)
    if status:
        try:
            await status.edit_text("📡 Сейчас нет связи. Скачаю видео сам, как только сеть вернётся.")
        except Exception:
            pass


async def retry_pending(bot: Bot) -> None:
    """Раз в минуту проверяет сеть и докачивает ссылки из очереди, отвечая на исходные сообщения."""
    while True:
        try:
            items = load_pending()
            if items and await internet_ok():
                for item in items:
                    await retry_one(bot, item)
        except Exception as e:
            if not is_network_error(e):
                logging.exception("Retry loop failed")
        await asyncio.sleep(RETRY_EVERY)


async def retry_one(bot: Bot, item: dict) -> None:
    chat_id, message_id, url = item["chat_id"], item["message_id"], item["url"]
    status_id = item.get("status_id")

    if time.time() - item["added"] > GIVE_UP_AFTER:
        drop_pending(chat_id, message_id)
        if status_id:
            try:
                await bot.edit_message_text("Не удалось скачать видео: связи не было больше суток.",
                                            chat_id=chat_id, message_id=status_id)
            except Exception:
                pass
        return

    text = "🔄 Связь вернулась, скачиваю видео..."
    status = None
    if status_id:
        try:
            status = await bot.edit_message_text(text, chat_id=chat_id, message_id=status_id)
        except Exception:
            status = None
    if status is None:
        status = await bot.send_message(
            chat_id, text, reply_parameters=ReplyParameters(message_id=message_id, allow_sending_without_reply=True),
        )

    logging.info("Retrying queued link %s", url)
    try:
        await process_link(bot, chat_id, message_id, url, detect_source(url), status)
    except Exception as e:
        if is_network_error(e) or not await internet_ok():
            items = load_pending()
            for i in items:
                if (i["chat_id"], i["message_id"]) == (chat_id, message_id):
                    i["status_id"] = status.message_id
            save_pending(items)
            try:
                await status.edit_text("📡 Связь опять пропала. Попробую ещё раз позже.")
            except Exception:
                pass
            raise
        logging.exception("Retry of %s failed", url)
        try:
            await status.edit_text("Не удалось скачать видео по этой ссылке.")
        except Exception:
            pass
    drop_pending(chat_id, message_id)


@dp.channel_post(F.text)
@dp.message(F.text, F.chat.type == "private")
async def link_handler(message: Message) -> None:
    url = find_url(message)
    if url is None:
        return

    source = detect_source(url)
    if source is None:
        if not await is_reachable(url):
            logging.info("Skip unreachable link %s", url)
            return
        status = await message.reply("⏳ Делаю выжимку...")
        stat("summary", None)
        await show_summary(status, summarize_url(url))
        return

    if source == "instagram" and not INSTAGRAM_POST_RE.search(url):
        return

    status = None
    try:
        status = await message.reply("Скачиваю видео...")
        await process_link(message.bot, message.chat.id, message.message_id, url, source, status)
    except Exception as e:
        if not (is_network_error(e) or not await internet_ok()):
            raise
        await queue_link(message.chat.id, message.message_id, url, status)


async def main() -> None:
    watch_loop()
    dp.update.outer_middleware(aiogram_middleware())
    bot = Bot(token=TOKEN)
    retry_task = asyncio.create_task(retry_pending(bot))
    try:
        await dp.start_polling(bot)
    finally:
        retry_task.cancel()


if __name__ == "__main__":
    setup_logging("link-bot")
    asyncio.run(main())
