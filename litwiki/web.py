"""Web UI (served at /app/ behind Caddy): add items for ingestion, triage digests, write paper notes.
Everything it does is a plain vault/DB edit that the existing pipeline already understands; the static
site stays read-only and open on the tailnet, while this app needs the shared password (LITWIKI_WEB_PASSWORD)."""
import asyncio
import hashlib
import hmac
import json
import os
import re
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
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import ask as askmod, chat, concept, exclusions, manual, notes, project, swipe, vault
from .common import LOG_DIR, ROOT, STATE_DIR, db, load_config, log, read_page, setup_logging
from .discover import tick_items
from .llm import spent

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

# The one colour palette for every page (light + dark). The two standalone project pages get it injected, so they cannot drift.
THEME_VARS = (":root{color-scheme:light dark;--bg:#fff;--fg:#1d2330;--mut:#667085;--line:#d9dde5;--acc:#3f51b5;--accfg:#fff;--card:#f6f7fb;"
              "--ok:#1b7f3b;--bad:#b3261e;--new:#b45309;--lnk:#6d28d9;--sel:#3f51b522}\n"
              "@media(prefers-color-scheme:dark){:root{--bg:#14161c;--fg:#e4e7ee;--mut:#98a2b3;--line:#2c313c;--acc:#8c9eff;--accfg:#14161c;--card:#1c1f27;"
              "--ok:#5fd07f;--bad:#ff8a80;--new:#fbbf24;--lnk:#c4b5fd;--sel:#8c9eff26}}")

BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{{ title }} · litwiki</title>
<style>
__THEME__
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
{% if authed %}<nav><a href="/app/"><b>litwiki</b></a><a href="/app/inbox">Inbox{% if inbox_n %} ({{ inbox_n }}){% endif %}</a><a href="/app/swipe">Swipe</a><a href="/app/projects">Projects</a><a href="/app/concepts">Concepts</a>
<a href="/app/ask">Ask</a><a href="/app/papers">Papers</a><a href="/clusters/">Map</a><a href="/">Wiki</a><a href="/app/settings">Settings</a><span class="sp"></span>
<form method="post" action="/app/logout"><input type="hidden" name="_csrf" value="{{ csrf }}"><button>Log out</button></form></nav>{% endif %}
<main>{% if flash %}<div class="flash {{ flash.kind }}">{{ flash.msg }}</div>{% endif %}{% block body %}{% endblock %}</main></body></html>""".replace("__THEME__", THEME_VARS)

TEMPLATES = {
    "base.html": BASE,
    "login.html": """{% extends "base.html" %}{% block body %}<h1>litwiki</h1>
<form method="post" action="/app/login" class="card"><label for="p">Password</label>
<input id="p" type="password" name="password" autofocus autocomplete="current-password">
<p><button class="b">Log in</button></p></form>{% endblock %}""",
    "dashboard.html": """{% extends "base.html" %}{% block body %}
<h1>Add to the wiki</h1>
<form method="post" action="/app/add" enctype="multipart/form-data" class="card"><input type="hidden" name="_csrf" value="{{ csrf }}">
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
<td><a href="/app/notes/{{ p.stem }}">{% if p.has_note %}Edit notes{% else %}Add notes{% endif %}</a>{% if p.chat %} · <a href="/app/paper/{{ p.stem }}/chat">Chat</a>{% endif %}</td></tr>{% endfor %}</table>
{% if more %}<p class="mut">Showing the first {{ papers|length }}; refine the search.</p>{% endif %}{% endblock %}""",
    "ask.html": """{% extends "base.html" %}{% block body %}<h1>Ask the library</h1>
<form method="post" action="/app/ask" class="card"><input type="hidden" name="_csrf" value="{{ csrf }}">
<label for="q">Question</label><textarea id="q" name="question" rows="3" autofocus placeholder="What major topics do my papers share?&#10;Which papers evaluate on real-world protocol implementations?"></textarea>
<div class="row"><div><label for="c">Only concept</label><select id="c" name="concept"><option value="">all papers</option>
{% for s, t in concepts %}<option value="{{ s }}">{{ t }}</option>{% endfor %}</select></div>
<div><label for="r">Only review</label><select id="r" name="review"><option value="">—</option>
{% for s, t in reviews %}<option value="{{ s }}">{{ t }}</option>{% endfor %}</select></div>
<div><label for="y">Year from</label><input id="y" type="text" name="since" inputmode="numeric" placeholder="e.g. 2022"></div></div>
<p><button class="b">Ask</button> <span class="mut">Reads every matching paper's TL;DR (and findings, limitations and your notes for up to 30 papers if it needs detail). ≈ $0.15–0.30 per question.</span></p></form>
<h2>Recent questions</h2>
{% for a in recent %}<div><a href="/app/ask/{{ a.id }}">{{ a.question[:110] }}</a> <span class="mut">· {{ a.status }}{% if a.scope %} · {{ a.scope }}{% endif %} · {{ a.ts[:16] }}</span></div>
{% else %}<p class="mut">No questions yet.</p>{% endfor %}{% endblock %}""",
    "ask_result.html": """{% extends "base.html" %}{% block body %}<p><a href="/app/ask">← Ask another</a></p>
<h1>{{ a.question }}</h1><p class="mut">{% if a.scope %}{{ a.scope }} · {% endif %}{{ a.ts[:16] }} UTC</p>
{% if a.status == 'running' %}<div class="card">Reading the library… this page refreshes itself.</div>
{% elif a.status == 'error' %}<div class="flash err">{{ a.error }}</div>
{% else %}<div class="card answer">{{ html|safe }}</div>
<p class="mut">{{ a.n_papers }} papers read · ${{ '%.2f'|format(a.cost) }}</p>{% endif %}
<style>.answer a{color:var(--acc)}.answer h1,.answer h2,.answer h3{font-size:1.05rem;margin:1rem 0 .3rem}</style>{% endblock %}""",
    "attach.html": """{% extends "base.html" %}{% block body %}<h1>Attach a PDF: {{ paper_title }}</h1>
<p>{% if stem %}<a href="/papers/{{ stem }}/">View paper page</a> · {% if has_pdf %}this page has a PDF{% else %}this page has <b>no PDF</b>{% endif %}{% else %}This Zotero item has no page yet; attaching a PDF creates it{% endif %}{% if abstract_only %} and was summarised from the abstract only{% endif %}.</p>
<form method="post" action="/app/attach/{{ ref }}" enctype="multipart/form-data" class="card"><input type="hidden" name="_csrf" value="{{ csrf }}">
<label for="f">PDF file (max 60 MB, must contain selectable text)</label>
<input id="f" type="file" name="file" accept="application/pdf" required>
{% if edited %}<p class="flash err">You edited this page by hand (status: {{ page_status }}). Re-summarising replaces the summary text.
<label><input type="checkbox" name="force" value="1"> Overwrite my edits</label></p>{% endif %}
<p><button class="b">Attach and re-summarise</button>
<span class="mut">Replaces the generated summary with one written from the full text (about $0.05-0.15). Tags, collections, concepts, reviews and your notes are kept.
It runs in the background and the page updates when the site rebuilds (about a minute).</span></p></form>
{% endblock %}""",
    "projects.html": """{% extends "base.html" %}{% block body %}<h1>Projects</h1>
