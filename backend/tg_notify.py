"""Send a Telegram message to the allowlisted DocChat admins.

Used by cron/systemd jobs (backups, long-running tasks) that want to notify
the owner without going through the bot's long-polling loop.

Usage:
    python tg_notify.py "Backup completed at 03:47"
"""
import os
import pathlib
import sys

import httpx

BASE = pathlib.Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
except Exception:
    pass

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().isdigit()]


def notify(message: str) -> int:
    if not TOKEN or not ADMIN_IDS:
        return 1
    sent = 0
    for uid in ADMIN_IDS:
        r = httpx.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": uid, "text": message},
            timeout=30,
        )
        if r.status_code == 200:
            sent += 1
    return 0 if sent == len(ADMIN_IDS) else 1


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) or "DocChat notification"
    raise SystemExit(notify(msg))