#!/bin/bash
# Cron entry point: run a litwiki subcommand with the right PATH (claude lives in ~/.local/bin).
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$HOME/litwiki" || exit 1
exec nice -n 10 "$HOME/litwiki/.venv/bin/litwiki" "$@" >> "$HOME/litwiki/logs/cron.out" 2>&1
