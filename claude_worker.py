import json
import logging
import subprocess
import sys
import time
from os import getenv
from pathlib import Path

JOBS = Path(getenv("CLAUDE_JOBS", "/home/server/linkbot-data/claude"))
CLAUDE_BIN = getenv("CLAUDE_BIN", "/home/server/.local/bin/claude")
CLAUDE_MODEL = getenv("CLAUDE_MODEL", "sonnet")
TIMEOUT = 400


def run(folder: Path) -> dict:
    job = json.loads((folder / "job.json").read_text())
    try:
        out = subprocess.run(
            [CLAUDE_BIN, "-p", job["prompt"], "--output-format", "json", "--model", CLAUDE_MODEL,
             "--allowedTools", job["tools"], "--max-turns", "15"],
            cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"is_error": True, "error": f"timeout {TIMEOUT}s"}
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"is_error": True, "error": f"exit {out.returncode}: {(out.stderr or out.stdout)[-500:]}"}


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    JOBS.mkdir(parents=True, exist_ok=True)
    logging.info("Waiting for jobs in %s", JOBS)
    while True:
        for job in sorted(JOBS.glob("*/job.json"), key=lambda p: p.stat().st_mtime):
            folder = job.parent
            if (folder / "result.json").exists():
                continue
            logging.info("Job %s started", folder.name)
            data = run(folder)
            tmp = folder / "result.tmp"
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            tmp.replace(folder / "result.json")
            logging.info("Job %s done, error=%s, %s", folder.name, data.get("is_error"),
                         str(data.get("error") or data.get("result"))[:200].replace("\n", " "))
        time.sleep(2)


if __name__ == "__main__":
    main()
