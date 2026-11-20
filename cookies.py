import asyncio
import logging
import time
from os import getenv
from pathlib import Path

from aiogram import Bot

PROFILE = getenv("BROWSER_PROFILE")
BROWSER_URL = getenv("BROWSER_URL", "")
OWNER_CHAT = int(getenv("SCAN_CHAT_ID", "0"))
CHECK_EVERY = 60
RECHECK_PAUSE = 300
REMIND_EVERY = 24 * 3600

_state = {"dead": False, "url": None, "notified": 0.0, "mtime": None, "checked": 0.0, "revived": False}


def options() -> tuple:
    if PROFILE:
        return ('chromium', PROFILE, 'BASICTEXT', None)
    return ('chrome',)


def _mtime() -> float | None:
    if not PROFILE:
        return None
    try:
        return (Path(PROFILE) / "Cookies").stat().st_mtime
    except OSError:
        return None


def mark_dead(url: str) -> None:
    if _state["dead"]:
        return
    logging.warning("Instagram cookies are dead: request with cookies failed, without them worked",
                    extra={"url": url})
    _state.update(dead=True, url=url, notified=0.0, mtime=_mtime(), checked=0.0, revived=False)


def mark_alive() -> None:
    if not _state["dead"]:
        return
    logging.info("Instagram cookies work again")
    _state.update(dead=False, url=None, revived=True)


def _login_text() -> str:
    where = f"\n{BROWSER_URL}" if BROWSER_URL else "\n(адрес браузера не задан: переменная BROWSER_URL)"
    return (
        "🔑 Instagram разлогинил бота: с cookies он отвечает отказом, закрытые посты не скачаются.\n"
        "Зайди в Instagram в браузере бота (логин и пароль браузера — BROWSER_USER и BROWSER_PASSWORD из stack.env):"
        f"{where}\n"
        "После входа я сам проверю cookies и напишу."
    )


async def _send(bot: Bot, text: str) -> bool:
    try:
        await bot.send_message(OWNER_CHAT, text, disable_web_page_preview=True)
        return True
    except Exception as e:
        logging.warning("Can't send cookies notice: %s", e)
        return False


async def _recheck(check) -> None:
    mtime = _mtime()
    if mtime == _state["mtime"] or time.time() - _state["checked"] < RECHECK_PAUSE:
        return
    _state["checked"] = time.time()
    try:
        ok = await asyncio.to_thread(check, _state["url"])
    except Exception as e:
        logging.warning("Cookies check skipped: %s", e)
        return
    _state["mtime"] = mtime
    if ok:
        mark_alive()


async def watch(bot: Bot, check) -> None:
    while True:
        try:
            if _state["dead"]:
                await _recheck(check)
            if _state["dead"] and time.time() - _state["notified"] >= REMIND_EVERY:
                if await _send(bot, _login_text()):
                    _state["notified"] = time.time()
            if _state["revived"] and await _send(bot, "✅ Вход в Instagram сохранён, cookies снова работают."):
                _state["revived"] = False
        except Exception:
            logging.exception("Cookies watch failed")
        await asyncio.sleep(CHECK_EVERY)
