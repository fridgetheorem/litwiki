"""Hourly health checks that notify on anything needing attention."""
import shutil
import subprocess
from datetime import date

from .common import db, log
from .notify import notify


def check_health(cfg) -> None:
    v = cfg["vault"]
    sync_on = bool(cfg["syncthing"]["enabled"])
    conflicts = [p.relative_to(v).as_posix() for p in v.rglob("*.sync-conflict-*")] if sync_on else []
    if conflicts:
        notify(f"Syncthing conflict file(s) in the vault — the pipeline and an Obsidian edit touched the same "
               f"page. Merge and delete: {', '.join(conflicts[:5])}",
               once_key=f"conflict-{'|'.join(sorted(conflicts))}", level="warn")
    free = shutil.disk_usage(v).free / 2**30
    if free < 2:
        notify(f"Low disk space: {free:.1f} GiB free.", once_key=f"disk-{date.today()}", level="warn")
    for name in ("caddy", *(("syncthing",) if sync_on else ())):
        if subprocess.run(["pgrep", "-u", str(__import__('os').getuid()), "-x", name],
                          capture_output=True).returncode != 0:
            notify(f"`{name}` is not running (the watchdog will try to restart it).",
                   once_key=f"down-{name}-{date.today()}", level="warn")
    con = db()
    stuck = con.execute("SELECT COUNT(*) FROM items WHERE status='failed'").fetchone()[0]
    if stuck:
        log.info("%d failed items (see `litwiki status`, `litwiki retry`)", stuck)
