#!/bin/bash
# Keep Caddy, the web UI, the Discord bot and (only if syncthing.enabled) Syncthing running (no systemd user lingering needed). Run from cron.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
L="$ROOT/logs"; mkdir -p "$L"
LW="$ROOT/.venv/bin/litwiki"
# Paths and ports come from config.yaml (LITWIKI_*, SYNCTHING_*).
eval "$("$LW" config-env)" || { echo "$(date -Is) config-env failed (is config.yaml present?)" >> "$L/watchdog.log"; exit 1; }
if ! pgrep -u "$(id -u)" -x caddy >/dev/null; then
    echo "$(date -Is) starting caddy" >> "$L/watchdog.log"
    nohup caddy run --config "$ROOT/Caddyfile" --adapter caddyfile >> "$L/caddy.out" 2>&1 &
fi
if [ "${SYNCTHING_ENABLED:-0}" = 1 ] && ! pgrep -u "$(id -u)" -x syncthing >/dev/null; then
    echo "$(date -Is) starting syncthing" >> "$L/watchdog.log"
    nohup syncthing serve --home "$SYNCTHING_HOME" --no-browser \
        --gui-address="$SYNCTHING_GUI" >> "$L/syncthing.log" 2>&1 &
fi
if ! pgrep -u "$(id -u)" -f "bin/litwiki serve$" >/dev/null; then
    echo "$(date -Is) starting litwiki web" >> "$L/watchdog.log"
    nohup "$LW" serve >> "$L/web.out" 2>&1 &
fi
if [ -n "$(grep -s '^DISCORD_BOT_TOKEN=.' "$HOME/.env")" ] && ! pgrep -u "$(id -u)" -f "bin/litwiki discord$" >/dev/null; then
    echo "$(date -Is) starting litwiki discord bot" >> "$L/watchdog.log"
    nohup "$LW" discord >> "$L/discord.out" 2>&1 &
fi
f="$L/cron.out"; [ -f "$f" ] && [ "$(stat -c%s "$f")" -gt 5000000 ] && tail -c 1000000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
