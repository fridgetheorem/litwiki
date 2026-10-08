"""litwiki command line. Cron calls `hourly` and `daily`; the rest are for manual use."""
import argparse
import sys
import traceback
from datetime import datetime, timezone

from .common import db, kv_get, kv_set, load_config, log, run_lock, setup_logging


def cmd_sync(cfg, a):
    from .zotero import sync
    print(sync(cfg))


def cmd_bootstrap(cfg, a):
    from .bootstrap import bootstrap
    print("\n".join(bootstrap(cfg)))


def cmd_ingest(cfg, a):
    from .ingest import ingest_zotero
    print(ingest_zotero(cfg, limit=a.limit))


def cmd_discover(cfg, a):
    from .discover import discover
    print(discover(cfg))


def cmd_approved(cfg, a):
    from .discover import ingest_approved
    print(ingest_approved(cfg))


def cmd_build(cfg, a):
    from .site import build
    print(build(cfg, force=a.force))


def cmd_status(cfg, a):
    from .llm import spent
    con = db()
    print("Zotero items by status:")
    for r in con.execute("SELECT status, COUNT(*) n FROM items GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:10} {r['n']}")
    print(f"LLM spend: today ${spent(con, 'day'):.2f}, this month ${spent(con, 'month'):.2f} "
          f"(cap ${cfg['budget']['monthly_usd']:.0f})")
    for r in con.execute("SELECT purpose, COUNT(*) n, SUM(usd) s, AVG(usd) a FROM spend "
                         "WHERE strftime('%Y-%m',ts)=strftime('%Y-%m','now') GROUP BY purpose"):
        print(f"  {r['purpose']:18} {r['n']:4} calls  ${r['s']:.2f}  (avg ${r['a']:.3f})")
    for r in con.execute("SELECT key, note FROM items WHERE status='failed' LIMIT 10"):
        print(f"  failed {r['key']}: {r['note'][:120]}")


def cmd_retry(cfg, a):
    con = db()
    n = con.execute("UPDATE items SET status='queued' WHERE status='failed'").rowcount
    con.commit()
    print(f"requeued {n}")


def cmd_redo(cfg, a):
    """Delete a generated paper page (and its backlinks) and requeue the Zotero item."""
    from . import vault
    con = db()
    for key in a.keys:
        row = con.execute("SELECT page FROM items WHERE key=?", (key,)).fetchone()
        if not row:
            print(f"{key}: unknown"); continue
        if row["page"]:
            vault.remove_backlinks(cfg["vault"], row["page"])
            (cfg["vault"] / row["page"]).unlink(missing_ok=True)
        con.execute("UPDATE items SET status='queued', page=NULL, note=NULL WHERE key=?", (key,))
        con.commit()
        print(f"{key}: requeued (was {row['page']})")


def cmd_retag(cfg, a):
    from .ingest import retag_vault
    with run_lock():
        print(f"retagged {retag_vault(cfg)} pages")


def cmd_review_new(cfg, a):
    from . import vault
    slug = vault.create_review(cfg["vault"], " ".join(a.title))
    print(f"vault/reviews/{slug}.md created: fill in the question, scope and search_terms")


def cmd_notify_test(cfg, a):
    from .notify import notify
    notify("Test notification — Discord is wired up.")
    print("sent (check Discord; if DISCORD_WEBHOOK_URL is unset it was only logged)")


def cmd_hourly(cfg, a):
    """Zotero sync -> ingest new/approved items -> rebuild site if the vault changed."""
    from .discover import ingest_approved
    from .health import check_health
    from .ingest import ingest_zotero
    from .notify import notify
    from .site import build
    from .zotero import sync
    with run_lock():
        s = sync(cfg)
        approved = ingest_approved(cfg)
        z = ingest_zotero(cfg)
        if z["failed"] or approved.get("failed"):
            notify(f"Hourly run: {z['failed'] + approved.get('failed', 0)} item(s) failed to ingest. "
                   "See `litwiki status`.", level="warn")
        log.info("hourly: sync=%s zotero=%s approved=%s", s, z, approved)
    check_health(cfg)
    build(cfg)


def cmd_daily(cfg, a):
    from .discover import discover
    with run_lock():
        discover(cfg)
    from .site import build
    build(cfg)


def main():
    p = argparse.ArgumentParser(prog="litwiki")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="pull changes from Zotero").set_defaults(f=cmd_sync)
    sub.add_parser("bootstrap", help="propose initial concept pages from the library").set_defaults(f=cmd_bootstrap)
    sp = sub.add_parser("ingest", help="summarise queued Zotero items")
    sp.add_argument("--limit", type=int)
    sp.set_defaults(f=cmd_ingest)
    sub.add_parser("discover", help="search for new literature and write a digest").set_defaults(f=cmd_discover)
    sub.add_parser("approved", help="ingest items ticked in digests").set_defaults(f=cmd_approved)
    sp = sub.add_parser("build", help="rebuild the website")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(f=cmd_build)
    sub.add_parser("status", help="queue, spend and failures").set_defaults(f=cmd_status)
    sub.add_parser("retry", help="requeue failed items").set_defaults(f=cmd_retry)
    sp = sub.add_parser("redo", help="delete + requeue generated pages by Zotero key")
    sp.add_argument("keys", nargs="+")
    sp.set_defaults(f=cmd_redo)
    sub.add_parser("retag", help="refresh Obsidian tags on all paper pages (no LLM)").set_defaults(f=cmd_retag)
    sp = sub.add_parser("review-new", help="scaffold a literature-review page in the vault")
    sp.add_argument("title", nargs="+")
    sp.set_defaults(f=cmd_review_new)
    sub.add_parser("notify-test", help="send a test Discord notification").set_defaults(f=cmd_notify_test)
    sub.add_parser("hourly", help="cron: sync + ingest + build").set_defaults(f=cmd_hourly)
    sub.add_parser("daily", help="cron: discovery digest").set_defaults(f=cmd_daily)
    a = p.parse_args()
    setup_logging(a.cmd if a.cmd in ("hourly", "daily") else "manual")
    cfg = load_config()
    con = db()
    if not kv_get(con, "installed_at"):
        # Zotero items added before this moment are 'backlog' and metered separately.
        kv_set(con, "installed_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    try:
        a.f(cfg, a)
    except SystemExit:
        raise
    except Exception as e:
        log.exception("%s failed", a.cmd)
        if a.cmd in ("hourly", "daily"):
            from .notify import notify
            notify(f"`{a.cmd}` job crashed: {type(e).__name__}: {e}\n```{traceback.format_exc()[-800:]}```",
                   level="error")
        sys.exit(1)


if __name__ == "__main__":
    main()