<p class="mut">A project is a literature review that grows over time: link papers you already have, screen new candidates in bulk, and search for more. Open one to edit its question, scope and search terms, run a search, and work with its papers and notes.</p>
<p><a class="b" href="/app/projects/new">New project</a></p>
{% if not projects %}<p class="mut">No projects yet.</p>{% endif %}
{% for p in projects %}<div class="card"><b><a href="/app/project/{{ p.slug }}">{{ p.title }}</a></b>
<div class="mut">{{ p.linked }} linked paper(s) · {{ p.waiting }} new candidate(s) waiting · {{ p.terms }} search term(s)</div>
<div><a href="/app/project/{{ p.slug }}">Overview</a> · <a href="/app/project/{{ p.slug }}/papers">Papers</a> · <a href="/app/project/{{ p.slug }}/notes">Notes</a> · <a href="/reviews/{{ p.slug }}/">Wiki page</a></div>
<form method="post" action="/app/project/{{ p.slug }}/daily" style="margin-top:.4rem"><input type="hidden" name="_csrf" value="{{ csrf }}">
<input type="hidden" name="on" value="{{ '0' if p.daily else '1' }}"><button class="b {{ 'sec' if p.daily else '' }}">{{ 'Daily digest: ON (turn off)' if p.daily else 'Turn on daily digest' }}</button>
<span class="mut">{% if p.status != 'active' %}archived projects are skipped{% elif p.daily %}searched every day{% else %}manual searches only{% endif %}</span></form></div>{% endfor %}{% endblock %}""",
    "project_form.html": """{% extends "base.html" %}{% block body %}
<p><a href="/app/projects">← Projects</a></p>
<h1>New project</h1>
<form method="post" action="{{ action }}" class="card" id="pf"><input type="hidden" name="_csrf" value="{{ csrf }}">
<label for="title">Title</label><input id="title" type="text" name="title" value="{{ f.title }}" maxlength="120" required autofocus>
<label for="question">Research question <span class="mut">(what is this review trying to find out?)</span></label>
<textarea id="question" name="question" rows="3">{{ f.question }}</textarea>
<label for="scope">Scope <span class="mut">(what is in, and what is out; candidates are scored against this)</span></label>
<textarea id="scope" name="scope" rows="3">{{ f.scope }}</textarea>
<label for="terms">Search terms <span class="mut">(one per line, 1-4 words each; searched on arXiv and OpenAlex)</span></label>
<textarea id="terms" name="terms" rows="7">{{ f.terms }}</textarea>
<label for="exclude">Exclusions <span class="mut">(topics or paper types to score down, in plain words)</span></label>
<textarea id="exclude" name="exclude" rows="2">{{ f.exclude }}</textarea>
<p><button type="button" class="b sec" id="suggest">Suggest terms</button>
<span class="mut" id="smsg">Fills the two boxes above from your title and question (one small LLM call, about a cent). You can edit the result.</span></p>
<p><button class="b">Create project</button></p></form>
<script>
const q=s=>document.querySelector(s), btn=q('#suggest');
btn.onclick=async()=>{btn.disabled=true;q('#smsg').textContent='Thinking…';
 try{const body=new URLSearchParams({_csrf:q('input[name=_csrf]').value,title:q('#title').value,question:q('#question').value,scope:q('#scope').value});
  const r=await fetch('/app/projects/suggest',{method:'POST',body}); const j=await r.json();
  if(!r.ok)throw new Error(j.error||r.status);
  q('#terms').value=j.terms.join('\\n'); q('#exclude').value=j.exclude; q('#smsg').textContent='Suggested: edit freely before creating.';
 }catch(e){q('#smsg').textContent='Could not suggest: '+e.message;} btn.disabled=false;};
</script>{% endblock %}""",
    "project_overview.html": """{% extends "base.html" %}{% block body %}
<p><a href="/app/projects">← Projects</a></p><h1>{{ f.title }}</h1>{{ tabs|safe }}
<p class="mut">{{ linked }} linked paper(s){% if f.status != 'active' %} · <b>archived</b> (skipped by the daily run){% endif %}.
Link or screen papers under <a href="/app/project/{{ slug }}/papers">Papers</a>; keep free-form thoughts under <a href="/app/project/{{ slug }}/notes">Notes</a>.</p>
<form method="post" action="/app/project/{{ slug }}/overview" class="card"><input type="hidden" name="_csrf" value="{{ csrf }}">
<input type="hidden" name="version" value="{{ f.version }}">
<label for="question">Research question <span class="mut">(what is this review trying to find out?)</span></label>
<textarea id="question" name="question" rows="3">{{ f.question }}</textarea>
<label for="scope">Scope <span class="mut">(what is in, and what is out; candidates are scored against this text)</span></label>
<textarea id="scope" name="scope" rows="5">{{ f.scope }}</textarea>
<div class="row"><div><label for="terms">Search terms <span class="mut">(one per line, 1-4 words)</span></label>
<textarea id="terms" name="terms" rows="7">{{ f.terms_text }}</textarea></div>
<div><label for="exclude">Exclusions <span class="mut">(scored down, in plain words)</span></label>
<textarea id="exclude" name="exclude" rows="3">{{ f.exclude }}</textarea>
<label for="status">Status</label><select id="status" name="status">
<option value="active"{% if f.status == 'active' %} selected{% endif %}>active</option>
<option value="archived"{% if f.status == 'archived' %} selected{% endif %}>archived (skipped by the daily run)</option></select></div></div>
<p><button class="b">Save</button> <span class="mut">Changes the wiki page too (its other content is left alone).</span></p></form>
<h2>Searching</h2>
<div class="card">
<form method="post" action="/app/project/{{ slug }}/search" style="display:inline"><input type="hidden" name="_csrf" value="{{ csrf }}">
<button class="b" id="srch"{% if searching %} disabled{% endif %}>Run search now</button></form>
<span class="mut" id="smsg">{% if searching %}Searching…{% elif search_rc == 0 %}Last search finished. New candidates are under <a href="/app/project/{{ slug }}/papers">Papers → New</a>.{% elif search_rc %}Last search failed (see logs/web-jobs.log).{% else %}Searches arXiv and OpenAlex with the terms above and scores the results (a few cents). Saved changes to the terms apply.{% endif %}</span>
<form method="post" action="/app/project/{{ slug }}/daily" style="margin-top:.6rem"><input type="hidden" name="_csrf" value="{{ csrf }}">
<input type="hidden" name="back" value="/app/project/{{ slug }}"><input type="hidden" name="on" value="{{ '0' if f.daily else '1' }}">
<button class="b {{ 'sec' if f.daily else '' }}">{{ 'Daily digest: ON (turn off)' if f.daily else 'Turn on daily digest' }}</button>
<span class="mut">{% if f.status != 'active' %}archived projects are skipped{% elif f.daily %}searched every day, recent papers only{% else %}manual searches only{% endif %}</span></form></div>
{% if searching %}<script>(function p(){fetch('/app/project/{{ slug }}/search-status').then(r=>r.json()).then(j=>{if(j.running)setTimeout(p,5000);else location.reload()}).catch(()=>setTimeout(p,10000))})()</script>{% endif %}
{% endblock %}""",
    "concepts.html": """{% extends "base.html" %}{% block body %}<h1>Concepts</h1>
