"""Incremental, read-only sync from the Zotero Web API into the state DB."""
import json
import os
from pathlib import Path

import requests

from .common import db, kv_get, kv_set, log

API = "https://api.zotero.org"


class Zotero:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = f"{API}/users/{cfg['zotero']['user_id']}"
        self.s = requests.Session()
        self.s.headers.update({"Zotero-API-Key": os.environ["ZOTERO_API_KEY"], "Zotero-API-Version": "3"})

    def _get(self, path, **params):
        r = self.s.get(self.base + path, params=params, timeout=60)
        r.raise_for_status()
        return r

    def _paged(self, path, **params):
        start = 0
        while True:
            r = self._get(path, start=start, limit=100, **params)
            batch = r.json()
            yield from batch
            total = int(r.headers.get("Total-Results", 0))
            start += len(batch)
            if not batch or start >= total:
                return

    def collections(self) -> dict:
        cols = {c["key"]: c["data"] for c in self._paged("/collections")}
        def path(k):
            c = cols[k]
            p = c.get("parentCollection")
            return (path(p) + " / " if p and p in cols else "") + c["name"]
        return {k: path(k) for k in cols}

    def download_file(self, att_key: str, dest: Path) -> bool:
        r = self.s.get(f"{self.base}/items/{att_key}/file", timeout=300, stream=True)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name("." + dest.name + ".part")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
        tmp.replace(dest)
        return True


def sync(cfg: dict) -> dict:
    """Pull items changed since the last sync. Returns counts."""
    z = Zotero(cfg)
    con = db()
    since = int(kv_get(con, "zotero_version", 0))
    head = z._get("/items", limit=1, format="versions")
    remote_version = int(head.headers["Last-Modified-Version"])
    if remote_version == since:
        return {"new": 0, "updated": 0}

    cols = z.collections()
    kv_set(con, "zotero_collections", json.dumps(cols))
    excluded = cfg["zotero"]["exclude_collections"]
    skip_types = set(cfg["zotero"]["skip_item_types"])

    def is_excluded(data):
        paths = [cols.get(c, "") for c in data.get("collections", [])]
        return bool(paths) and all(any(p == e or p.startswith(e + " / ") for e in excluded) for p in paths)

    new = updated = 0
    for it in z._paged("/items", since=since, includeTrashed=0):
        d = it["data"]
        key, ver = d["key"], d["version"]
        row = con.execute("SELECT status FROM items WHERE key=?", (key,)).fetchone()
        standalone_file = (d["itemType"] == "attachment" and not d.get("parentItem")
                           and best_attachment([d]) is not None)
        if d.get("parentItem"):
            status = "child"
        elif (d["itemType"] in skip_types and not standalone_file) or d.get("deleted") or is_excluded(d):
            status = "skipped"
        else:
            status = None
        if row is None:
            con.execute("INSERT INTO items(key, version, data, status) VALUES(?,?,?,?)",
                        (key, ver, json.dumps(d), status or "queued"))
            new += status is None
        else:
            # A previously ingested item keeps 'done' but is flagged for a metadata refresh.
            new_status = status or ("refresh" if row["status"] in ("done", "refresh") else row["status"])
            if row["status"] == "skipped" and status is None:
                new_status = "queued"
            con.execute("UPDATE items SET version=?, data=?, status=?, updated=datetime('now') WHERE key=?",
                        (ver, json.dumps(d), new_status, key))
            updated += 1
    for key in z._get("/deleted", since=since).json().get("items", []):
        con.execute("UPDATE items SET status='deleted' WHERE key=? AND status NOT IN ('done','refresh')", (key,))
    con.commit()
    kv_set(con, "zotero_version", remote_version)
    log.info("zotero sync %s -> %s: %d new, %d updated", since, remote_version, new, updated)
    return {"new": new, "updated": updated}


def children(con, parent_key: str) -> list[dict]:
    rows = con.execute("SELECT data FROM items WHERE status='child'").fetchall()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        if d.get("parentItem") == parent_key:
            out.append(d)
    return out


def best_attachment(atts: list[dict]) -> dict | None:
    """Prefer stored PDFs, then stored HTML snapshots."""
    def rank(a):
        stored = a.get("linkMode") in ("imported_file", "imported_url")
        ct = a.get("contentType", "")
        if stored and ct == "application/pdf":
            return 0
        if stored and ct == "text/html":
            return 1
        return 9
    atts = [a for a in atts if a.get("itemType") == "attachment" and rank(a) < 9]
    return min(atts, key=rank) if atts else None


def collection_paths(con, data: dict) -> list[str]:
    cols = json.loads(kv_get(con, "zotero_collections", "{}"))
    return [cols[c] for c in data.get("collections", []) if c in cols]
