"""Projects: a literature review (reviews/<slug>.md) that grows over time.

Membership is the `reviews:` list in a paper's frontmatter (surfaced as the `review/<slug>` tag). This module links
library papers to a project, builds the pool of candidates for the bulk-screening page (library papers not yet in
the project + new papers found by the project's searches), and runs a cheap Haiku pass that scores each candidate
against the project and says, in a few bullets, how it relates. Results are cached per project so reopening the
page costs nothing."""
import hashlib
import json
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone

from . import vault
from .common import LOG_DIR, ROOT, db, log, read_page, replace_block, write_page
from .notes import Conflict

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
    from .llm import claude, pick_model
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
        out = claude(cfg, f"{context}\n\nCANDIDATES:\n{listing}", system=SYSTEM, model=pick_model(cfg, "screen"),
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


NOTES_DIR = "notes/projects"
SKELETON = "## Ideas\n\n\n## Questions\n\n\n## Outline\n\n\n## To do\n\n"


def notes_path(cfg, slug: str):
    get(cfg, slug)  # validates the slug against the existing review pages
    return cfg["vault"] / NOTES_DIR / f"{slug}.md"


def read_notes(cfg, slug: str) -> tuple[str, int]:
    """(text, mtime_ns) of the project's whiteboard notes; ('', 0) when there are none yet."""
    p = notes_path(cfg, slug)
    if not p.exists():
        return "", 0
    _, body = read_page(p)
    return body.strip("\n"), p.stat().st_mtime_ns


def save_notes(cfg, slug: str, text: str, expected_mtime: int) -> int:
    """Write the notes; raises notes.Conflict if the file changed (e.g. in Obsidian) since it was loaded.
    Empty text deletes the file. Returns the new mtime."""
    p = notes_path(cfg, slug)
    current = p.stat().st_mtime_ns if p.exists() else 0
    if current != expected_mtime:
        raise Conflict()
    text = text.replace("\r\n", "\n").strip("\n")
    if not text.strip():
        p.unlink(missing_ok=True)
        return 0
    fm = read_page(p)[0] if p.exists() else {}
    fm.update(title=f"Notes: {get(cfg, slug)['title']}", project=f"reviews/{slug}", type="note",
              updated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    write_page(p, fm, text + "\n")
    return p.stat().st_mtime_ns


def linked_papers(cfg, slug: str) -> list[dict]:
    """Papers linked to the project (stem, title, year, authors), newest first: the notes page's link picker."""
    out = []
    for p in sorted((cfg["vault"] / vault.PAPERS).glob("*.md")):
        fm, _ = read_page(p)
        if slug in (fm.get("reviews") or []) and fm.get("title"):
            au = [str(a) for a in (fm.get("authors") or []) if not str(a).strip().isdigit()]
            out.append({"stem": p.stem, "title": str(fm["title"]), "year": fm.get("year") or "",
                        "authors": (au[0].split()[-1] + (" et al." if len(au) > 1 else "")) if au else ""})
    out.sort(key=lambda r: str(r["year"]), reverse=True)
    return out


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
             "question": _section(rv.get("text", ""), "Research question"), "created": str(rv.get("created") or ""),
             "status": str(rv.get("status", "active"))} for s, rv in vault.reviews(v).items()]


def set_status(cfg, slug: str, status: str) -> None:
    """Archive or reactivate a project (an archived one is skipped by the daily run). Changes only the status field."""
    if status not in ("active", "archived"):
        raise ProjectError("Status must be active or archived.")
    rv = get(cfg, slug)
    fm, body = read_page(rv["path"])
    if str(fm.get("status", "active")) != status:
        fm["status"] = status
        write_page(rv["path"], fm, body)


# ---------- creating and configuring projects from the web app ----------

class ProjectError(ValueError):
    """A project form was filled in wrongly; the message is shown to the user."""


def clean_terms(raw) -> list[str]:
    """Search terms from a textarea (one per line, or comma-separated) or a list: trimmed, de-duplicated, capped."""
    items = re.split(r"[\n,;]+", raw) if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    for t in items:
        t = " ".join(str(t).split())[:60]
        if t and t.lower() not in {o.lower() for o in out}:
            out.append(t)
    return out[:20]


def create(cfg, title: str, question: str = "", scope: str = "", terms=(), exclude: str = "") -> str:
    """Create reviews/<slug>.md from the web form and return its slug."""
    from .common import slugify
    title = " ".join(title.split())
    if not title:
        raise ProjectError("Give the project a title.")
    if len(title) > 120:
        raise ProjectError("Keep the title under 120 characters.")
    slug = slugify(title, 50)
    if slug in vault.reviews(cfg["vault"]):
        raise ProjectError(f"A project called '{slug}' already exists; pick a different title.")
    vault.create_review(cfg["vault"], title, question=question[:4000], scope=scope[:4000],
                        search_terms=clean_terms(terms), exclude=" ".join(exclude.split())[:1000])
    return slug


