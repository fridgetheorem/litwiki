"""The reader's own annotation layer: free-form pen and highlighter strokes drawn on a paper's PDF.

Strokes are stored one row each in state.db (table `annotations`), in PDF user space (points, origin bottom-left, 0-based
page index), the same coordinates Zotero uses for its ink annotations, so they can be exchanged with Zotero later. The browser
sends small batches of operations (add / delete / restore) and chooses the ids, so a retried request or a second device
cannot duplicate or clobber anything. Deleting is soft, which lets "undo" bring a stroke back."""
import json
import math
import re

from .common import db

KINDS = {"pen", "highlighter"}
MAX_PER_PAPER = 20000
MAX_PATHS, MAX_NUMBERS, MAX_OPS_BYTES = 40, 20000, 2_000_000
_ID = re.compile(r"[0-9a-f]{8,32}")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


class AnnotationError(Exception):
    """Shown to the user as-is (a malformed request, or too much ink)."""


def _nums(v, what: str) -> list[float]:
    if not isinstance(v, list) or len(v) > MAX_NUMBERS:
        raise AnnotationError(f"{what} must be a list of at most {MAX_NUMBERS} numbers.")
    out = []
    for x in v:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > 1e6:
            raise AnnotationError(f"{what} holds a value that is not a sane number.")
        out.append(round(float(x), 2))
    return out


def clean(a) -> dict:
    """Validate one stroke from the browser; returns the normalised dict. Raises AnnotationError."""
    if not isinstance(a, dict):
        raise AnnotationError("An annotation must be an object.")
    aid, page, kind = a.get("id"), a.get("page"), a.get("kind")
    if not isinstance(aid, str) or not _ID.fullmatch(aid):
        raise AnnotationError("Bad annotation id.")
    if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page < 10000:
        raise AnnotationError("Bad page number.")
    if kind not in KINDS:
        raise AnnotationError("Unknown annotation kind.")
    color = a.get("color")
    if not isinstance(color, str) or not _COLOR.fullmatch(color):
        raise AnnotationError("Bad colour.")
    try:
        width, opacity = float(a.get("width")), float(a.get("opacity", 1))
    except (TypeError, ValueError):
        raise AnnotationError("Bad width or opacity.")
    if not (0.1 <= width <= 80 and 0.05 <= opacity <= 1):
        raise AnnotationError("Width or opacity out of range.")
    paths = a.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_PATHS:
        raise AnnotationError("A stroke needs between 1 and %d paths." % MAX_PATHS)
    paths = [_nums(p, "A path") for p in paths]
    if any(len(p) < 2 or len(p) % 2 for p in paths):
        raise AnnotationError("A path is a flat list of x,y pairs.")
    pressures = a.get("pressures")
    if pressures is not None:
        pressures = [_nums(p, "Pressures") for p in pressures] if isinstance(pressures, list) else None
        if pressures is None or len(pressures) != len(paths) or any(
                len(pr) != len(p) // 2 or any(not 0 <= x <= 1 for x in pr) for pr, p in zip(pressures, paths)):
            pressures = None     # pressure is a nicety; drop it rather than refuse the stroke
    return {"id": aid, "page": page, "kind": kind, "color": color.lower(), "width": round(width, 2),
            "opacity": round(opacity, 2), "paths": paths, "pressures": pressures}


def _row(r) -> dict:
    d = json.loads(r["data"])
    return {"id": r["id"], "page": r["page"], "kind": r["kind"], "color": r["color"], "width": r["width"],
            "opacity": r["opacity"], "paths": d["paths"], "pressures": d.get("pressures")}


def items(stem: str) -> list[dict]:
    rows = db().execute("SELECT * FROM annotations WHERE stem=? AND deleted=0 ORDER BY page, created, rowid", (stem,)).fetchall()
    return [_row(r) for r in rows]


def apply(stem: str, ops_json: str) -> dict:
    """Apply {"add": [stroke...], "delete": [id...], "restore": [id...]}; returns {added, deleted, restored, total}."""
    if len(ops_json) > MAX_OPS_BYTES:
        raise AnnotationError("That is too much ink in one go.")
    try:
        ops = json.loads(ops_json)
    except ValueError:
        raise AnnotationError("Could not read the changes.")
    if not isinstance(ops, dict):
        raise AnnotationError("Could not read the changes.")
    adds = [clean(a) for a in (ops.get("add") or [])]
    ids = lambda k: [i for i in (ops.get(k) or []) if isinstance(i, str) and _ID.fullmatch(i)]
    dels, rests = ids("delete"), ids("restore")
    con = db()
    live = con.execute("SELECT COUNT(*) FROM annotations WHERE stem=? AND deleted=0", (stem,)).fetchone()[0]
    if live + len(adds) > MAX_PER_PAPER:
        raise AnnotationError("This paper has reached the limit of %d strokes." % MAX_PER_PAPER)
    added = 0
    for a in adds:
        cur = con.execute("INSERT OR IGNORE INTO annotations(id, stem, page, kind, color, width, opacity, data) VALUES(?,?,?,?,?,?,?,?)",
                          (a["id"], stem, a["page"], a["kind"], a["color"], a["width"], a["opacity"],
                           json.dumps({"paths": a["paths"], "pressures": a["pressures"]}, separators=(",", ":"))))
        added += cur.rowcount
    deleted = restored = 0
    for i in dels:    # only ever this paper's own rows, whatever ids the browser names
        deleted += con.execute("UPDATE annotations SET deleted=1, updated=datetime('now') WHERE id=? AND stem=? AND deleted=0", (i, stem)).rowcount
    for i in rests:
        restored += con.execute("UPDATE annotations SET deleted=0, updated=datetime('now') WHERE id=? AND stem=? AND deleted=1", (i, stem)).rowcount
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM annotations WHERE stem=? AND deleted=0", (stem,)).fetchone()[0]
    return {"added": added, "deleted": deleted, "restored": restored, "total": total}
