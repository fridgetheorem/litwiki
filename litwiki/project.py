"""Projects: a literature review (reviews/<slug>.md) that grows over time.

Membership is the `reviews:` list in a paper's frontmatter (surfaced as the `review/<slug>` tag). This module links
library papers to a project, builds the pool of candidates for the bulk-screening page (library papers not yet in
the project + new papers found by the project's searches), and runs a cheap Haiku pass that scores each candidate
against the project and says, in a few bullets, how it relates. Results are cached per project so reopening the
page costs nothing."""
import json
import re
import threading

from . import vault
from .common import db, log, read_page, replace_block, write_page

STEM = re.compile(r"^[\w.\-]+$")
BATCH = 12
SCHEMA_DB = """CREATE TABLE IF NOT EXISTS project_screen (
    project TEXT, cid TEXT, score INTEGER, bullets TEXT, ts TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (project, cid));"""

SYSTEM = """You help a researcher decide which papers belong in one project (a literature review that is still taking shape).
The project's description, scope and exclusions are given. For each candidate paper give:
- score 0-10: how useful it is for THIS project (9-10 core reference; 6-8 clearly relevant evidence or background;
  3-5 tangential; 0-2 off-topic or matches the exclusions);
- 2-3 bullets (each at most 22 words) saying specifically how it relates to the project: what it contributes, how it
  could be used, or why it does not fit. Do not restate the title. Use only what the candidate text says; if the text
  is too thin to judge, say so in a bullet and score conservatively."""

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "score": {"type": "integer"},
                       "bullets": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
        "required": ["n", "score", "bullets"]}}},
    "required": ["items"],
}

JOBS: dict[str, dict] = {}   # slug -> {"running", "done", "total", "error"} (this process only; the page polls it)


def _con(con=None):
    con = con or db()
    con.executescript(SCHEMA_DB)
    return con


def get(cfg, slug: str) -> dict:
    rv = vault.reviews(cfg["vault"]).get(slug)
    if not rv:
        raise KeyError(slug)
    return rv


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z\-]{3,}", text.lower())}


def _overlap(rv: dict, text: str) -> int:
    proj = _tokens(" ".join([rv.get("title", ""), *(rv.get("search_terms") or []), rv.get("text", "")[:1500]]))
    return len(proj & _tokens(text))


def pool(cfg, slug: str, con=None) -> list[dict]:
    """Every paper relevant to the screening page: linked ('linked'), in the library but not linked ('library'),
    and new candidates from this project's searches that are waiting in a digest ('new')."""
    from .cluster import section_text
    from .discover import TICKED
    from .swipe import digest_label
    con, v, rv = _con(con), cfg["vault"], get(cfg, slug)
    out = []
    for p in sorted((v / vault.PAPERS).glob("*.md")):
        fm, body = read_page(p)
        if not fm.get("title"):
            continue
        authors = [str(a) for a in (fm.get("authors") or []) if not str(a).strip().isdigit()]
        text = section_text(body, ["TL;DR", "Problem"])
        out.append({
            "id": f"page:{p.stem}", "kind": "linked" if slug in (fm.get("reviews") or []) else "library",
            "title": str(fm["title"]), "authors": ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else ""),
            "year": fm.get("year") or "", "venue": fm.get("venue") or "", "abstract": text[:900],
            "url": f"/papers/{p.stem}/", "pdf_url": "", "overlap": _overlap(rv, f"{fm['title']} {text}"),
        })
    ticked = set()
    for p in (v / vault.DIGESTS).glob("*.md"):
        ticked.update(TICKED.findall(p.read_text(encoding="utf-8")))
    for r in con.execute("SELECT id, data, digest FROM candidates WHERE status='digested' AND digest!='web'"):
        c = json.loads(r["data"])
        if c.get("review") != slug or r["id"] in ticked:
            continue
        authors = [str(a) for a in c.get("authors", [])]
        out.append({
            "id": r["id"], "kind": "new", "title": c.get("title", ""),
            "authors": ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else ""),
            "year": c.get("year") or (c.get("date") or "")[:4], "venue": c.get("venue", ""),
            "abstract": (c.get("abstract") or "")[:1200], "url": c.get("url", ""), "pdf_url": c.get("pdf_url", ""),
            "triage": c.get("score", ""), "digest": digest_label(r["digest"]),
            "overlap": _overlap(rv, f"{c.get('title', '')} {c.get('abstract', '')}"),
        })
    return out


def cards(cfg, slug: str, con=None) -> list[dict]:
    """The pool joined with cached Haiku results (fit score + bullets, or None when not analysed yet)."""
    con = _con(con)
    cached = {r["cid"]: r for r in con.execute("SELECT cid, score, bullets FROM project_screen WHERE project=?", (slug,))}
    out = pool(cfg, slug, con)
    for c in out:
        r = cached.get(c["id"])
        c["fit"] = r["score"] if r else None
        c["bullets"] = json.loads(r["bullets"]) if r else []
    return out


def pending_analysis(cfg, slug: str, con=None) -> list[dict]:
    """Unlinked candidates without a cached result: new ones first, then library papers by keyword overlap."""
    todo = [c for c in cards(cfg, slug, con) if c["kind"] != "linked" and c["fit"] is None]
    todo.sort(key=lambda c: (c["kind"] != "new", -c["overlap"]))
    return todo


