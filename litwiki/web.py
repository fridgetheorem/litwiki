"""Web UI (served at /app/ behind Caddy): add items for ingestion, triage digests, write paper notes.
Everything it does is a plain vault/DB edit that the existing pipeline already understands; the static
site stays read-only and open on the tailnet, while this app needs the shared password (LITWIKI_WEB_PASSWORD)."""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

from itsdangerous import BadSignature, URLSafeTimedSerializer
from jinja2 import DictLoader, Environment, select_autoescape
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from . import manual, notes, vault
from .common import LOG_DIR, ROOT, STATE_DIR, db, load_config, log, read_page, setup_logging
from .discover import tick_items
from .llm import spent

PORT = 8081
SESSION_DAYS = 30
CFG = None  # loaded in create_app


# ---------- auth ----------

def _secret() -> str:
    p = STATE_DIR / "web_secret"
    if not p.exists():
        STATE_DIR.mkdir(exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    return p.read_text().strip()


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt=salt)


def _pwtag() -> str:
    """Changing the password invalidates every session."""
    return hashlib.sha256(os.environ["LITWIKI_WEB_PASSWORD"].encode()).hexdigest()[:12]


def get_session(request: Request) -> dict | None:
    tok = request.cookies.get("lw_session")
    if not tok:
        return None
    try:
        s = _serializer("session").loads(tok, max_age=SESSION_DAYS * 86400)
    except BadSignature:
        return None
    return s if s.get("pw") == _pwtag() else None


_fails: list[float] = []


def _throttled() -> bool:
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < 300]
    return len(_fails) >= 10


# ---------- templates ----------

BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{{ title }} · litwiki</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#1d2330;--mut:#667085;--line:#d9dde5;--acc:#3f51b5;--card:#f6f7fb;--ok:#1b7f3b;--bad:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#14161c;--fg:#e4e7ee;--mut:#98a2b3;--line:#2c313c;--acc:#8c9eff;--card:#1c1f27;--ok:#5fd07f;--bad:#ff8a80}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
nav{display:flex;gap:1rem;flex-wrap:wrap;align-items:center;padding:.6rem 1rem;border-bottom:1px solid var(--line);background:var(--card)}
nav a,nav button{color:var(--acc);text-decoration:none;background:none;border:0;font:inherit;cursor:pointer;padding:0}
nav .sp{flex:1}main{max-width:60rem;margin:0 auto;padding:1rem}
h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.8rem 1rem;margin:.6rem 0}
input[type=text],input[type=password],select,textarea{width:100%;box-sizing:border-box;padding:.5rem;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
textarea{font-family:ui-monospace,monospace;font-size:.95rem}
button.b,a.b{display:inline-block;background:var(--acc);color:var(--bg);border:0;border-radius:6px;padding:.45rem .9rem;font:inherit;cursor:pointer;text-decoration:none}
button.b.sec{background:none;color:var(--acc);border:1px solid var(--acc)}
.row{display:flex;gap:.8rem;flex-wrap:wrap}.row>*{flex:1;min-width:12rem}
.mut{color:var(--mut);font-size:.9rem}.ok{color:var(--ok)}.bad{color:var(--bad)}
.flash{padding:.6rem 1rem;border-radius:6px;margin:.6rem 0;border:1px solid var(--line)}
.flash.ok{border-color:var(--ok)}.flash.err{border-color:var(--bad);color:var(--bad)}
label{display:block;margin:.6rem 0 .2rem;font-weight:600}.item{display:flex;gap:.6rem;align-items:flex-start}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.3rem .5rem;border-bottom:1px solid var(--line);vertical-align:top}
</style>{% if refresh %}<meta http-equiv="refresh" content="{{ refresh }}">{% endif %}</head><body>
{% if authed %}<nav><a href="/app/"><b>litwiki</b></a><a href="/app/inbox">Inbox{% if inbox_n %} ({{ inbox_n }}){% endif %}</a>
<a href="/app/papers">Papers</a><a href="/">Site ↗</a><span class="sp"></span>
<form method="post" action="/app/logout"><input type="hidden" name="_csrf" value="{{ csrf }}"><button>Log out</button></form></nav>{% endif %}
<main>{% if flash %}<div class="flash {{ flash.kind }}">{{ flash.msg }}</div>{% endif %}{% block body %}{% endblock %}</main></body></html>"""

TEMPLATES = {
    "base.html": BASE,
    "login.html": """{% extends "base.html" %}{% block body %}<h1>litwiki</h1>
