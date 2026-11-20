# LinkBot

Telegram bot that downloads videos from Instagram, TikTok and YouTube links and adds a short AI summary to the caption. For ordinary article links it replies with a summary only.

Works in private chats and in channels where the bot is an admin.

## Features

- **Video download.** `yt-dlp` fetches the video; `ffmpeg` re-encodes it to H.264 with `faststart` so Telegram plays video and sound.
- **Photo posts with music.** Instagram posts with only photos are turned into a video (picture + the post's music).
- **AI summaries.** Google Gemini (`gemini-flash-latest`, with fallback models) writes the summary. If every Gemini model is rate-limited or failing, the summary is produced by Claude through the Claude Code CLI (`claude -p`) running as a small host service (`claude_worker.py`, `linkbot-claude.service`).
- **Telegram limits.** Files over 50 MB (Bot API limit) are not sent; the bot replies with the summary only.
- **Network-failure queue.** If the connection drops or a download is too slow, the link is saved to `pending.json` and retried every minute for up to 24 hours.
- **Catch-up for missed posts.** After a restart the bot re-checks recent posts in connected channels and answers links it missed (`history.py`).
- **Instagram access.** Cookies are read from a Chromium profile kept by a separate `browser` container.
- **Structured logs.** JSON logs to stdout (`logsetup.py`).

## Stack

Python 3.12, aiogram 3, yt-dlp, curl_cffi, aiohttp, google-genai (Gemini), Claude Code CLI, ffmpeg, Deno (needed by yt-dlp for YouTube), Docker Compose.

## Run

1. Create a bot with @BotFather.
2. Copy `.env.example` to `.env` and fill in:

   | Variable | Purpose |
   | --- | --- |
   | `BOT_TOKEN` | Telegram bot token |
   | `GEMINI_API_KEY` | Google AI Studio key |
   | `GEMINI_MODEL` | optional, defaults to `gemini-flash-latest` |
   | `BROWSER_PASSWORD` | password for the Chromium container used to sign in to Instagram |

3. Start with Docker:

   ```bash
   docker compose up -d --build
   ```

   `docker-compose.yaml` mounts `/home/server/linkbot-data` and `/home/server/linkbot-browser`; change these paths for your host.

Running without Docker: install `ffmpeg` and Deno, then `pip install -r requirements.txt` and `python main.py`. Step-by-step Linux setup, including the Claude fallback service, is in [DEPLOY_LINUX.md](DEPLOY_LINUX.md).

## Project layout

| File | What it does |
| --- | --- |
| `main.py` | bot handlers, download, summaries, retry queue |
| `history.py` | catch-up for posts missed while the bot was offline |
| `claude_ai.py` | prepares jobs (video frames + prompt) for Claude and waits for the result |
| `claude_worker.py` | host service that runs `claude -p` for each job |
| `logsetup.py` | JSON logging |
| `KNOWLEDGE.md` | design notes and incident log |