<p class="mut">Concepts are the topics your papers are filed under. Each one has search terms the daily digest looks for, and an overview you write.</p>
<p><a class="b" href="/app/concepts/new">New concept</a></p>
<table><tr><th>Concept</th><th>Papers</th><th>Terms</th><th>Daily search</th></tr>
{% for c in concepts %}<tr><td><a href="/app/concepts/{{ c.slug }}">{{ c.title }}</a><div class="mut">{{ c.description[:140] }}</div></td>
<td>{{ c.papers }}</td><td>{{ c.terms }}</td><td class="{{ '' if c.discover else 'mut' }}">{{ 'on' if c.discover else 'off' }}</td></tr>
{% else %}<tr><td colspan="4" class="mut">No concepts yet.</td></tr>{% endfor %}</table>{% endblock %}""",
    "concept_form.html": """{% extends "base.html" %}{% block body %}<p><a href="/app/concepts">← Concepts</a></p>
<h1>{% if mode == 'new' %}New concept{% else %}{{ f.title }}{% endif %}</h1>
<form method="post" action="{{ action }}" class="card"><input type="hidden" name="_csrf" value="{{ csrf }}">
{% if mode == 'edit' %}<input type="hidden" name="version" value="{{ f.version }}">{% endif %}
<label for="title">Title</label><input id="title" type="text" name="title" value="{{ f.title }}" maxlength="100" required>
<label for="description">Description <span class="mut">(one sentence; the scoring model sees it)</span></label>
<textarea id="description" name="description" rows="2">{{ f.description }}</textarea>
<div class="row"><div><label for="terms">Search terms <span class="mut">(one per line, 1-4 words)</span></label>
<textarea id="terms" name="terms" rows="6">{{ f.terms_text }}</textarea></div>
<div><label for="categories">arXiv categories <span class="mut">(optional, e.g. cs.CR cs.SE)</span></label>
<input id="categories" type="text" name="categories" value="{{ f.categories_text }}">
{% if mode == 'edit' %}<label for="aliases">Also known as <span class="mut">(one per line; used to match concepts by name)</span></label>
<textarea id="aliases" name="aliases" rows="3">{{ f.aliases_text }}</textarea>{% endif %}</div></div>
{% if mode == 'edit' %}<label><input type="checkbox" name="discover" value="1"{% if f.discover %} checked{% endif %}> Search for new papers on this concept in the daily digest</label>
<label for="overview">Overview <span class="mut">(your synthesis; markdown. The paper list below it is maintained by litwiki)</span></label>
<textarea id="overview" name="overview" rows="10">{{ f.overview }}</textarea>{% endif %}
<p><button class="b">{% if mode == 'new' %}Create concept{% else %}Save{% endif %}</button></p></form>{% endblock %}""",
    "chat_panel.html": """<div id="chat" data-scope="{{ scope }}" data-csrf="{{ csrf }}">
<div id="msgs" aria-live="polite"></div>
<form id="cf"><textarea id="cq" rows="2" placeholder="{{ placeholder }}" maxlength="2000"></textarea>
<div class="crow"><button class="b" id="cs">Ask</button><span class="mut" id="chint">Enter to send, Shift+Enter for a new line.</span></div></form></div>
<style>
#chat{display:flex;flex-direction:column;gap:.8rem}#msgs{display:flex;flex-direction:column;gap:.7rem}
.m{border:1px solid var(--line);border-radius:10px;padding:.6rem .9rem;max-width:100%}.m.u{background:var(--card);align-self:flex-end;max-width:85%;white-space:pre-wrap}
.m.a{background:var(--bg)}.m.err{border-color:var(--bad);color:var(--bad)}.m .ans p{margin:.4rem 0}.m .ans{overflow-wrap:anywhere}
.cite{color:var(--acc);text-decoration:none;font-weight:600}.qs{margin:.6rem 0 0;padding:0;list-style:none;display:flex;flex-direction:column;gap:.45rem}
.qs li{border-left:3px solid var(--acc);padding:.2rem .6rem;background:var(--card);border-radius:0 6px 6px 0;font-size:.92rem}
.qs q{font-style:normal}.qs q:before,.qs q:after{content:'"'}.qs .src{display:block;color:var(--mut);font-size:.85rem;margin-top:.15rem}
.qs li:target{outline:2px solid var(--acc)}.note{color:var(--mut);font-size:.85rem;margin-top:.4rem}
#chat textarea{resize:vertical}.crow{display:flex;gap:.7rem;align-items:center;margin-top:.4rem}
</style>
<script>
(function(){
const root=document.getElementById("chat"),scope=root.dataset.scope,csrf=root.dataset.csrf,msgs=document.getElementById("msgs");
const q=document.getElementById("cq"),btn=document.getElementById("cs"),hint=document.getElementById("chint");
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
let last=0,timer=null,busy=false;
function setBusy(b){busy=b;btn.disabled=b;hint.textContent=b?"Reading the papers… the first question can take a minute while text is extracted.":"Enter to send, Shift+Enter for a new line."}
function bubble(m){
  const d=m.data||{};
  if(m.role==="user"){const e=el("div","m u",m.text);e.dataset.id=m.id;return e}
  const e=el("div","m a");e.dataset.id=m.id;
  if(m.status==="running"){e.textContent="Thinking…";e.classList.add("run");return e}
  if(m.status==="error"){e.className="m a err";e.textContent=m.text;return e}
  const ans=el("div","ans");ans.innerHTML=m.text;   // server-escaped markdown; the only markup is the cite links
  ans.querySelectorAll("a.cite").forEach(a=>{a.href="#c"+m.id+"-"+a.textContent.replace(/\D/g,"")});
  e.append(ans);
  if((d.quotes||[]).length){
    const ol=el("ol","qs");
    d.quotes.forEach(x=>{
      const li=el("li");li.id="c"+m.id+"-"+x.n;
      const body=el("q");body.textContent=x.quote;
      const src=el("span","src");src.append("["+x.n+"] ");
      const a=el("a",null,x.label+", p. "+x.page);a.href=x.pdf;a.target="_blank";a.rel="noopener";src.append(a);
      src.append(" · ");const w=el("a",null,"paper page");w.href=x.wiki;src.append(w);
      li.append(body,src);ol.append(li)});
    e.append(ol)}
  const notes=[];
  if(d.dropped)notes.push(d.dropped+" quote"+(d.dropped>1?"s":"")+" could not be found in the paper"+(d.n_papers>1?"s":"")+" and "+(d.dropped>1?"were":"was")+" removed.");
  if((d.skipped||[]).length)notes.push(d.skipped.length+" paper"+(d.skipped.length>1?"s":"")+" without readable PDF text "+(d.skipped.length>1?"were":"was")+" not searched.");
  if(d.cost)notes.push("$"+d.cost.toFixed(3));
  if(notes.length)e.append(el("div","note",notes.join(" · ")));
  return e}
function put(m){const old=msgs.querySelector('[data-id="'+m.id+'"]');const b=bubble(m);if(old)old.replaceWith(b);else msgs.append(b);if(m.id>last)last=m.id;return b}
async function load(first){
  const r=await fetch("/app/chat/messages?scope="+encodeURIComponent(scope)+"&after=0");
  const j=await r.json();msgs.textContent="";last=0;(j.messages||[]).forEach(put);
  if((j.messages||[]).some(m=>m.status==="running")){setBusy(true);poll()}
  if(first)window.scrollTo(0,document.body.scrollHeight)}
function poll(){clearTimeout(timer);timer=setTimeout(async()=>{
  try{const r=await fetch("/app/chat/messages?scope="+encodeURIComponent(scope)+"&after=0");const j=await r.json();
    (j.messages||[]).forEach(put);
    if((j.messages||[]).some(m=>m.status==="running")){poll()}else{setBusy(false)}
  }catch(e){poll()}},2500)}
async function send(){
  const text=q.value.trim();if(!text||busy)return;setBusy(true);
  const body=new URLSearchParams({_csrf:csrf,scope,question:text});
  try{const r=await fetch("/app/chat/send",{method:"POST",body});const j=await r.json();
    if(!r.ok){setBusy(false);const e=el("div","m a err",j.error||("Error "+r.status));msgs.append(e);return}
    q.value="";await load(false);poll();window.scrollTo(0,document.body.scrollHeight)
  }catch(e){setBusy(false);msgs.append(el("div","m a err","Could not reach the server: "+e.message))}}
document.getElementById("cf").addEventListener("submit",ev=>{ev.preventDefault();send()});
q.addEventListener("keydown",ev=>{if(ev.key==="Enter"&&!ev.shiftKey){ev.preventDefault();send()}});
load(true).catch(e=>msgs.append(el("div","m a err","Could not load the conversation: "+e.message)));
})();
</script>""",
    "project_chat.html": """{% extends "base.html" %}{% block body %}
