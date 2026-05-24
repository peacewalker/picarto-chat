import asyncio
import json
import os
import re
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from picabot import PicaBot, PicaMessage


PICARTO_BOT_USERNAME = os.getenv("PICARTO_BOT_USERNAME", "").strip()
PICARTO_BOT_PASSWORD = os.getenv("PICARTO_BOT_PASSWORD", "").strip()
PICARTO_CHANNEL = os.getenv("PICARTO_CHANNEL", "").strip()

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

if not PICARTO_BOT_USERNAME:
    raise RuntimeError("Missing environment variable: PICARTO_BOT_USERNAME")

if not PICARTO_BOT_PASSWORD:
    raise RuntimeError("Missing environment variable: PICARTO_BOT_PASSWORD")

if not PICARTO_CHANNEL:
    raise RuntimeError("Missing environment variable: PICARTO_CHANNEL")


stop_event = asyncio.Event()
current_log_file: Optional[Path] = None
current_session_started_at: Optional[str] = None


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def filename_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def safe_name(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value)
    return value or "unknown"


def channel_log_dir() -> Path:
    path = DATA_DIR / safe_name(PICARTO_CHANNEL)
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_session_file() -> Path:
    return channel_log_dir() / f"{filename_timestamp()}_{safe_name(PICARTO_CHANNEL)}.jsonl"


def write_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


def picarto_channel_info(channel_name: str) -> dict[str, Any]:
    url = f"https://api.picarto.tv/api/v1/channel/name/{channel_name}"
    response = requests.get(
        url,
        headers={
            "User-Agent": "picarto-chatlog-recorder/1.0",
            "Accept": "application/json",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def is_channel_online(channel_name: str) -> bool:
    data = picarto_channel_info(channel_name)

    for key in ("online", "is_online", "streaming", "live"):
        if key in data:
            return bool(data[key])

    status = str(data.get("status", "")).lower()
    if status in ("online", "live", "streaming"):
        return True

    stream = data.get("stream")
    if isinstance(stream, dict):
        for key in ("online", "is_online", "streaming", "live"):
            if key in stream:
                return bool(stream[key])

    return False


def extract_message_record(message: PicaMessage) -> dict[str, Any]:
    """
    picabot 的 PicaMessage 字段可能随版本变化。
    这里尽量兼容常见字段；如果你的运行日志里字段为空，
    可以把 message.__dict__ 打印出来后按实际字段调整。
    """
    raw = getattr(message, "__dict__", {})

    return {
        "record_type": "chat_message",
        "channel": PICARTO_CHANNEL,
        "recorded_at": now_utc_iso(),
        "user_name": (
            getattr(message, "user_name", None)
            or getattr(message, "username", None)
            or getattr(message, "name", None)
        ),
        "message": (
            getattr(message, "message", None)
            or getattr(message, "content", None)
            or getattr(message, "text", None)
        ),
        "raw": raw,
    }


def start_new_session() -> None:
    global current_log_file, current_session_started_at

    current_log_file = new_session_file()
    current_session_started_at = now_utc_iso()

    write_jsonl(
        current_log_file,
        {
            "record_type": "session_start",
            "channel": PICARTO_CHANNEL,
            "recorded_at": current_session_started_at,
            "file": str(current_log_file),
        },
    )

    print(f"Started new session log: {current_log_file}", flush=True)


def end_current_session(reason: str) -> None:
    global current_log_file, current_session_started_at

    if current_log_file is None:
        return

    write_jsonl(
        current_log_file,
        {
            "record_type": "session_end",
            "channel": PICARTO_CHANNEL,
            "recorded_at": now_utc_iso(),
            "started_at": current_session_started_at,
            "reason": reason,
        },
    )

    print(f"Ended session log: {current_log_file}, reason={reason}", flush=True)

    current_log_file = None
    current_session_started_at = None


async def online_status_loop() -> None:
    """
    负责按开播切分文件：
    - offline -> online：新建一个 jsonl 文件
    - online -> offline：写入 session_end，关闭当前文件
    """
    was_online = False

    while not stop_event.is_set():
        try:
            online = is_channel_online(PICARTO_CHANNEL)

            if online and not was_online:
                start_new_session()

            if not online and was_online:
                end_current_session("offline")

            was_online = online

            print(
                f"Channel={PICARTO_CHANNEL}, online={online}, log_file={current_log_file}",
                flush=True,
            )

        except Exception as e:
            print(f"Failed to check channel status: {e}", flush=True)

        await asyncio.sleep(CHECK_INTERVAL)

    if current_log_file is not None:
        end_current_session("stopped")


bot = PicaBot.from_password(
    PICARTO_BOT_USERNAME,
    PICARTO_BOT_PASSWORD,
    PICARTO_CHANNEL,
)


@bot.on("message")
async def on_message(message: PicaMessage):
    """
    只有频道在线、当前 session 文件存在时才写入。
    这样可以确保文件按每次开播划分。
    """
    if current_log_file is None:
        return

    record = extract_message_record(message)
    write_jsonl(current_log_file, record)

    print(json.dumps(record, ensure_ascii=False), flush=True)


async def run_bot_forever() -> None:
    while not stop_event.is_set():
        try:
            print("Connecting Picarto bot...", flush=True)
            await bot.connect()

        except Exception as e:
            print(f"Bot connection error: {e}. Reconnecting in 10s.", flush=True)
            await asyncio.sleep(10)


async def main() -> None:
    status_task = asyncio.create_task(online_status_loop())
    bot_task = asyncio.create_task(run_bot_forever())

    done, pending = await asyncio.wait(
        [status_task, bot_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()


def handle_stop(*_args) -> None:
    stop_event.set()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    asyncio.run(main())
