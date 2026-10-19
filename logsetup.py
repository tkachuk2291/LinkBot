import asyncio
import contextvars
import json
import logging
import os
import sys
import threading
import traceback
from datetime import datetime, timezone

_context: contextvars.ContextVar[dict] = contextvars.ContextVar("log_context", default={})
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}
_QUIET = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "asyncio": logging.WARNING,
    "PIL": logging.WARNING,
    "google_genai": logging.WARNING,
    "aiosqlite": logging.WARNING,
    "sqlalchemy.engine": logging.WARNING,
}
_TEXT_LIMIT = 200
_log = logging.getLogger("logsetup")


class JsonFormatter(logging.Formatter):
    def __init__(self, bot: str) -> None:
        super().__init__()
        self.bot = bot
        self.cwd = os.getcwd()

    def _where(self, record: logging.LogRecord) -> str:
        path = record.pathname
        if path.startswith(self.cwd + os.sep):
            path = os.path.relpath(path, self.cwd)
        return f"{path}:{record.lineno} {record.funcName}"

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "bot": self.bot,
            "logger": record.name,
            "msg": record.getMessage(),
            "where": self._where(record),
        }
        entry.update(_context.get())
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                entry[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            error = record.exc_info[1]
            entry["exc_type"] = f"{type(error).__module__}.{type(error).__qualname__}"
            entry["exc_msg"] = str(error)[:1000]
            entry["traceback"] = "".join(traceback.format_exception(*record.exc_info))
        if record.stack_info:
            entry["stack"] = record.stack_info
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(bot: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(bot))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    for name, level in _QUIET.items():
        logging.getLogger(name).setLevel(level)
    logging.captureWarnings(True)

    def excepthook(kind, error, tb):
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, error, tb)
            return
        _log.critical("Uncaught exception, process is going down", exc_info=(kind, error, tb))

    def thread_excepthook(args):
        _log.critical("Uncaught exception in thread %s", args.thread.name if args.thread else "?",
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    _log.info("Logging started", extra={"pid": os.getpid(), "python": sys.version.split()[0]})


def watch_loop() -> None:
    def handler(loop, context):
        error = context.get("exception")
        details = {k: repr(v)[:300] for k, v in context.items() if k not in ("exception", "message")}
        _log.error("Unhandled error in asyncio: %s", context.get("message", "no message"),
                   exc_info=(type(error), error, error.__traceback__) if error else None,
                   extra={"asyncio": details})

    asyncio.get_running_loop().set_exception_handler(handler)


def set_context(**values) -> None:
    clean = {k: v for k, v in values.items() if v is not None}
    _context.set({**_context.get(), **clean})


def _short(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT] + "…"


def aiogram_middleware():
    from aiogram import BaseMiddleware

    class LogContextMiddleware(BaseMiddleware):
        async def __call__(self, handler, event, data):
            inner = getattr(event, "event", None)
            message = getattr(inner, "message", None)
            chat = getattr(inner, "chat", None) or getattr(message, "chat", None)
            user = getattr(inner, "from_user", None)
            text = getattr(inner, "text", None) or getattr(inner, "caption", None) or getattr(inner, "data", None)
            _context.set({
                k: v for k, v in {
                    "update_id": getattr(event, "update_id", None),
                    "update_type": getattr(event, "event_type", None),
                    "chat_id": getattr(chat, "id", None),
                    "chat_type": getattr(chat, "type", None),
                    "user_id": getattr(user, "id", None),
                    "username": getattr(user, "username", None),
                    "text": _short(text),
                }.items() if v is not None
            })
            return await handler(event, data)

    return LogContextMiddleware()