<p><a href="/app/projects">← Projects</a></p><h1>{{ title_ }}</h1>{{ tabs|safe }}
<p class="mut">Ask about the {{ n_pdf }} linked paper(s) with a PDF. Every answer is backed by quotes copied from the papers and checked by the server;
anything it cannot find is removed. Page numbers are PDF page numbers.
{% if skipped %}<br>Not searched (no PDF): {{ skipped|join('; ') }}.{% endif %}</p>
{% if n_pdf %}{% include "chat_panel.html" %}{% else %}<div class="card">No linked paper has a PDF yet. Link papers under <a href="/app/project/{{ slug }}/papers">Papers</a>,
or attach PDFs to the ones you have.</div>{% endif %}{% endblock %}""",
    "paper_chat.html": """{% extends "base.html" %}{% block body %}
<p><a href="/papers/{{ stem }}/">← {{ paper_title }}</a>{% if pdf %} · <a href="/{{ pdf }}" target="_blank" rel="noopener">Open PDF</a>{% endif %}</p>
<h1>Chat: {{ paper_title }}</h1>
{% if pdf %}<p class="mut">Answers are backed by quotes copied from this paper and checked by the server; anything it cannot find is removed.
Each quote links to its PDF page. Your conversation about this paper is kept here.</p>{% include "chat_panel.html" %}
{% else %}<div class="card">This paper has no PDF, so there is nothing to quote from. <a href="/app/attach/{{ stem }}">Attach a PDF</a> to chat about it.</div>{% endif %}{% endblock %}""",
    "settings.html": """{% extends "base.html" %}{% block body %}<h1>Settings</h1>