<form method="post" action="/app/login" class="card"><label for="p">Password</label>
<input id="p" type="password" name="password" autofocus autocomplete="current-password">
<p><button class="b">Log in</button></p></form>{% endblock %}""",
    "dashboard.html": """{% extends "base.html" %}{% block body %}
<h1>Add to the wiki</h1>
<form method="post" action="/app/add" enctype="multipart/form-data" class="card">
<label for="t">DOIs, arXiv IDs or URLs <span class="mut">(one per line)</span></label>
<textarea id="t" name="text" rows="3" placeholder="10.1145/3849807&#10;arXiv:2401.12345&#10;https://example.org/paper.pdf"></textarea>
<label for="f">…or upload PDFs</label><input id="f" type="file" name="files" accept="application/pdf" multiple>
<div class="row"><div><label for="r">Attach to review</label><select id="r" name="review"><option value="">—</option>
{% for s, t in reviews %}<option value="{{ s }}">{{ t }}</option>{% endfor %}</select></div>
<div><label for="c">Add to concept</label><select id="c" name="concept"><option value="">— (let the summary choose)</option>
{% for s, t in concepts %}<option value="{{ s }}">{{ t }}</option>{% endfor %}</select></div></div>
<p><button class="b">Add &amp; ingest</button> <span class="mut">Summaries use the LLM budget (≈$0.10 each).</span></p></form>

<h2>Queue</h2>
{% if items %}<table><tr><th>Item</th><th>Status</th><th></th></tr>
{% for i in items %}<tr><td>{% if i.page %}<a href="/{{ i.page_url }}">{{ i.title }}</a>{% else %}{{ i.title }}{% endif %}
<div class="mut">{{ i.id }}{% if i.error %} · <span class="bad">{{ i.error }}</span>{% endif %}</div></td>
<td class="{{ 'ok' if i.status == 'ingested' else ('bad' if i.status in ('failed','rejected') else '') }}">{{ i.status }}</td>
<td>{% if i.status in ('failed','rejected') %}<form method="post" action="/app/retry"><input type="hidden" name="_csrf" value="{{ csrf }}">
<input type="hidden" name="id" value="{{ i.id }}"><button class="b sec">Retry</button></form>{% endif %}
{% if i.notes_stem %}<a href="/app/notes/{{ i.notes_stem }}">Notes</a>{% endif %}</td></tr>{% endfor %}</table>
<form method="post" action="/app/run"><input type="hidden" name="_csrf" value="{{ csrf }}"><p><button class="b sec">Run ingest now</button>
<span class="mut">Otherwise queued items are picked up hourly.</span></p></form>
{% else %}<p class="mut">Nothing added from the web yet.</p>{% endif %}

<h2>Recent notes</h2>
{% for stem, t in recent_notes %}<div><a href="/app/notes/{{ stem }}">{{ t }}</a></div>{% else %}<p class="mut">No notes yet — open a paper under <a href="/app/papers">Papers</a>.</p>{% endfor %}
<p class="mut">LLM spend this month: ${{ '%.2f'|format(spend) }} of ${{ '%.0f'|format(cap) }}.</p>
{% endblock %}""",
    "inbox.html": """{% extends "base.html" %}{% block body %}<h1>Digest inbox</h1>
{% if not groups %}<p class="mut">No pending digest items.</p>{% endif %}
<form method="post" action="/app/inbox"><input type="hidden" name="_csrf" value="{{ csrf }}">
{% for digest, items in groups %}<h2><a href="/{{ digest|replace('.md','') }}/">{{ digest|replace('digests/','')|replace('.md','') }}</a></h2>
{% for i in items %}<div class="card item">{% if i.ticked %}<span title="approved">⏳</span>{% else %}<input type="checkbox" name="id" value="{{ i.id }}" id="c{{ loop.index }}-{{ digest }}">{% endif %}
<div><b>{{ i.title }}</b> <span class="mut">· {{ i.score }}/10</span>{% if i.ticked %} <span class="ok">approved, ingesting</span>{% endif %}
<div class="mut">{{ i.authors }} — {{ i.venue }}, {{ i.date }}</div><div>{{ i.why }}</div>
<div class="mut"><a href="{{ i.url }}" rel="noreferrer">abstract</a>{% if i.pdf_url %} · <a href="{{ i.pdf_url }}" rel="noreferrer">PDF</a>{% else %} · no open-access PDF (abstract-only summary){% endif %}</div></div></div>{% endfor %}{% endfor %}
{% if groups %}<p><button class="b" name="action" value="approve">Approve &amp; ingest selected</button>
<button class="b sec" name="action" value="dismiss">Dismiss selected</button></p>{% endif %}</form>{% endblock %}""",
    "papers.html": """{% extends "base.html" %}{% block body %}<h1>Papers</h1>
