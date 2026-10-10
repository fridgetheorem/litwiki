#!/bin/bash
# Keep Caddy, Syncthing, the web UI and the Discord bot running (no systemd user lingering on this host). Run from cron.
export PATH="$HOME/.local/bin:$PATH"
L="$HOME/litwiki/logs"
if ! pgrep -u "$(id -u)" -x caddy >/dev/null; then
    echo "$(date -Is) starting caddy" >> "$L/watchdog.log"
    nohup caddy run --config "$HOME/litwiki/Caddyfile" --adapter caddyfile >> "$L/caddy.out" 2>&1 &
fi
if ! pgrep -u "$(id -u)" -x syncthing >/dev/null; then
    echo "$(date -Is) starting syncthing" >> "$L/watchdog.log"
    nohup syncthing serve --home "$HOME/.config/syncthing-litwiki" --no-browser \
        --gui-address=127.0.0.1:8384 >> "$L/syncthing.log" 2>&1 &
fi
if ! pgrep -u "$(id -u)" -f "bin/litwiki serve$" >/dev/null; then
    echo "$(date -Is) starting litwiki web" >> "$L/watchdog.log"
    nohup "$HOME/litwiki/.venv/bin/litwiki" serve >> "$L/web.out" 2>&1 &
fi
if [ -n "$(grep -s '^DISCORD_BOT_TOKEN=.' "$HOME/.env")" ] && ! pgrep -u "$(id -u)" -f "bin/litwiki discord$" >/dev/null; then
    echo "$(date -Is) starting litwiki discord bot" >> "$L/watchdog.log"
    nohup "$HOME/litwiki/.venv/bin/litwiki" discord >> "$L/discord.out" 2>&1 &
fi
f="$L/cron.out"; [ -f "$f" ] && [ "$(stat -c%s "$f")" -gt 5000000 ] && tail -c 1000000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