_PLACEHOLDERS = {"Research question": "_What is this review trying to find out?_",
                 "Scope": "_What is in scope, and what is not. Candidates are scored against this text._"}


def _section(body: str, name: str) -> str:
    return vault.get_section(body, name, _PLACEHOLDERS[name])


def _set_section(body: str, name: str, text: str) -> str:
    return vault.set_section(body, name, text, _PLACEHOLDERS[name])


def _version(question: str, scope: str, terms: list, exclude: str, status: str) -> str:
    """Identifies the editable content. Linking a paper also rewrites the page, so a file mtime would report false
    conflicts; this only changes when one of the fields the form edits changes."""
    return hashlib.sha1(json.dumps([question, scope, terms, exclude, status]).encode()).hexdigest()[:16]


def read_overview(cfg, slug: str) -> dict:
    """Everything the project's Overview form edits, plus the version token the save is checked against."""
    rv = get(cfg, slug)
    _, body = read_page(rv["path"])
    q, sc = _section(body, "Research question"), _section(body, "Scope")
    terms, exclude, status = list(rv.get("search_terms") or []), str(rv.get("exclude") or ""), str(rv.get("status", "active"))
    return {"title": rv["title"], "question": q, "scope": sc, "terms": terms, "exclude": exclude, "status": status,
            "daily": rv.get("daily") is True, "version": _version(q, sc, terms, exclude, status)}


def save_overview(cfg, slug: str, question: str, scope: str, terms, exclude: str, status: str, expected_version: str) -> str:
    """Save the Overview form; frontmatter and the two text sections only, the rest of the page is untouched.
    Raises notes.Conflict if those fields changed since the form was loaded. Returns the new version."""
    if status not in ("active", "archived"):
        raise ProjectError("Status must be active or archived.")
    cur = read_overview(cfg, slug)
    if cur["version"] != expected_version:
        raise Conflict()
    rv = get(cfg, slug)
    fm, body = read_page(rv["path"])
    fm["search_terms"] = clean_terms(terms)
    fm["exclude"] = " ".join(exclude.split())[:1000]
    fm["status"] = status
    body = _set_section(_set_section(body, "Research question", question[:4000]), "Scope", scope[:4000])
    write_page(rv["path"], fm, body)
    return read_overview(cfg, slug)["version"]


SEARCHES: dict[str, dict] = {}   # slug -> {"running", "rc"} for searches started from the web (this process only)


def start_search(cfg, slug: str) -> bool:
    """Run the project's full search now (what `run: true` did in Obsidian) as a detached job; False if one is running.
    The job waits for the pipeline lock, searches arXiv + OpenAlex, triages with the cheap model and writes a digest."""
    if not get(cfg, slug).get("search_terms"):
        raise ProjectError("Add search terms first.")
    if SEARCHES.get(slug, {}).get("running"):
        return False
    SEARCHES[slug] = {"running": True, "rc": None}
    out = open(LOG_DIR / "web-jobs.log", "a")
    p = subprocess.Popen([sys.executable, "-m", "litwiki.cli", "search", slug], stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=ROOT)
    out.close()

    def wait():
        SEARCHES[slug] = {"running": False, "rc": p.wait()}
    threading.Thread(target=wait, daemon=True).start()
    return True


SUGGEST_SYSTEM = """You help a researcher set up the automatic literature search for a new project (a literature review).
From the title and research question, propose:
- terms: 5-8 search phrases of 1-4 words each, as they would appear in paper titles or abstracts (arXiv and OpenAlex
  search). Mix the core topic with close variants; avoid single generic words such as "security" or "learning".
- exclude: one short sentence naming topics or paper types that are probably NOT wanted (empty string if nothing is obvious).
Do not invent details the researcher did not give."""

SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                   "exclude": {"type": "string"}},
    "required": ["terms", "exclude"],
}


def suggest(cfg, title: str, question: str, scope: str = "") -> dict:
    """One cheap Haiku call: starting search terms and exclusions the user then edits."""
    from .llm import claude, pick_model
    if not (title.strip() or question.strip()):
        raise ProjectError("Fill in the title or the research question first.")
    out = claude(cfg, f"TITLE: {title.strip()}\nRESEARCH QUESTION: {question.strip()[:3000]}\nSCOPE: {scope.strip()[:3000]}",
                 system=SUGGEST_SYSTEM, model=pick_model(cfg, "screen"), schema=SUGGEST_SCHEMA,
                 purpose="project-suggest", ref=title[:40])
    return {"terms": clean_terms(out["terms"]), "exclude": " ".join(str(out["exclude"]).split())[:1000]}