<form method="get" action="/app/papers"><input type="text" name="q" value="{{ q }}" placeholder="Search titles / authors" autofocus></form>
<table>{% for p in papers %}<tr><td><a href="/papers/{{ p.stem }}/">{{ p.title }}</a><div class="mut">{{ p.authors }} {{ p.year }}</div></td>
<td><a href="/app/notes/{{ p.stem }}">{% if p.has_note %}Edit notes{% else %}Add notes{% endif %}</a></td></tr>{% endfor %}</table>
{% if more %}<p class="mut">Showing the first {{ papers|length }}; refine the search.</p>{% endif %}{% endblock %}""",
    "notes.html": """{% extends "base.html" %}{% block body %}<h1>Notes: {{ paper_title }}</h1>
<p><a href="/papers/{{ stem }}/">View paper page</a></p>
<form method="post" action="/app/notes/{{ stem }}"><input type="hidden" name="_csrf" value="{{ csrf }}">
<input type="hidden" name="mtime" value="{{ mtime }}">
<textarea name="text" rows="22" autofocus>{{ text }}</textarea>
<p><button class="b">Save</button> <span class="mut">Markdown; [[wikilinks]] work. Saved to notes/{{ stem }}.md and shown on the paper page after the site rebuilds (~15 s). Saving empty text deletes the note.</span></p></form>
{% endblock %}""",
}
ENV = Environment(loader=DictLoader(TEMPLATES), autoescape=select_autoescape(default=True))


def render(request: Request, name: str, status: int = 200, **ctx) -> Response:
    sess = get_session(request)
    flash = None
    if tok := request.cookies.get("lw_flash"):
        try:
            flash = _serializer("flash").loads(tok, max_age=300)
        except BadSignature:
            pass
    inbox_n = sum(not i["ticked"] for _, g in _inbox_groups() for i in g) if sess else 0
    html = ENV.get_template(name).render(authed=bool(sess), csrf=(sess or {}).get("csrf", ""), flash=flash,
                                         inbox_n=inbox_n, **ctx)
    resp = HTMLResponse(html, status_code=status)
    if flash:
        resp.delete_cookie("lw_flash", path="/app")
    return resp


def redirect(url: str, msg: str = "", kind: str = "ok") -> Response:
    resp = RedirectResponse(url, status_code=303)
    if msg:
        resp.set_cookie("lw_flash", _serializer("flash").dumps({"msg": msg[:1500], "kind": kind}),
                        max_age=300, path="/app", httponly=True, samesite="strict")
    return resp


def guard(fn):
    """Require a logged-in session; POSTs also need the CSRF token (form field _csrf)."""
    async def wrapper(request: Request):
        sess = get_session(request)
        if not sess:
            return RedirectResponse("/app/login", status_code=303)
        if request.method == "POST":
            request.state.form = form = await request.form()
            if not hmac.compare_digest(str(form.get("_csrf", "")), sess["csrf"]):
                return HTMLResponse("Bad CSRF token — reload the page and try again.", status_code=403)
        res = fn(request)
        return await res if asyncio.iscoroutine(res) else res
    return wrapper


# ---------- background work ----------

def spawn_queue() -> None:
    """Run `litwiki web-queue` detached: it waits for the pipeline lock, ingests, rebuilds the site."""
    out = open(LOG_DIR / "web-jobs.log", "a")
    p = subprocess.Popen([sys.executable, "-m", "litwiki.cli", "web-queue"], stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=ROOT)
    threading.Thread(target=p.wait, daemon=True).start()  # reap
    out.close()


_build_lock = threading.Lock()
_build_dirty = False


def request_build() -> None:
    """Rebuild the site in the background; bursts of saves coalesce into one extra build."""
    global _build_dirty
    from .site import build
    _build_dirty = True
    if not _build_lock.acquire(blocking=False):
        return

    def worker():
        global _build_dirty
        try:
            while _build_dirty:
                _build_dirty = False
                try:
                    build(CFG)
                except BaseException:
                    log.exception("web-triggered build failed")
        finally:
            _build_lock.release()
        if _build_dirty:
            request_build()
    threading.Thread(target=worker, daemon=True).start()


# ---------- pages ----------

def _titles(items) -> list[tuple[str, str]]:
    return [(s, fm["title"]) for s, fm in items.items()]


def _inbox_groups() -> list[tuple[str, list[dict]]]:
    con = db()
    v = CFG["vault"]
    from .discover import TICKED
    ticked = set()
    for p in (v / vault.DIGESTS).glob("*.md"):
        ticked.update(TICKED.findall(p.read_text(encoding="utf-8")))
    groups: dict[str, list] = {}
    for r in con.execute("SELECT id, data, digest FROM candidates WHERE status='digested' AND digest!='web' "
                         "ORDER BY digest DESC, score DESC"):
        c = json.loads(r["data"])
        groups.setdefault(r["digest"], []).append({
            "id": r["id"], "title": c["title"], "score": c.get("score", ""), "why": c.get("why", ""),
            "authors": ", ".join(c.get("authors", [])[:3]) + (" et al." if len(c.get("authors", [])) > 3 else ""),
            "venue": c.get("venue", ""), "date": c.get("date", ""), "url": c.get("url", ""),
            "pdf_url": c.get("pdf_url", ""), "ticked": r["id"] in ticked})
    return list(groups.items())


def dashboard(request: Request) -> Response:
    v, con = CFG["vault"], db()
    items = []
    for r in con.execute("SELECT id, title, status, data FROM candidates WHERE digest='web' "
                         "ORDER BY seen DESC, rowid DESC LIMIT 30"):
        c = json.loads(r["data"])
        page = c.get("page") if r["status"] == "ingested" else None
        items.append({"id": r["id"], "title": r["title"], "status": r["status"], "error": c.get("error"),
                      "page": page, "page_url": quote(page.removesuffix(".md")) + "/" if page else "",
                      "notes_stem": Path(page).stem if page else ""})
    busy = any(i["status"] == "queued" for i in items)
    return render(request, "dashboard.html", title="Dashboard", items=items, reviews=_titles(vault.reviews(v)),
                  concepts=_titles(vault.concepts(v)), recent_notes=notes.recent(v), spend=spent(con, "month"),
                  cap=CFG["budget"]["monthly_usd"], refresh=15 if busy else None)


async def add(request: Request) -> Response:
    form = request.state.form
    review, concept = str(form.get("review", "")), str(form.get("concept", ""))
    queued, errors = 0, []
    lines = [l.strip() for l in str(form.get("text", "")).splitlines() if l.strip()]
    for line in lines:
        try:
            await run_in_threadpool(manual.add_text, CFG, line, review, concept)
            queued += 1
        except manual.AddError as e:
            errors.append(f"{line[:60]}: {e}")
    for f in form.getlist("files"):
        if not getattr(f, "filename", ""):
            continue
        try:
            data = await f.read(manual.MAX_UPLOAD + 1)
            await run_in_threadpool(manual.add_pdf, CFG, f.filename, data, review, concept)
            queued += 1
        except manual.AddError as e:
            errors.append(f"{f.filename}: {e}")
    if queued:
        spawn_queue()
    if not lines and queued == 0 and not errors:
        errors.append("Nothing to add.")
    msg = (f"Queued {queued} item(s); ingesting now. " if queued else "") + " ".join(errors)
    return redirect("/app/", msg, "err" if errors else "ok")


def retry(request: Request) -> Response:
    form = request.state.form
    manual.retry(CFG, str(form.get("id", "")))
    spawn_queue()
    return redirect("/app/", "Requeued; ingesting now.")


def run_now(request: Request) -> Response:
    spawn_queue()
    return redirect("/app/", "Ingest started.")


def inbox(request: Request) -> Response:
    return render(request, "inbox.html", title="Inbox", groups=_inbox_groups())


def inbox_action(request: Request) -> Response:
    form = request.state.form
    ids, action = form.getlist("id"), form.get("action")
    if not ids:
        return redirect("/app/inbox", "Nothing selected.", "err")
    if action == "approve":
        n = tick_items(CFG, ids)
        if n:
            spawn_queue()
        return redirect("/app/inbox", f"Approved {n} item(s); ingesting now.")
    con = db()
    con.executemany("UPDATE candidates SET status='rejected' WHERE id=? AND status='digested'", [(i,) for i in ids])
    con.commit()
    return redirect("/app/inbox", f"Dismissed {len(ids)} item(s).")


def papers(request: Request) -> Response:
    v = CFG["vault"]
    q = request.query_params.get("q", "").lower().strip()
    out, more = [], False
    for p in sorted((v / vault.PAPERS).glob("*.md")):
        fm, _ = read_page(p)
        hay = f"{fm.get('title', '')} {' '.join(map(str, fm.get('authors') or []))} {fm.get('year', '')}".lower()
        if q and not all(w in hay for w in q.split()):
            continue
        if len(out) >= 100:
            more = True
            break
        out.append({"stem": p.stem, "title": fm.get("title", p.stem), "year": fm.get("year") or "",
                    "authors": ", ".join(map(str, (fm.get("authors") or [])[:3])),
                    "has_note": (v / notes.NOTES / p.name).exists()})
    return render(request, "papers.html", title="Papers", papers=out, q=request.query_params.get("q", ""), more=more)


def note_get(request: Request) -> Response:
    stem = request.path_params["stem"]
    try:
        pfm, _ = read_page(notes.paper_path(CFG["vault"], stem))
    except FileNotFoundError:
        return HTMLResponse("No such paper.", status_code=404)
    text, mtime = notes.read_note(CFG["vault"], stem)
    return render(request, "notes.html", title="Notes", paper_title=pfm.get("title", stem), stem=stem,
                  text=text, mtime=mtime)


def note_post(request: Request) -> Response:
    stem, form = request.path_params["stem"], request.state.form
    try:
        notes.save_note(CFG["vault"], stem, str(form.get("text", "")), int(form.get("mtime") or 0))
    except FileNotFoundError:
        return HTMLResponse("No such paper.", status_code=404)
    except notes.Conflict:
        return redirect(f"/app/notes/{stem}", "This note changed elsewhere (e.g. in Obsidian) since you opened "
                        "it. Your edit was NOT saved: reload to see the current text, then redo it.", "err")
    request_build()
    return redirect(f"/app/notes/{stem}", "Saved. The site is rebuilding.")


# ---------- login ----------

def login_get(request: Request) -> Response:
    if get_session(request):
        return RedirectResponse("/app/", status_code=303)
    return render(request, "login.html", title="Log in")


async def login_post(request: Request) -> Response:
    if _throttled():
        return HTMLResponse("Too many failed attempts; wait a few minutes.", status_code=429)
    form = await request.form()
    ok = hmac.compare_digest(str(form.get("password", "")).encode(), os.environ["LITWIKI_WEB_PASSWORD"].encode())
    if not ok:
        _fails.append(time.time())
        log.warning("failed login from %s", request.client.host if request.client else "?")
        await asyncio.sleep(1)
        return HTMLResponse(ENV.get_template("login.html").render(
            authed=False, flash={"kind": "err", "msg": "Wrong password."}, csrf="", inbox_n=0, title="Log in"),
            status_code=401)
    tok = _serializer("session").dumps({"pw": _pwtag(), "csrf": secrets.token_urlsafe(24)})
    resp = RedirectResponse("/app/", status_code=303)
    resp.set_cookie("lw_session", tok, max_age=SESSION_DAYS * 86400, path="/app", httponly=True, samesite="strict")
    return resp


def logout(request: Request) -> Response:
    resp = RedirectResponse("/app/login", status_code=303)
    resp.delete_cookie("lw_session", path="/app")
    return resp


def create_app(cfg) -> Starlette:
    global CFG
    CFG = cfg
    return Starlette(routes=[
        Route("/app", lambda r: RedirectResponse("/app/", status_code=308)),
        Route("/app/", guard(dashboard)),
        Route("/app/login", login_get, methods=["GET"]),
        Route("/app/login", login_post, methods=["POST"]),
        Route("/app/logout", guard(logout), methods=["POST"]),
        Route("/app/add", guard(add), methods=["POST"]),
        Route("/app/retry", guard(retry), methods=["POST"]),
        Route("/app/run", guard(run_now), methods=["POST"]),
        Route("/app/inbox", guard(inbox), methods=["GET"]),
        Route("/app/inbox", guard(inbox_action), methods=["POST"]),
        Route("/app/papers", guard(papers)),
        Route("/app/notes/{stem}", guard(note_get), methods=["GET"]),
        Route("/app/notes/{stem}", guard(note_post), methods=["POST"]),
    ])


def serve(cfg) -> None:
    import uvicorn
    if not os.environ.get("LITWIKI_WEB_PASSWORD"):
        raise SystemExit("LITWIKI_WEB_PASSWORD is not set in ~/.env")
    os.environ["PATH"] = f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"  # for `claude` in jobs
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
