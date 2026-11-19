import asyncio
import json
import logging
import shutil
import subprocess
import time
import uuid
from os import getenv
from pathlib import Path

JOBS = Path(getenv("CLAUDE_JOBS", "/data/claude"))
CLAUDE_TIMEOUT = 420
FRAMES = 8


class ClaudeError(RuntimeError):
    pass


def extract_frames(path: Path, folder: Path, duration: float | None) -> list[str]:
    if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        shutil.copy(path, folder / f"frame_01{path.suffix.lower()}")
    else:
        rate = f"{FRAMES}/{max(duration or 0, 1):.0f}"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vf", f"fps={rate},scale=640:-2",
             "-frames:v", str(FRAMES), str(folder / "frame_%02d.jpg")],
            check=True,
        )
    return sorted(p.name for p in folder.glob("frame_*"))


def new_job() -> Path:
    folder = JOBS / uuid.uuid4().hex
    folder.mkdir(parents=True)
    folder.chmod(0o777)
    return folder


async def run_job(folder: Path, prompt: str, tools: str) -> str:
    for file in folder.iterdir():
        file.chmod(0o666)
    job = folder / "job.json"
    job.with_suffix(".tmp").write_text(json.dumps({"prompt": prompt, "tools": tools}, ensure_ascii=False))
    job.with_suffix(".tmp").replace(job)
    result = folder / "result.json"
    deadline = time.monotonic() + CLAUDE_TIMEOUT
    try:
        while not result.exists():
            if time.monotonic() > deadline:
                raise ClaudeError(f"Claude не ответил за {CLAUDE_TIMEOUT} с: запущен ли linkbot-claude на сервере?")
            await asyncio.sleep(2)
        data = json.loads(result.read_text())
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if data.get("is_error") or not data.get("result"):
        raise ClaudeError(f"Claude error: {str(data.get('result') or data.get('error') or data.get('subtype'))[:500]}")
    logging.info("Claude summary done", extra={"event": "claude", "duration_ms": data.get("duration_ms")})
    return data["result"]


async def summarize_media(path: Path, meta: str, prompt: str, duration: float | None) -> str:
    folder = new_job()
    frames = await asyncio.to_thread(extract_frames, path, folder, duration)
    task = (
        f"{prompt}\n"
        "Видео посмотреть нельзя, поэтому открой инструментом Read кадры из него по порядку "
        "(они лежат в текущей папке; звука нет, опирайся на кадры и данные поста):\n"
        + "\n".join(frames)
        + f"\n\nДанные поста с сайта:\n{meta[:3000]}\n\n"
        "В ответе выведи только готовую выжимку, без пояснений о том, как ты её делал."
    )
    return await run_job(folder, task, "Read,WebSearch,WebFetch")


async def summarize_url(url: str, prompt: str) -> str:
    task = (
        f"{prompt}\n\nОткрой ссылку через WebFetch и сделай выжимку: {url}\n"
        "В ответе выведи только готовую выжимку, без пояснений о том, как ты её делал."
    )
    return await run_job(new_job(), task, "WebSearch,WebFetch")
