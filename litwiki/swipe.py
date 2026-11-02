"""Swipe screening of digest items: swipe right = ingest, left = dismiss.

Each swipe is recorded immediately in `swipe_decisions` (so nothing is lost if you close the tab) but only *applied*
when you press Finish: ingesting costs money, so a mis-swipe can be undone until then. Decisions you never finish
are applied by the hourly job once they are `STALE_MINUTES` old. Applying is the same thing the Inbox does: ingest =
tick the item in its digest file (what ticking in Obsidian does) and let the ingest job pick it up; dismiss =
mark the candidate rejected."""
import json
import re

from .common import db
from .discover import TICKED, tick_items

STALE_MINUTES = 10
ABSTRACT_CHARS = 1800
SCHEMA = """CREATE TABLE IF NOT EXISTS swipe_decisions (
    id TEXT PRIMARY KEY,              -- candidate id
    action TEXT,                      -- ingest | dismiss
    ts TEXT DEFAULT (datetime('now')));"""


def _con(con=None):
    con = con or db()
    con.executescript(SCHEMA)
    return con


def digest_label(digest: str) -> str:
    name = digest.removeprefix("digests/").removesuffix(".md")
    m = re.fullmatch(r"search-(.+)-(\d{4}-\d{2}-\d{2})", name)
    return f"Search · {m.group(1).replace('-', ' ')} · {m.group(2)}" if m else f"Daily · {name}"


def deck(cfg, con=None) -> list[dict]:
    """Pending digest items nobody has decided on yet: newest digest first, best score first."""
    con = _con(con)
    ticked = set()
    for p in (cfg["vault"] / "digests").glob("*.md"):
        ticked.update(TICKED.findall(p.read_text(encoding="utf-8")))
    decided = {r["id"] for r in con.execute("SELECT id FROM swipe_decisions")}
    out = []
    for r in con.execute("SELECT id, data, digest FROM candidates WHERE status='digested' AND digest!='web' "
                         "ORDER BY digest DESC, score DESC"):
        if r["id"] in ticked or r["id"] in decided:
            continue
        c = json.loads(r["data"])
        authors = [str(a) for a in c.get("authors", [])]
        out.append({
            "id": r["id"], "title": c.get("title", ""), "score": c.get("score", ""), "why": c.get("why", ""),
            "authors": ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else ""),
            "venue": c.get("venue", ""), "date": c.get("date", ""), "url": c.get("url", ""),
            "pdf_url": c.get("pdf_url", ""), "kind": c.get("kind", ""),
            "abstract": (c.get("abstract") or "")[:ABSTRACT_CHARS], "digest": digest_label(r["digest"]),
            "digest_file": r["digest"],
        })
    def day(x):  # digest page date, whatever its name (daily: 2026-11-02, search: search-<review>-2026-11-02)
        m = re.search(r"(\d{4}-\d{2}-\d{2})", x["digest_file"])
        return m.group(1) if m else ""
    out.sort(key=lambda x: -float(x["score"] or 0))   # best first within a day (sort is stable)
    out.sort(key=day, reverse=True)                    # newest digest first
    for x in out:
        del x["digest_file"]
    return out


def pending(con=None) -> dict:
    con = _con(con)
    counts = {"ingest": 0, "dismiss": 0}
    for r in con.execute("SELECT action, COUNT(*) n FROM swipe_decisions GROUP BY action"):
        counts[r["action"]] = r["n"]
    return counts


def decide(cid: str, action: str, con=None) -> dict:
    """Record (or, with action 'undo', remove) a decision for a candidate that is still waiting in a digest."""
    con = _con(con)
    if action == "undo":
        con.execute("DELETE FROM swipe_decisions WHERE id=?", (cid,))
    elif action in ("ingest", "dismiss"):
        row = con.execute("SELECT 1 FROM candidates WHERE id=? AND status='digested' AND digest!='web'", (cid,)).fetchone()
        if not row:
            raise ValueError("That item is no longer waiting for a decision.")
        con.execute("INSERT INTO swipe_decisions(id, action) VALUES(?,?) "
                    "ON CONFLICT(id) DO UPDATE SET action=excluded.action, ts=datetime('now')", (cid, action))
    else:
        raise ValueError(f"unknown action {action!r}")
    con.commit()
    return pending(con)


def commit(cfg, older_than_min: float = 0, con=None) -> dict:
    """Apply recorded decisions (all of them, or only those older than `older_than_min` minutes)."""
    con = _con(con)
    rows = con.execute("SELECT id, action FROM swipe_decisions WHERE "
                       "(julianday('now') - julianday(ts)) * 1440 >= ?", (older_than_min,)).fetchall()
    ingest = [r["id"] for r in rows if r["action"] == "ingest"]
    dismiss = [r["id"] for r in rows if r["action"] == "dismiss"]
    ticked = tick_items(cfg, ingest) if ingest else 0
    if dismiss:
        con.executemany("UPDATE candidates SET status='rejected' WHERE id=? AND status='digested'",
                        [(i,) for i in dismiss])
    con.executemany("DELETE FROM swipe_decisions WHERE id=?", [(r["id"],) for r in rows])
    con.commit()
    return {"ingest": ticked, "dismiss": len(dismiss), "skipped": len(ingest) - ticked}