<h2>Daily digest: excluded topics</h2>
<p class="mut">The general daily digest skips these. (Projects have their own exclusions under Projects → Settings.)
Papers dropped by a keyword are not shown anywhere, so keep keywords specific.</p>
<form method="post" action="/app/settings" class="card"><input type="hidden" name="_csrf" value="{{ csrf }}">
<label for="kw">Keywords <span class="mut">(one per line; whole words, any case; a paper whose title or abstract contains one is dropped before scoring)</span></label>
<textarea id="kw" name="keywords" rows="6" placeholder="blockchain&#10;smart contract">{{ f.keywords }}</textarea>
<label for="tp">Topics <span class="mut">(plain words; the scoring model rates papers mainly about these 0-2; catches papers that never use your keyword)</span></label>
<textarea id="tp" name="topics" rows="2" placeholder="quantum computing; hardware side channels">{{ f.topics }}</textarea>
{% if fixed.topics or fixed.keywords %}<p class="mut">Also set by the host in config.yaml (read-only here):
{% if fixed.keywords %}keywords: {{ fixed.keywords|join(', ') }}. {% endif %}{% if fixed.topics %}topics: {{ fixed.topics }}.{% endif %}</p>{% endif %}
<p><button class="b">Save</button> <span class="mut">Takes effect at the next daily run.</span></p></form>
{% if dropped %}<h2>Recently dropped</h2><p class="mut">The latest papers removed by keyword, so you can spot a keyword that is too broad.</p>
{% for d in dropped %}<div>{{ d.title }} <span class="mut">· matched “{{ d.kw }}”</span></div>{% endfor %}{% endif %}{% endblock %}""",
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
    flash = ctx.pop("flash", None)  # a form error shown on this response
    if flash is None and (tok := request.cookies.get("lw_flash")):
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

spawn_queue = manual.spawn_queue


_build_lock = threading.Lock()
_build_dirty = False


def request_build() -> None:
    """Rebuild the site in the background; bursts of saves coalesce into one extra build."""
    global _build_dirty
    _build_dirty = True
    if not _build_lock.acquire(blocking=False):
        return

    def worker():
        global _build_dirty
        try:
            while _build_dirty:
                _build_dirty = False
                try:
                    # A fresh process each time: this long-running server keeps old modules in memory, and a
                    # build with stale code once overwrote a newer site with an older template.
                    with open(LOG_DIR / "web-jobs.log", "a") as out:
                        r = subprocess.run([sys.executable, "-m", "litwiki.cli", "build"], stdout=out, stderr=out,
                                           stdin=subprocess.DEVNULL, cwd=ROOT, timeout=900)
                    if r.returncode:
                        log.error("web-triggered build exited with %s (see logs/web-jobs.log)", r.returncode)
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


def swipe_get(request: Request) -> Response:
    """Full-screen swipe deck of pending digest items (the page is a static template; the deck is embedded)."""
    data = {"deck": swipe.deck(CFG), "pending": swipe.pending()}
    page = Path(__file__).with_name("swipe_page.html").read_text(encoding="utf-8")
    token = json.dumps(get_session(request)["csrf"])[1:-1]
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = page.replace("__CSRF__", token).replace("__DATA__", blob)  # data last: titles can't inject placeholders
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


async def swipe_decide(request: Request) -> Response:
    form = request.state.form
    try:
        return JSONResponse(await run_in_threadpool(swipe.decide, str(form.get("id", "")), str(form.get("action", ""))))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)


async def swipe_commit(request: Request) -> Response:
    r = await run_in_threadpool(swipe.commit, CFG)
    if r["ingest"]:
        spawn_queue()
    return JSONResponse(r)


def projects_list(request: Request) -> Response:
    return render(request, "projects.html", title="Projects", projects=project.summary(CFG))


def _project_or_404(request: Request):
    slug = request.path_params["slug"]
    try:
        return slug, project.get(CFG, slug)
    except KeyError:
        return slug, None


def project_papers(request: Request) -> Response:
    """Grid of cards for bulk screening: library papers, new candidates and linked papers of one project."""
    slug, rv = _project_or_404(request)
    if rv is None:
        return HTMLResponse("No such project.", status_code=404)
    data = {"slug": slug, "title": rv["title"], "page_url": f"/reviews/{slug}/", "limit": 120,
            "running": bool(project.JOBS.get(slug, {}).get("running")), "cards": project.cards(CFG, slug)}
    page = Path(__file__).with_name("project_page.html").read_text(encoding="utf-8")
    token = json.dumps(get_session(request)["csrf"])[1:-1]
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return HTMLResponse(page.replace("__CSRF__", token).replace("__DATA__", blob).replace("__THEME__", THEME_VARS).replace("__TABS__", project_tabs(slug, "papers")),
                        headers={"Cache-Control": "no-store"})


async def project_cards(request: Request) -> Response:
    slug, rv = _project_or_404(request)
    if rv is None:
        return JSONResponse({"error": "No such project."}, status_code=404)
    return JSONResponse({"cards": await run_in_threadpool(project.cards, CFG, slug)})


def project_status(request: Request) -> Response:
    slug, rv = _project_or_404(request)
    if rv is None:
        return JSONResponse({"error": "No such project."}, status_code=404)
    return JSONResponse(project.JOBS.get(slug, {"running": False, "done": 0, "total": 0, "error": None}))


def project_analyze(request: Request) -> Response:
    slug, rv = _project_or_404(request)
    if rv is None:
        return JSONResponse({"error": "No such project."}, status_code=404)
    if not project.start_analysis(CFG, slug):
        return JSONResponse({"error": "An analysis is already running for this project."}, status_code=409)
    return JSONResponse({"started": True})


async def project_act(request: Request) -> Response:
    slug, rv = _project_or_404(request)
    if rv is None:
        return JSONResponse({"error": "No such project."}, status_code=404)
    form = request.state.form
    action, ids = str(form.get("action", "")), [str(i) for i in form.getlist("id")]
    if not ids:
        return JSONResponse({"error": "Nothing selected."}, status_code=400)
    if action in ("link", "unlink"):
        stems = [i.removeprefix("page:") for i in ids if i.startswith("page:")]
        n = await run_in_threadpool(project.link, CFG, slug, stems, action == "unlink")
        if n:
            request_build()
        return JSONResponse({"message": f"{'Linked' if action == 'link' else 'Unlinked'} {n} paper(s)."})
    cids = [i for i in ids if not i.startswith("page:")]
    if action == "ingest":
        n = await run_in_threadpool(tick_items, CFG, cids)
        if n:
            spawn_queue()
        return JSONResponse({"message": f"Ingesting {n} paper(s); they join the project when done."})
    if action == "dismiss":
        con = db()
        con.executemany("UPDATE candidates SET status='rejected' WHERE id=? AND status='digested'", [(i,) for i in cids])
        con.commit()
        return JSONResponse({"message": f"Dismissed {len(cids)} candidate(s)."})
    return JSONResponse({"error": f"Unknown action {action!r}."}, status_code=400)


async def project_daily(request: Request) -> Response:
    slug, rv = _project_or_404(request)
    if rv is None:
        return HTMLResponse("No such project.", status_code=404)
    on = str(request.state.form.get("on", "")) == "1"
    await run_in_threadpool(project.set_daily, CFG, slug, on)
    back = str(request.state.form.get("back", ""))
    back = back if back.startswith("/app/project/") else "/app/projects"
    return redirect(back, f"Daily digest {'on' if on else 'off'} for {rv['title']}.")


def _exclusion_page(request: Request, f=None, status: int = 200, flash=None) -> Response:
    saved = exclusions.load_saved()
    f = f or {"keywords": "\n".join(saved["keywords"]), "topics": saved["topics"]}
    dc = CFG.get("discovery") or {}
    fixed = {"keywords": exclusions.clean_keywords(dc.get("exclude_keywords")), "topics": str(dc.get("exclude_topics") or "").strip()}
    dropped = []
    for r in db().execute("SELECT title, data FROM candidates WHERE status='excluded' ORDER BY seen DESC LIMIT 10"):
        dropped.append({"title": r["title"], "kw": json.loads(r["data"]).get("excluded_by", "")})
    return render(request, "settings.html", status=status, title="Settings", f=f, fixed=fixed, dropped=dropped, flash=flash)


def settings_get(request: Request) -> Response:
    return _exclusion_page(request)


async def settings_post(request: Request) -> Response:
    form = request.state.form
    kws, topics = exclusions.clean_keywords(str(form.get("keywords", ""))), str(form.get("topics", ""))
    await run_in_threadpool(exclusions.save, topics, kws)
    return redirect("/app/settings", f"Saved: {len(kws)} keyword(s){' and a topics note' if topics.strip() else ''}.")


def concepts_list(request: Request) -> Response:
    return render(request, "concepts.html", title="Concepts", concepts=concept.summary(CFG))


def _concept_form(request: Request, mode: str, action: str, f: dict, status: int = 200, flash=None) -> Response:
    f = {**f, "terms_text": "\n".join(f.get("terms") or []) if not isinstance(f.get("terms"), str) else f["terms"],
         "aliases_text": "\n".join(f.get("aliases") or []) if not isinstance(f.get("aliases"), str) else f["aliases"],
         "categories_text": " ".join(f.get("categories") or []) if not isinstance(f.get("categories"), str) else f["categories"]}
    return render(request, "concept_form.html", status=status, title=f.get("title") or "New concept", mode=mode,
                  action=action, f=f, flash=flash)


def concept_new_get(request: Request) -> Response:
    return _concept_form(request, "new", "/app/concepts/new",
                         {"title": "", "description": "", "terms": [], "categories": [], "aliases": []})


async def concept_new_post(request: Request) -> Response:
    form = request.state.form
    f = {k: str(form.get(k, "")) for k in ("title", "description", "terms", "categories")}
    try:
        slug = await run_in_threadpool(concept.create, CFG, f["title"], f["description"], f["terms"], f["categories"])
    except project.ProjectError as e:
        return _concept_form(request, "new", "/app/concepts/new", {**f, "aliases": []}, 400, {"msg": str(e), "kind": "err"})
    request_build()
    return redirect(f"/app/concepts/{slug}", "Concept created. Add search terms to include it in the daily digest.")


def _concept_or_404(request: Request):
    slug = request.path_params["slug"]
    try:
        return slug, concept.read(CFG, slug)
    except KeyError:
        return slug, None


def concept_get(request: Request) -> Response:
    slug, f = _concept_or_404(request)
    if f is None:
        return HTMLResponse("No such concept.", status_code=404)
    return _concept_form(request, "edit", f"/app/concepts/{slug}", f)


async def concept_post(request: Request) -> Response:
    slug, cur = _concept_or_404(request)
    if cur is None:
        return HTMLResponse("No such concept.", status_code=404)
    form = request.state.form
    p = {k: str(form.get(k, "")) for k in ("title", "description", "aliases", "terms", "categories", "overview", "version")}
    try:
        await run_in_threadpool(concept.save, CFG, slug, p["title"], p["description"], p["aliases"], p["terms"],
                                p["categories"], bool(form.get("discover")), p["overview"], p["version"])
    except (notes.Conflict, project.ProjectError) as e:
        conflict = isinstance(e, notes.Conflict)
        msg = ("This concept was changed somewhere else since you opened it. Your edits are kept below; "
               "saving again overwrites that change.") if conflict else str(e)
        mine = {**cur, **{k: p[k] for k in ("title", "description", "aliases", "terms", "categories", "overview")},
                "discover": bool(form.get("discover"))}
        return _concept_form(request, "edit", f"/app/concepts/{slug}", mine, 409 if conflict else 400, {"msg": msg, "kind": "err"})
    request_build()
    return redirect(f"/app/concepts/{slug}", "Saved.")


_SCOPE = re.compile(r"^(paper|project):[\w.\-]+$")


def chat_messages(request: Request) -> Response:
    scope = request.query_params.get("scope", "")
    if not _SCOPE.match(scope):
        return JSONResponse({"error": "Bad scope."}, status_code=400)
    try:
        after = int(request.query_params.get("after", "0"))
    except ValueError:
        after = 0
    return JSONResponse({"messages": chat.messages(scope, after)}, headers={"Cache-Control": "no-store"})


async def chat_send(request: Request) -> Response:
    form = request.state.form
    scope, question = str(form.get("scope", "")), str(form.get("question", ""))
    if not _SCOPE.match(scope):
        return JSONResponse({"error": "Bad scope."}, status_code=400)
    try:
        mid = await run_in_threadpool(chat.send, CFG, scope, question)
    except (chat.ChatError, chat.BudgetExceeded) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"id": mid})


def project_chat_get(request: Request) -> Response:
    slug, rv = _project_or_404(request)
    if rv is None:
        return HTMLResponse("No such project.", status_code=404)
    summ = chat.scope_summary(CFG, f"project:{slug}")
    return render(request, "project_chat.html", title=f"Chat: {rv['title']}", title_=rv["title"], slug=slug,
                  tabs=project_tabs(slug, "chat"), n_pdf=len(summ["with_pdf"]), skipped=summ["without_pdf"],
                  scope=f"project:{slug}", placeholder="Ask about the papers in this project, e.g. how do they evaluate their tools?")


def paper_chat_get(request: Request) -> Response:
    stem = request.path_params["stem"]
    try:
        pfm, _ = read_page(notes.paper_path(CFG["vault"], stem))
    except FileNotFoundError:
        return HTMLResponse("No such paper.", status_code=404)
    info = chat.paper_info(CFG, stem)
    return render(request, "paper_chat.html", title=f"Chat: {pfm.get('title', stem)}", stem=stem,
                  paper_title=pfm.get("title", stem), pdf=info["pdf"] if info else "", scope=f"paper:{stem}",
                  placeholder="Ask about this paper, e.g. what are the main limitations?")


def project_new_get(request: Request) -> Response:
    return render(request, "project_form.html", title="New project", mode="new", action="/app/projects/new",
                  f={"title": "", "question": "", "scope": "", "terms": "", "exclude": ""})


async def project_new_post(request: Request) -> Response:
    form = request.state.form
    f = {k: str(form.get(k, "")) for k in ("title", "question", "scope", "terms", "exclude")}
    try:
        slug = await run_in_threadpool(project.create, CFG, f["title"], f["question"], f["scope"], f["terms"], f["exclude"])
    except project.ProjectError as e:
        return render(request, "project_form.html", status=400, title="New project", mode="new",
                      action="/app/projects/new", f=f, flash={"msg": str(e), "kind": "err"})
    request_build()  # so /reviews/<slug>/ exists
    return redirect(f"/app/project/{slug}", f"Created project '{f['title']}'. Run a search, or link papers you already have under Papers.")


async def project_suggest(request: Request) -> Response:
    form = request.state.form
    try:
        out = await run_in_threadpool(project.suggest, CFG, str(form.get("title", "")),
                                      str(form.get("question", "")), str(form.get("scope", "")))
    except project.ProjectError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # LLM budget, claude CLI failures
        log.warning("project suggest failed: %s", e)
        return JSONResponse({"error": str(e)[:200]}, status_code=502)
    return JSONResponse(out)


TABS_CSS = ("<style>.ptabs{display:flex;gap:1.1rem;padding:0 1rem;border-bottom:1px solid var(--line);font-size:.95rem;flex-wrap:wrap}"
            ".ptabs a{padding:.5rem 0;color:var(--mut);text-decoration:none;border-bottom:2px solid transparent}"
            ".ptabs a.on{color:var(--fg);border-bottom-color:var(--acc,var(--ink,currentColor));font-weight:600}.ptabs .sp{flex:1}</style>")


def project_tabs(slug: str, active: str) -> str:
    """The tab bar shared by a project's pages (also injected into the two standalone pages)."""
    base = f"/app/project/{quote(slug)}"
    tabs = [("overview", "Overview", base), ("papers", "Papers", base + "/papers"), ("notes", "Notes", base + "/notes"), ("chat", "Chat", base + "/chat")]
    links = "".join(f'<a href="{href}"{" class=on" if key == active else ""}>{label}</a>' for key, label, href in tabs)
    return f'{TABS_CSS}<nav class="ptabs">{links}<span class="sp"></span><a href="/reviews/{quote(slug)}/">Wiki page ↗</a></nav>'


