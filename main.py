import yt_dlp
from yt_dlp.extractor.instagram import _id_to_pk

import asyncio
import html
import json
import logging
import re
import subprocess
import sys
import tempfile
from os import getenv
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from google import genai
from google.genai import errors, types

# Боти в Telegram можуть надсилати файли до 50 МБ
MAX_SIZE = 50 * 1024 * 1024
# Максимальна довжина повідомлення і підпису до відео в Telegram
MAX_TEXT = 4096
MAX_CAPTION = 1024

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
        # Кодек підходить, лише переносимо індекс на початок файлу, щоб відео одразу стрімилось
        args = ["-c", "copy"]
    else:
        logging.info("Re-encode %s -> h264", info["codec"])
        args = [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            # не більше 720 по короткій стороні, розміри мають бути парними
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


def get_media(url: str, source: str, folder: str) -> tuple[Path, str, dict | None]:
    """Качає відео з поста. Для поста лише з фото повертає картинку і None замість даних відео."""
    ydl_opts = {
        'format': 'bv*+ba/b',
        # спершу H.264 (його показує Telegram), далі не більше 720p по короткій стороні
        'format_sort': ['vcodec:h264', 'res:720', 'acodec:aac'],
        'merge_output_format': 'mp4',
        'outtmpl': f'{folder}/%(id)s.%(ext)s',
        'noplaylist': True,
        'quiet': True,
        'noprogress': True,
        'logger': YdlLogger(),
        'remote_components': ['ejs:github'],
        # пост з фото в Instagram — не помилка, тоді візьмемо картинку
        'ignore_no_formats_error': True,
    }
    if source == "instagram":
        cookies_file = getenv("COOKIES_FILE")
        if cookies_file and Path(cookies_file).is_file():
            ydl_opts['cookiefile'] = cookies_file
        elif not cookies_file:
            ydl_opts['cookiesfrombrowser'] = ('chrome',)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        # Карусель приходить плейлистом: беремо перше відео з неї, а якщо відео нема — перше фото
        entries = info.get('entries') or [info]
        videos = [entry for entry in entries if entry.get('formats')]
        if videos:
            ydl.process_ie_result(videos[0], download=True)
            music = None
        else:
            image_url = next((entry['thumbnail'] for entry in entries if entry.get('thumbnail')), None)
            if image_url is None:
                raise RuntimeError("В посте нет ни видео, ни фото")
            image = download(ydl, image_url, Path(folder) / "image")
            music = get_instagram_music(ydl, url, folder) if source == "instagram" else None

    # Назва, опис і автор допомагають Gemini впізнати, що це за відео
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
         # не ширше 1080, розміри мають бути парними
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
    # Gemini сам відкриває сторінку за посиланням
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

    try:
        summary = await summary_coro
    except Exception as e:
        logging.exception("Summary failed")
        await edit(**{field: f"Не удалось сделать выжимку: {e}"[:limit]})
        return

    body = f"📝 <b>Выжимка</b>\n\n<blockquote>{summary.strip()}</blockquote>"
    if len(body) <= limit:
        try:
            await edit(**{field: body}, parse_mode="HTML")
            return
        except TelegramBadRequest:
            logging.exception("Bad HTML from Gemini, sending plain text")

    plain = html.unescape(re.sub(r"<[^>]+>", "", body))
    await edit(**{field: plain[:limit]})


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


@dp.channel_post(F.text)
@dp.message(F.text, F.chat.type == "private")
async def link_handler(message: Message) -> None:
    # Без нормального посилання мовчки ігноруємо
    url = find_url(message)
    if url is None:
        return

    source = detect_source(url)
    if source is None:
        if not await is_reachable(url):
            logging.info("Skip unreachable link %s", url)
            return
        status = await message.reply("⏳ Делаю выжимку...")
        await show_summary(status, summarize_url(url))
        return

    if source == "instagram" and not INSTAGRAM_POST_RE.search(url):
        return

    status = await message.reply("Скачиваю видео...")

    with tempfile.TemporaryDirectory() as folder:
        try:
            path, meta, video = await asyncio.to_thread(get_media, url, source, folder)
        except Exception as e:
            logging.exception("Download failed")
            if "login" in str(e).lower() or "empty media response" in str(e):
                await status.edit_text("Instagram не отдаёт этот пост: он закрытый, удалён или нужен вход в аккаунт.")
            else:
                await status.edit_text("Не удалось скачать видео по этой ссылке.")
            return

        # Спочатку відразу віддаємо відео чи фото, вижимку допишемо в підпис, коли буде готова
        if video is None:
            target = await message.reply_photo(FSInputFile(path), caption="⏳ Делаю выжимку...")
            await status.delete()
        elif path.stat().st_size > MAX_SIZE:
            target = status
            await target.edit_text(
                "Видео больше 50 МБ, Telegram не даёт боту его отправить.\n"
                "⏳ Делаю выжимку..."
            )
        else:
            target = await message.reply_video(
                FSInputFile(path), caption="⏳ Делаю выжимку...", supports_streaming=True,
                width=video["width"], height=video["height"], duration=video["duration"],
            )
            await status.delete()

        await show_summary(target, summarize_media(path, meta))


async def main() -> None:
    bot = Bot(token=TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
