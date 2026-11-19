import asyncio
import json
import logging
from os import getenv
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Message, MessageEntity

STATE_FILE = Path(getenv("CHANNELS_FILE", "/data/channels.json"))
SCAN_CHAT = int(getenv("SCAN_CHAT_ID", "564395362"))
RESCAN = 20
MAX_GAP = 30
KEEP = 500
SCAN_PAUSE = 0.35
BOT_PREFIXES = (
    "📝 Выжимка", "⏳", "Скачиваю видео", "Не удалось", "Instagram не отдаёт", "Видео больше 50",
    "📡", "🔄", "🤖",
)
CHAT_TYPES = ("channel", "group", "supergroup")
START_DELAY = 30
_active: set[tuple[int, int]] = set()


def load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        logging.exception("Can't read %s", STATE_FILE)
        return {}


def save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(STATE_FILE)


def _chat(state: dict, chat_id: int) -> dict:
    return state.setdefault(str(chat_id), {"last": None, "done": [], "sent": []})


def _remember(items: list, message_id: int) -> None:
    if message_id not in items:
        items.append(message_id)
        del items[:-KEEP]


def is_known(chat_id: int) -> bool:
    return load().get(str(chat_id), {}).get("last") is not None


def note_post(chat_id: int, message_id: int) -> None:
    state = load()
    chat = _chat(state, chat_id)
    chat["last"] = max(chat["last"] or 0, message_id)
    save(state)


def mark_done(chat_id: int, message_id: int) -> None:
    state = load()
    chat = _chat(state, chat_id)
    _remember(chat["done"], message_id)
    chat["last"] = max(chat["last"] or 0, message_id)
    save(state)


def claim(chat_id: int, message_id: int) -> bool:
    key = (chat_id, message_id)
    if key in _active or message_id in _chat(load(), chat_id)["done"]:
        return False
    _active.add(key)
    return True


def release(chat_id: int, message_id: int) -> None:
    mark_done(chat_id, message_id)
    _active.discard((chat_id, message_id))


def note_sent(chat_id: int, message_id: int) -> None:
    state = load()
    chat = _chat(state, chat_id)
    _remember(chat["sent"], message_id)
    chat["last"] = max(chat["last"] or 0, message_id)
    save(state)


class RememberSent(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        if isinstance(result, Message) and result.chat.type in CHAT_TYPES:
            note_sent(result.chat.id, result.message_id)
        return result


def find_urls(text: str | None, entities: list[MessageEntity] | None) -> list[str]:
    urls = []
    for entity in entities or []:
        if entity.type == "text_link":
            url = entity.url
        elif entity.type == "url":
            url = entity.extract_from(text or "")
        else:
            continue
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if url not in urls:
            urls.append(url)
    return urls


async def _read(bot: Bot, chat_id: int, message_id: int) -> Message | None:
    while True:
        try:
            copy = await bot.forward_message(SCAN_CHAT, chat_id, message_id, disable_notification=True)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            continue
        except TelegramBadRequest:
            return None
        try:
            await bot.delete_message(SCAN_CHAT, copy.message_id)
        except TelegramBadRequest:
            pass
        return copy


def _answered(posts: list[tuple[int, str, list[str]]]) -> list[tuple[int, list[str]]]:
    found = []
    run = []
    for message_id, kind, urls in posts + [(0, "other", [])]:
        if kind == "links":
            run.append((message_id, urls))
            continue
        if kind == "answer" and run:
            run.pop(0)
            continue
        if kind == "bot":
            continue
        found.extend(run)
        run = []
    return found


def _kind(text: str, media: bool) -> str:
    if text.startswith(("📝 Выжимка", "Не удалось", "Instagram не отдаёт", "Видео больше 50", "📡")):
        return "answer"
    if media and text.startswith("⏳"):
        return "answer"
    if text.startswith(BOT_PREFIXES):
        return "bot"
    return "other"


async def scan_chat(bot: Bot, chat_id: int) -> list[tuple[int, list[str]]]:
    chat = _chat(load(), chat_id)
    if chat["last"] is None:
        return []
    skip = set(chat["done"])
    sent = set(chat["sent"])
    last = chat["last"]
    posts = []
    message_id = max(1, last - RESCAN + 1)
    gap = 0
    while gap < MAX_GAP:
        if message_id in skip or (chat_id, message_id) in _active:
            message_id += 1
            continue
        copy = None if message_id in sent else await _read(bot, chat_id, message_id)
        if message_id not in sent:
            await asyncio.sleep(SCAN_PAUSE)
        if copy is None and message_id not in sent:
            if message_id > last:
                gap += 1
            message_id += 1
            continue
        gap = 0
        if copy is None:
            posts.append((message_id, "bot", []))
            message_id += 1
            continue
        text = copy.text or copy.caption or ""
        kind = _kind(text, bool(copy.video or copy.photo))
        urls = find_urls(text, copy.entities or copy.caption_entities) if kind == "other" else []
        posts.append((message_id, "links" if urls else kind, urls))
        message_id += 1
    found = _answered(posts)
    todo = {m for m, _ in found}
    for message_id, _, _ in posts:
        if message_id not in todo:
            mark_done(chat_id, message_id)
    return found


def register(chat_id: int, from_id: int) -> None:
    state = load()
    chat = _chat(state, chat_id)
    chat["last"] = from_id - 1 if chat["last"] is None else min(chat["last"], from_id - 1)
    chat["done"] = [m for m in chat["done"] if m < from_id]
    save(state)


async def catch_up(bot: Bot, handle, delay: float = START_DELAY, only: int | None = None) -> None:
    await asyncio.sleep(delay)
    for key in list(load()):
        if only is not None and int(key) != only:
            continue
        chat_id = int(key)
        try:
            found = await scan_chat(bot, chat_id)
        except TelegramForbiddenError as e:
            logging.warning("Catch-up skipped chat %s: %s", chat_id, e.message)
            continue
        if found:
            logging.info("Catch-up: %d missed posts with links in chat %s", len(found), chat_id,
                         extra={"chat_id": chat_id, "missed": [m for m, _ in found]})
        for message_id, urls in found:
            if not claim(chat_id, message_id):
                continue
            try:
                await handle(bot, chat_id, message_id, urls)
            finally:
                release(chat_id, message_id)
