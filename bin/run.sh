#!/bin/bash
# Cron entry point: run a litwiki subcommand with the right PATH (claude lives in ~/.local/bin).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$ROOT" || exit 1
mkdir -p "$ROOT/logs"
exec nice -n 10 "$ROOT/.venv/bin/litwiki" "$@" >> "$ROOT/logs/cron.out" 2>&1
