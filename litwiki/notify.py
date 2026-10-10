"""Notifications via Discord webhook (falls back to the log if not configured)."""
import os

import requests

from .common import LOG_DIR, db, log


def bot_active() -> bool:
    """True when the Discord bot owns digest announcements (it posts them itself, with buttons)."""
    return bool(os.environ.get("DISCORD_BOT_TOKEN"))


def bot_channel() -> str | None:
    if not bot_active():
        return None
    from .common import kv_get
    return kv_get(db(), "discord_channel")


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
    if not url and (chan := bot_channel()):  # no webhook: post as the bot in its digest channel
        try:
            r = requests.post(f"https://discord.com/api/v10/channels/{chan}/messages", json={"content": text},
                              headers={"Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}"}, timeout=20)
            r.raise_for_status()
        except Exception as e:
            log.error("discord bot notify failed: %s", e)
        return
    if not url:
        log.warning("DISCORD_WEBHOOK_URL not set; notification logged only: %s", message)
        return
    try:
        r = requests.post(url, json={"content": text}, timeout=20)
        r.raise_for_status()
    except Exception as e:  # never let notification failure kill a job
        log.error("discord notify failed: %s", e)
