"""Notifications via Discord webhook (falls back to the log if not configured)."""
import os

import requests

from .common import LOG_DIR, db, log


def notify(message: str, *, once_key: str | None = None, level: str = "info") -> None:
    """Send a Discord message. `once_key` suppresses repeats of the same alert."""
    if once_key:
        con = db()
        if con.execute("SELECT 1 FROM notified WHERE k=?", (once_key,)).fetchone():
            return
        con.execute("INSERT INTO notified(k) VALUES(?)", (once_key,))
        con.commit()
    icon = {"info": "📚", "warn": "⚠️", "error": "🚨"}.get(level, "📚")
    text = f"{icon} **litwiki** {message}"[:1990]
    with open(LOG_DIR / "notifications.log", "a") as fh:
        fh.write(text.replace("\n", " | ") + "\n")
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        log.warning("DISCORD_WEBHOOK_URL not set; notification logged only: %s", message)
        return
    try:
        r = requests.post(url, json={"content": text}, timeout=20)
        r.raise_for_status()
    except Exception as e:  # never let notification failure kill a job
        log.error("discord notify failed: %s", e)