def _overview_page(request: Request, slug: str, f: dict, status: int = 200, flash=None) -> Response:
    f = {**f, "terms_text": "\n".join(f["terms"]) if isinstance(f["terms"], list) else f["terms"]}
    run = project.SEARCHES.get(slug, {"running": False, "rc": None})
    return render(request, "project_overview.html", status=status, title=f["title"], slug=slug, f=f, flash=flash,
                  tabs=project_tabs(slug, "overview"), searching=run["running"], search_rc=run["rc"],
                  linked=len(project.linked_papers(CFG, slug)))


def project_overview_get(request: Request) -> Response:
    slug, rv = _project_or_404(request)
    if rv is None:
        return HTMLResponse("No such project.", status_code=404)
    return _overview_page(request, slug, project.read_overview(CFG, slug))


async def project_overview_post(request: Request) -> Response:
    slug, rv = _project_or_404(request)
    if rv is None:
        return HTMLResponse("No such project.", status_code=404)
    form = request.state.form
    posted = {k: str(form.get(k, "")) for k in ("question", "scope", "terms", "exclude", "status", "version")}
    try:
        await run_in_threadpool(project.save_overview, CFG, slug, posted["question"], posted["scope"], posted["terms"],
                                posted["exclude"], posted["status"], posted["version"])
    except (notes.Conflict, project.ProjectError) as e:
        cur = project.read_overview(CFG, slug)
        msg = ("This project was changed somewhere else (another tab, or an edit to its page) since you opened it. "
               "Your edits are kept below; saving again overwrites that change.") if isinstance(e, notes.Conflict) else str(e)
        mine = {**cur, "question": posted["question"], "scope": posted["scope"], "terms": project.clean_terms(posted["terms"]),
                "exclude": posted["exclude"], "status": posted["status"] if posted["status"] in ("active", "archived") else cur["status"]}
        return _overview_page(request, slug, mine, status=409 if isinstance(e, notes.Conflict) else 400,
                              flash={"msg": msg, "kind": "err"})
    request_build()  # the wiki page shows the question and scope
    return redirect(f"/app/project/{slug}", "Saved.")