def analyze(cfg, slug: str, limit: int = 120, con=None, progress=None) -> dict:
    """Score + explain up to `limit` unanalysed candidates with the triage model, BATCH at a time."""
    from .discover import review_context
    from .llm import claude
    con, rv = _con(con), get(cfg, slug)
    todo = pending_analysis(cfg, slug, con)[:limit]
    context = review_context(rv)
    done = 0
    if progress:
        progress(0, len(todo))
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        listing = "\n\n".join(
            f"[{n}] {c['title']} ({c['year'] or 'n.d.'}, {c['venue'] or 'no venue'})\n{c['abstract'][:900] or '(no abstract available)'}"
            for n, c in enumerate(batch))
        out = claude(cfg, f"{context}\n\nCANDIDATES:\n{listing}", system=SYSTEM, model=cfg["llm"]["triage_model"],
                     schema=SCHEMA, purpose="project-screen", ref=slug, timeout=300)
        for r in out["items"]:
            if 0 <= r["n"] < len(batch):
                con.execute("INSERT OR REPLACE INTO project_screen(project, cid, score, bullets) VALUES(?,?,?,?)",
                            (slug, batch[r["n"]]["id"], max(0, min(10, r["score"])),
                             json.dumps([b.strip() for b in r["bullets"] if b.strip()][:3])))
                done += 1
        con.commit()
        if progress:
            progress(min(i + BATCH, len(todo)), len(todo))
    return {"analyzed": done, "remaining": len(pending_analysis(cfg, slug, con))}


def start_analysis(cfg, slug: str, limit: int = 120) -> bool:
    """Run analyze() in a background thread (web UI). False if one is already running for this project."""
    if JOBS.get(slug, {}).get("running"):
        return False
    JOBS[slug] = {"running": True, "done": 0, "total": 0, "error": None}

    def progress(done, total):
        JOBS[slug].update(done=done, total=total)

    def run():
        try:
            analyze(cfg, slug, limit, progress=progress)
        except Exception as e:  # BudgetExceeded included: shown on the page
            log.warning("project screen %s: %s", slug, e)
            JOBS[slug]["error"] = str(e)[:300]
        JOBS[slug]["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


def sync_page(cfg, slug: str) -> None:
    """Keep the auto-managed paper list on reviews/<slug>.md in step with the papers linked to it."""
    v, rv = cfg["vault"], get(cfg, slug)
    rows = []
    for p in sorted((v / vault.PAPERS).glob("*.md")):
        fm, _ = read_page(p)
        if slug in (fm.get("reviews") or []):
            rows.append((str(fm.get("year") or "0000"), f"- [[papers/{p.stem}|{fm.get('title', p.stem)}]] ({fm.get('year') or 'n.d.'})"))
    rows.sort(key=lambda r: r[0], reverse=True)
    fm, body = read_page(rv["path"])
    new = replace_block(body, "papers", "\n".join(r[1] for r in rows) or "_none yet_")
    if "## Linked papers" not in new:
        new = new.replace("<!-- litwiki:papers:start -->", "## Linked papers\n\n<!-- litwiki:papers:start -->", 1)
    if new != body:
        write_page(rv["path"], fm, new)


def link(cfg, slug: str, stems: list[str], unlink: bool = False) -> int:
    """Add (or remove) a project to the `reviews:` list of paper pages; returns how many pages changed."""
    from .ingest import apply_tags
    get(cfg, slug)
    v, n = cfg["vault"], 0
    for stem in dict.fromkeys(stems):
        if not STEM.match(stem):
            continue
        path = v / vault.PAPERS / f"{stem}.md"
        if not path.is_file():
            continue
        fm, body = read_page(path)
        cur = list(fm.get("reviews") or [])
        new = [r for r in cur if r != slug] if unlink else sorted({*cur, slug})
        if new == cur:
            continue
        if new:
            fm["reviews"] = new
        else:
            fm.pop("reviews", None)
        apply_tags(fm)
        write_page(path, fm, body)
        n += 1
    if n:
        sync_page(cfg, slug)
    return n


def set_daily(cfg, slug: str, on: bool) -> None:
    """Turn the project's daily digest on or off (frontmatter `daily`)."""
    rv = get(cfg, slug)
    fm, body = read_page(rv["path"])
    if fm.get("daily") is not on:
        fm["daily"] = on
        write_page(rv["path"], fm, body)


def summary(cfg) -> list[dict]:
    """One row per project for the projects list page."""
    v = cfg["vault"]
    counts: dict[str, int] = {}
    for p in (v / vault.PAPERS).glob("*.md"):
        fm, _ = read_page(p)
        for r in fm.get("reviews") or []:
            counts[r] = counts.get(r, 0) + 1
    waiting: dict[str, int] = {}
    for r in _con().execute("SELECT data FROM candidates WHERE status='digested' AND digest!='web'"):
        s = json.loads(r["data"]).get("review")
        if s:
            waiting[s] = waiting.get(s, 0) + 1
    return [{"slug": s, "title": rv["title"], "linked": counts.get(s, 0), "waiting": waiting.get(s, 0),
             "terms": len(rv.get("search_terms") or []), "daily": rv.get("daily") is True,
             "status": str(rv.get("status", "active"))} for s, rv in vault.reviews(v).items()]