async def project_search(request: Request) -> Response:
    slug, rv = _project_or_404(request)
    if rv is None:
        return HTMLResponse("No such project.", status_code=404)
    try:
        started = await run_in_threadpool(project.start_search, CFG, slug)
    except project.ProjectError as e:
        return redirect(f"/app/project/{slug}", str(e), "err")
    return redirect(f"/app/project/{slug}", "Search started." if started else "A search is already running for this project.",
                    "ok" if started else "err")


def project_search_status(request: Request) -> Response:
    slug, rv = _project_or_404(request)
    if rv is None:
        return JSONResponse({"error": "No such project."}, status_code=404)
    return JSONResponse(project.SEARCHES.get(slug, {"running": False, "rc": None}))


def _version(form) -> int:
    """The mtime token a notes form was loaded with. Anything that is not a plain integer (a number a browser has
    rounded to a float, junk) becomes 0, which cannot match an existing file, so the save is refused as a conflict
    instead of crashing."""
    try:
        return int(str(form.get("mtime") or "0"))
    except ValueError:
        return 0


def project_notes_get(request: Request) -> Response:
    """The project's whiteboard: a markdown scratchpad with quick capture and a picker for linked papers."""
    slug, rv = _project_or_404(request)
    if rv is None:
        return HTMLResponse("No such project.", status_code=404)
    text, mtime = project.read_notes(CFG, slug)
    data = {"slug": slug, "title": rv["title"], "page_url": f"/reviews/{slug}/", "text": text, "mtime": str(mtime),  # a string: nanosecond mtimes exceed what a JS number holds exactly
            "skeleton": project.SKELETON, "papers": project.linked_papers(CFG, slug)}
    page = Path(__file__).with_name("project_notes.html").read_text(encoding="utf-8")
    token = json.dumps(get_session(request)["csrf"])[1:-1]
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return HTMLResponse(page.replace("__CSRF__", token).replace("__DATA__", blob).replace("__THEME__", THEME_VARS).replace("__TABS__", project_tabs(slug, "notes")),
                        headers={"Cache-Control": "no-store"})


async def project_notes_post(request: Request) -> Response:
    slug, rv = _project_or_404(request)
    if rv is None:
        return JSONResponse({"error": "No such project."}, status_code=404)
    form = request.state.form
    try:
        mtime = await run_in_threadpool(project.save_notes, CFG, slug, str(form.get("text", "")), _version(form))
    except notes.Conflict:
        text, cur = await run_in_threadpool(project.read_notes, CFG, slug)
        return JSONResponse({"error": "changed elsewhere", "text": text, "mtime": str(cur)}, status_code=409)
    if form.get("publish"):
        request_build()
    return JSONResponse({"mtime": str(mtime)})


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
                    "has_note": (v / notes.NOTES / p.name).exists(),
                    "chat": str(fm.get("pdf") or "").lower().endswith(".pdf")})
    return render(request, "papers.html", title="Papers", papers=out, q=request.query_params.get("q", ""), more=more)


ASK_SCHEMA = """CREATE TABLE IF NOT EXISTS asks (id TEXT PRIMARY KEY, ts TEXT DEFAULT (datetime('now')), question TEXT,
    scope TEXT, status TEXT, answer TEXT, error TEXT, cost REAL DEFAULT 0, n_papers INTEGER DEFAULT 0)"""


def _asks_con():
    con = db()
    con.execute(ASK_SCHEMA)
    return con


def _run_ask(aid: str, question: str, concept: str, review: str, since: int | None) -> None:
    con = _asks_con()
    try:
        r = askmod.ask(CFG, question, concept, review, since)
        con.execute("UPDATE asks SET status='done', answer=?, cost=?, n_papers=? WHERE id=?",
                    (r["answer"], r["cost"], r["n_papers"], aid))
    except (askmod.AskError, askmod.BudgetExceeded) as e:
        con.execute("UPDATE asks SET status='error', error=? WHERE id=?", (str(e), aid))
    except Exception as e:
        log.exception("ask failed")
        con.execute("UPDATE asks SET status='error', error=? WHERE id=?", (f"Something went wrong: {e}"[:300], aid))
    con.commit()


def ask_get(request: Request) -> Response:
    v, con = CFG["vault"], _asks_con()
    recent = [dict(r) for r in con.execute("SELECT id, ts, question, scope, status FROM asks ORDER BY ts DESC, rowid DESC LIMIT 15")]
    return render(request, "ask.html", title="Ask", concepts=_titles(vault.concepts(v)), reviews=_titles(vault.reviews(v)),
                  recent=recent)


def ask_post(request: Request) -> Response:
    form = request.state.form
    question = str(form.get("question", "")).strip()
    concept, review = str(form.get("concept", "")), str(form.get("review", ""))
    since_raw = str(form.get("since", "")).strip()
    if not question:
        return redirect("/app/ask", "Ask a question first.", "err")
    if since_raw and not since_raw.isdigit():
        return redirect("/app/ask", "Year must be a number.", "err")
    since = int(since_raw) if since_raw else None
    con = _asks_con()
    aid = secrets.token_hex(6)
    scope = ", ".join(x for x in [f"concept: {concept}" if concept else "", f"review: {review}" if review else "",
                                  f"from {since}" if since else ""] if x)
    con.execute("INSERT INTO asks(id, question, scope, status) VALUES(?,?,?,'running')", (aid, question[:2000], scope))
    con.commit()
    threading.Thread(target=_run_ask, args=(aid, question, concept, review, since), daemon=True).start()
    return redirect(f"/app/ask/{aid}")


def ask_result(request: Request) -> Response:
    import markdown
    row = _asks_con().execute("SELECT * FROM asks WHERE id=?", (request.path_params["aid"],)).fetchone()
    if not row:
        return HTMLResponse("No such question.", status_code=404)
    a = dict(row)
    html = markdown.markdown(askmod.sanitize_for_html(a["answer"] or ""), extensions=["tables", "sane_lists"])
    return render(request, "ask_result.html", title="Answer", a=a, html=html,
                  refresh=3 if a["status"] == "running" else None)


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
        notes.save_note(CFG["vault"], stem, str(form.get("text", "")), _version(form))
    except FileNotFoundError:
        return HTMLResponse("No such paper.", status_code=404)
    except notes.Conflict:
        return redirect(f"/app/notes/{stem}", "This note changed elsewhere (another tab, or a synced edit) since you opened "
                        "it. Your edit was NOT saved: reload to see the current text, then redo it.", "err")
    request_build()
    return redirect(f"/app/notes/{stem}", "Saved. The site is rebuilding.")



def attach_get(request: Request) -> Response:
    from . import attach
    ref = request.path_params["ref"]
    try:
        kind, ident = attach.resolve_target(CFG["vault"], db(), ref)
    except attach.AttachError as e:
        return HTMLResponse(str(e), status_code=404)
    if kind == "page":
        fm, _ = read_page(CFG["vault"] / vault.PAPERS / f"{ident}.md")
        title, stem, has_pdf, abstract_only = fm.get("title", ident), ident, bool(fm.get("pdf")), fm.get("fulltext") is False
        status = fm.get("status")
    else:
        d = json.loads(db().execute("SELECT data FROM items WHERE key=?", (ident,)).fetchone()["data"])
        title, stem, has_pdf, abstract_only, status = d.get("title") or d.get("filename") or ident, "", False, False, "auto"
    return render(request, "attach.html", title="Attach PDF", paper_title=title, stem=stem, ref=ref, has_pdf=has_pdf,
                  abstract_only=abstract_only, edited=status not in (None, "auto"), page_status=status)


async def attach_post(request: Request) -> Response:
    from . import attach
    ref, form = request.path_params["ref"], request.state.form
    back = f"/app/attach/{ref}"
    f = form.get("file")
    if not getattr(f, "filename", ""):
        return redirect(back, "Choose a PDF first.", "err")
    try:
        data = await f.read(attach.MAX_UPLOAD + 1)
        await run_in_threadpool(attach.validate, data)
        kind, _ = await run_in_threadpool(attach.resolve_target, CFG["vault"], db(), ref)
    except attach.AttachError as e:
        return redirect(back, str(e), "err")
    tmp = STATE_DIR / "uploads"
    tmp.mkdir(exist_ok=True)
    pdf = tmp / f"attach-{secrets.token_hex(6)}.pdf"
    pdf.write_bytes(data)
    attach.spawn(ref, pdf, force=bool(form.get("force")))
    return redirect(back, "Attaching and re-summarising in the background; the paper page updates when the site "
                    "rebuilds (about a minute). If it fails you get a notification and the log has details.")

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
    chat.reset_interrupted()
    _asks_con().execute("UPDATE asks SET status='error', error='Interrupted by a restart; ask again.' WHERE status='running'")
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
        Route("/app/projects", guard(projects_list), methods=["GET"]),
        Route("/app/settings", guard(settings_get), methods=["GET"]),
        Route("/app/settings", guard(settings_post), methods=["POST"]),
        Route("/app/concepts", guard(concepts_list), methods=["GET"]),
        Route("/app/concepts/new", guard(concept_new_get), methods=["GET"]),
        Route("/app/concepts/new", guard(concept_new_post), methods=["POST"]),
        Route("/app/concepts/{slug}", guard(concept_get), methods=["GET"]),
        Route("/app/concepts/{slug}", guard(concept_post), methods=["POST"]),
        Route("/app/projects/new", guard(project_new_get), methods=["GET"]),
        Route("/app/projects/new", guard(project_new_post), methods=["POST"]),
        Route("/app/projects/suggest", guard(project_suggest), methods=["POST"]),
        Route("/app/project/{slug}/overview", guard(project_overview_post), methods=["POST"]),
        Route("/app/project/{slug}/search", guard(project_search), methods=["POST"]),
        Route("/app/project/{slug}/search-status", guard(project_search_status), methods=["GET"]),
        Route("/app/project/{slug}", guard(project_overview_get), methods=["GET"]),
        Route("/app/project/{slug}/papers", guard(project_papers), methods=["GET"]),
        Route("/app/project/{slug}/cards", guard(project_cards), methods=["GET"]),
        Route("/app/project/{slug}/status", guard(project_status), methods=["GET"]),
        Route("/app/project/{slug}/analyze", guard(project_analyze), methods=["POST"]),
        Route("/app/project/{slug}/act", guard(project_act), methods=["POST"]),
        Route("/app/project/{slug}/daily", guard(project_daily), methods=["POST"]),
        Route("/app/project/{slug}/chat", guard(project_chat_get), methods=["GET"]),
        Route("/app/paper/{stem}/chat", guard(paper_chat_get), methods=["GET"]),
        Route("/app/chat/messages", guard(chat_messages), methods=["GET"]),
        Route("/app/chat/send", guard(chat_send), methods=["POST"]),
        Route("/app/project/{slug}/notes", guard(project_notes_get), methods=["GET"]),
        Route("/app/project/{slug}/notes", guard(project_notes_post), methods=["POST"]),
        Route("/app/swipe", guard(swipe_get), methods=["GET"]),
        Route("/app/swipe/decide", guard(swipe_decide), methods=["POST"]),
        Route("/app/swipe/commit", guard(swipe_commit), methods=["POST"]),
        Route("/app/ask", guard(ask_get), methods=["GET"]),
        Route("/app/ask", guard(ask_post), methods=["POST"]),
        Route("/app/ask/{aid}", guard(ask_result), methods=["GET"]),
        Route("/app/papers", guard(papers)),
        Route("/app/notes/{stem}", guard(note_get), methods=["GET"]),
        Route("/app/notes/{stem}", guard(note_post), methods=["POST"]),
        Route("/app/attach/{ref}", guard(attach_get), methods=["GET"]),
        Route("/app/attach/{ref}", guard(attach_post), methods=["POST"]),
    ])


def serve(cfg) -> None:
    import uvicorn
    if not os.environ.get("LITWIKI_WEB_PASSWORD"):
        raise SystemExit("LITWIKI_WEB_PASSWORD is not set in ~/.env")
    os.environ["PATH"] = f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"  # for `claude` in jobs
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=int(cfg["ports"]["web"]), log_level="warning", access_log=False)
