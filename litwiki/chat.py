"""Chat about papers, answered with verbatim quotes that the server checks.

Scopes: `paper:<stem>` (one paper) and `project:<slug>` (the papers linked to a project). Only papers with a PDF that
has selectable text are included; web snapshots and abstract-only pages are not.

How an answer is made
1. Page text is extracted once per PDF and cached (state/text/<stem>.json, keyed by file size + mtime).
2. One paper that fits the text budget is sent whole, with page markers. Otherwise (a project, or a very long paper)
   the text is cut into overlapping chunks and TF-IDF picks the passages most similar to the question.
3. The model must answer in prose and back every claim with exact quotes: {paper, page, quote}.
4. The server then looks each quote up in the cited paper's own text (case, punctuation, ligatures and line-break
   hyphenation are ignored; the words must all be there, in order). A quote that is not found is dropped, never shown,
   and the answer says how many were dropped. Quotes found on a different page are re-pointed to the right page.
5. Each kept quote links to its PDF page (`/_pdfs/<file>#page=N`; the reader will later open it with a highlight).
Page numbers are PDF page indexes (1 = first page), which can differ from the numbers printed on the page."""
import json
import re
import secrets
import threading
from html import escape
from pathlib import Path
from urllib.parse import quote as urlquote

from . import vault
from .common import STATE_DIR, db, log, read_page
from .llm import BudgetExceeded, claude, spent_purpose_today
from .textract import _norm, pdf_pages

TEXT_DIR = STATE_DIR / "text"
CHUNK, OVERLAP = 900, 150
TOP_PROJECT, TOP_PAPER, PER_PAPER_CAP = 18, 24, 5
MIN_QUOTE_WORDS, MAX_QUOTE_WORDS = 4, 120
HISTORY_TURNS = 3


_HYPHEN = re.compile(r"-\s*\n\s*")   # a word hyphenated across a line break


class ChatError(Exception):
    """Shown to the user as-is."""


# ---------- papers and their text ----------

def paper_info(cfg, stem: str) -> dict | None:
    """Title/authors/year and the PDF of a paper page, or None if it has no PDF to quote from."""
    path = cfg["vault"] / vault.PAPERS / f"{stem}.md"
    if not path.is_file() or not re.fullmatch(r"[\w.\-]+", stem):
        return None
    fm, _ = read_page(path)
    pdf = str(fm.get("pdf") or "")
    if not pdf.lower().endswith(".pdf") or not (cfg["vault"] / pdf).is_file():
        return None
    authors = [str(a) for a in (fm.get("authors") or []) if not str(a).strip().isdigit()]
    return {"stem": stem, "title": str(fm.get("title") or stem), "authors": authors, "year": fm.get("year"),
            "pdf": pdf, "reviews": list(fm.get("reviews") or [])}


def label(p: dict) -> str:
    if not p["authors"]:
        return " ".join(p["title"].split()[:4])
    return f"{p['authors'][0].split()[-1]}{' et al.' if len(p['authors']) > 1 else ''} {p['year'] or ''}".strip()


def page_texts(cfg, p: dict) -> list[str]:
    """Per-page text of the paper's PDF, from the cache when the file has not changed."""
    pdf = cfg["vault"] / p["pdf"]
    st = pdf.stat()
    sig = [st.st_size, st.st_mtime_ns]
    cache = TEXT_DIR / f"{p['stem']}.json"
    try:
        c = json.loads(cache.read_text())
        if c.get("sig") == sig:
            return c["pages"]
    except (OSError, ValueError):
        pass
    pages = pdf_pages(pdf)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(f".{cache.name}.tmp")
    tmp.write_text(json.dumps({"sig": sig, "pages": pages}))
    tmp.replace(cache)
    return pages


def scope_papers(cfg, scope: str) -> tuple[list[dict], list[str]]:
    """(papers with usable PDF text, titles of papers skipped for lacking it) for a scope string."""
    kind, _, ident = scope.partition(":")
    v = cfg["vault"]
    if kind == "paper":
        p = paper_info(cfg, ident)
        if not p:
            raise ChatError("This paper has no PDF to read. Attach one first (the paper's page has an Attach PDF button).")
        stems = [ident]
    elif kind == "project":
        if ident not in vault.reviews(v):
            raise ChatError("No such project.")
        stems = []
        for pg in sorted((v / vault.PAPERS).glob("*.md")):
            fm, _ = read_page(pg)
            if ident in (fm.get("reviews") or []):
                stems.append(pg.stem)
        if not stems:
            raise ChatError("This project has no linked papers yet. Link some under Papers first.")
    else:
        raise ChatError("Unknown chat scope.")
    ok, skipped = [], []
    for stem in stems:
        p = paper_info(cfg, stem)
        if p and any(t.strip() for t in page_texts(cfg, p)):
            ok.append(p)
        else:
            fm, _ = read_page(v / vault.PAPERS / f"{stem}.md")
            skipped.append(str(fm.get("title") or stem))
    if not ok:
        raise ChatError("None of the papers here has a PDF with selectable text, so there is nothing to quote from.")
    return ok, skipped


def scope_summary(cfg, scope: str) -> dict:
    """{'with_pdf': [titles], 'without_pdf': [titles]} for a scope, without reading any PDF (cheap enough for page loads)."""
    kind, _, ident = scope.partition(":")
    v = cfg["vault"]
    if kind == "paper":
        stems = [ident]
    elif kind == "project":
        stems = [pg.stem for pg in sorted((v / vault.PAPERS).glob("*.md")) if ident in (read_page(pg)[0].get("reviews") or [])]
    else:
        stems = []
    with_pdf, without = [], []
    for stem in stems:
        info = paper_info(cfg, stem)
        if info:
            with_pdf.append(info["title"])
        elif (v / vault.PAPERS / f"{stem}.md").is_file():
            without.append(str(read_page(v / vault.PAPERS / f"{stem}.md")[0].get("title") or stem))
    return {"with_pdf": with_pdf, "without_pdf": without}


# ---------- retrieval ----------

def chunk_page(text: str) -> list[str]:
    t = _HYPHEN.sub("", text)          # rejoin words hyphenated across lines, as the verifier does
    t = " ".join(t.split())
    out, i = [], 0
    while i < len(t):
        end = min(len(t), i + CHUNK)
        if end < len(t):
            cut = t.rfind(". ", i + CHUNK * 6 // 10, end)
            end = cut + 1 if cut > 0 else end
        piece = t[i:end].strip()
        if piece:
            out.append(piece)
        if end >= len(t):
            break
        i = max(end - OVERLAP, i + 1)
    return out


_INDEX: dict = {}      # scope signature -> (chunks, vectorizer, matrix); small, in-process


def _index(cfg, papers: list[dict]):
    from sklearn.feature_extraction.text import TfidfVectorizer
    key = tuple((p["stem"], (cfg["vault"] / p["pdf"]).stat().st_mtime_ns) for p in papers)
    if key in _INDEX:
        return _INDEX[key]
    chunks = [{"paper": p["stem"], "page": n, "text": c}
              for p in papers for n, page in enumerate(page_texts(cfg, p), 1) for c in chunk_page(page)]
    if not chunks:
        raise ChatError("No readable text was found in these PDFs.")
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True, min_df=1)
    mat = vec.fit_transform([c["text"] for c in chunks])
    if len(_INDEX) >= 6:
        _INDEX.pop(next(iter(_INDEX)))
    _INDEX[key] = (chunks, vec, mat)
    return _INDEX[key]


def retrieve(cfg, papers: list[dict], query: str, k: int, per_paper: int | None) -> list[dict]:
    from sklearn.metrics.pairwise import linear_kernel
    chunks, vec, mat = _index(cfg, papers)
    sims = linear_kernel(vec.transform([query]), mat).ravel()
    picked, per = [], {}
    for i in sims.argsort()[::-1]:
        if sims[i] <= 0 or len(picked) >= k:
            break
        c = chunks[i]
        if per_paper and per.get(c["paper"], 0) >= per_paper:
            continue
        per[c["paper"]] = per.get(c["paper"], 0) + 1
        picked.append(c)
    order = {p["stem"]: n for n, p in enumerate(papers)}
    return sorted(picked, key=lambda c: (order[c["paper"]], c["page"]))


# ---------- prompt ----------

SYSTEM = """You answer questions about research papers using ONLY the text supplied. Never use outside knowledge about
the papers. If the supplied text does not answer the question, or answers it only in part, say so plainly.

Every claim in your answer must be backed by one or more direct quotes from the supplied text, given in `quotes`:
- `quote` is copied character for character from the text, one contiguous stretch of at most 60 words. Never
  paraphrase, merge, reorder, correct or abridge inside a quote.
- `paper` is the paper's id exactly as given, and `page` is the page number from its [page N] marker.
- Put the quote's marker, e.g. [q1], right after the claim it supports. Give each quote an id q1, q2, ... in order.
- A claim without a supporting quote should not be made.
Write the answer in Markdown, concisely and without preamble. Do not write URLs. When several papers are in scope, say
which paper each claim comes from by author and year."""

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Markdown, with [qN] markers after supported claims."},
        "quotes": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "paper": {"type": "string"}, "page": {"type": "integer"},
                           "quote": {"type": "string"}},
            "required": ["id", "paper", "page", "quote"]}},
    },
    "required": ["answer", "quotes"],
}


def _full_text(pages: list[str]) -> str:
    return "".join(f"\n=== [page {n}] ===\n{_HYPHEN.sub('', t).strip()}\n" for n, t in enumerate(pages, 1))


def build_prompt(cfg, papers: list[dict], question: str, history: list[tuple[str, str]]) -> str:
    roster = "\n".join(f"[{p['stem']}] {p['title']} — {label(p)}" for p in papers)
    budget = int(cfg["llm"].get("max_text_chars", 120000))
    if len(papers) == 1:
        pages = page_texts(cfg, papers[0])
        body = _full_text(pages)
        if len(body) > budget:
            body = None
    else:
        body = None
    if body is None:
        query = " ".join([*(q for q, _ in history[-1:]), question])
        passages = retrieve(cfg, papers, query, TOP_PAPER if len(papers) == 1 else TOP_PROJECT,
                            None if len(papers) == 1 else PER_PAPER_CAP)
        if not passages:
            raise ChatError("I could not find any passage related to that question in the papers.")
        body = "\n".join(f"\n--- [{c['paper']}] [page {c['page']}] ---\n{c['text']}" for c in passages)
        mode = "PASSAGES (the parts of the papers most similar to the question; the papers contain more)"
    else:
        mode = "FULL TEXT"
    convo = ""
    if history:
        convo = "CONVERSATION SO FAR (for follow-up questions only; answers are not evidence):\n" + "\n".join(
            f"Q: {q}\nA: {a[:500]}" for q, a in history) + "\n\n"
    return f"PAPERS IN SCOPE:\n{roster}\n\n{mode}:\n{body}\n\n{convo}QUESTION: {question}"


# ---------- verification and rendering ----------

def find_quote(quote: str, pages: list[str], hint: int | None) -> int | None:
    """1-based page where `quote` occurs (the hinted page first, then any other, then across a page break); None if
    its words are not all there in order. Strict: no fuzzy matching, so a paraphrase never passes."""
    q = _norm(quote)
    n = len(q.split())
    if not (MIN_QUOTE_WORDS <= n <= MAX_QUOTE_WORDS):
        return None
    normed = [_norm(p) for p in pages]
    order = list(range(len(pages)))
    if hint and 1 <= hint <= len(pages):
        order.remove(hint - 1)
        order.insert(0, hint - 1)
    for i in order:
        if q in normed[i]:
            return i + 1
    for i in order:
        if i + 1 < len(normed) and q in normed[i] + " " + normed[i + 1]:
            return i + 1
    return None


def verify(cfg, papers: list[dict], raw_quotes: list[dict]) -> tuple[dict, int]:
    """({marker id: verified quote record}, number dropped)."""
    by = {p["stem"]: p for p in papers}
    kept, dropped = {}, 0
    for q in raw_quotes:
        p = by.get(str(q.get("paper")))
        qid = str(q.get("id"))
        if not p or qid in kept:
            dropped += 1
            continue
        page = find_quote(str(q.get("quote", "")), page_texts(cfg, p), q.get("page") if isinstance(q.get("page"), int) else None)
        if page is None:
            dropped += 1
            continue
        kept[qid] = {"paper": p["stem"], "title": p["title"], "label": label(p), "page": page,
                     "quote": " ".join(str(q["quote"]).split()), "pdf": f"/{p['pdf']}#page={page}",
                     "wiki": f"/papers/{urlquote(p['stem'])}/"}
    return kept, dropped


MARK = re.compile(r"\[q(\d+)\]")


def render_answer(answer: str, kept: dict) -> tuple[str, list[dict]]:
    """Safe HTML for the answer, with each surviving [qN] turned into a numbered link; unsupported markers vanish.
    Returns (html, quotes in order of first appearance, numbered)."""
    import markdown
    from .ask import sanitize_for_html
    order: list[str] = []

    def repl(m):
        qid = f"q{m.group(1)}"
        if qid not in kept:
            return ""
        if qid not in order:
            order.append(qid)
        return f" @@cite{order.index(qid) + 1}@@"
    text = MARK.sub(repl, answer)
    html = markdown.markdown(sanitize_for_html(text), extensions=["tables", "sane_lists"])
    html = re.sub(r"@@cite(\d+)@@", r'<sup><a class="cite" href="#cite-\1">[\1]</a></sup>', html)
    quotes = [{"n": i + 1, **kept[qid]} for i, qid in enumerate(order)]
    return html, quotes


# ---------- history and jobs ----------

def _con():
    return db()


def messages(scope: str, after: int = 0) -> list[dict]:
    rows = _con().execute("SELECT id, ts, role, status, text, data FROM chat_messages WHERE scope=? AND id>? ORDER BY id",
                          (scope, after)).fetchall()
    return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]


def _history(scope: str) -> list[tuple[str, str]]:
    rows = _con().execute("SELECT role, text, data FROM chat_messages WHERE scope=? AND status='done' ORDER BY id DESC LIMIT ?",
                          (scope, HISTORY_TURNS * 2)).fetchall()[::-1]
    out, q = [], None
    for r in rows:
        if r["role"] == "user":
            q = r["text"]
        elif q is not None:
            out.append((q, re.sub(r"<[^>]+>", "", r["text"])))
            q = None
    return out


def answer(cfg, scope: str, question: str, history: list[tuple[str, str]]) -> dict:
    """The whole pipeline for one question (no database): returns {html, quotes, dropped, cost, n_papers, skipped}."""
    papers, skipped = scope_papers(cfg, scope)
    prompt = build_prompt(cfg, papers, question, history)
    model = (cfg.get("llm") or {}).get("ask_model") or cfg["llm"]["summary_model"]
    ref = f"chat:{secrets.token_hex(4)}"
    out = claude(cfg, prompt, system=SYSTEM, model=model, schema=SCHEMA, purpose="chat", ref=ref)
    kept, dropped = verify(cfg, papers, out.get("quotes") or [])
    html, quotes = render_answer(out.get("answer", ""), kept)
    cost = float(_con().execute("SELECT COALESCE(SUM(usd),0) FROM spend WHERE ref=?", (ref,)).fetchone()[0])
    log.info("chat %s: %d papers, %d quotes kept, %d dropped, $%.3f", scope, len(papers), len(quotes), dropped, cost)
    return {"html": html, "quotes": quotes, "dropped": dropped, "cost": cost, "n_papers": len(papers), "skipped": skipped}


def _run(cfg, scope: str, question: str, mid: int, history) -> None:
    con = db()
    try:
        r = answer(cfg, scope, question, history)
        html = r.pop("html")
        con.execute("UPDATE chat_messages SET status='done', text=?, data=? WHERE id=?", (html, json.dumps(r), mid))
    except (ChatError, BudgetExceeded) as e:
        con.execute("UPDATE chat_messages SET status='error', text=? WHERE id=?", (str(e), mid))
    except Exception as e:
        log.exception("chat failed")
        con.execute("UPDATE chat_messages SET status='error', text=? WHERE id=?", (f"Something went wrong: {e}"[:300], mid))
    con.commit()


def send(cfg, scope: str, question: str, background: bool = True) -> int:
    """Record the question and start answering it; returns the assistant message id to poll. One question at a time per
    scope. Raises ChatError for an empty question, a spent daily budget, or a scope with nothing to read."""
    question = question.strip()[:2000]
    if not question:
        raise ChatError("Type a question first.")
    cap = float((cfg.get("budget") or {}).get("chat_daily_usd", 3.0))
    con = _con()
    if spent_purpose_today(con, "chat") >= cap:
        raise ChatError(f"Today's chat budget (${cap:.2f}) is used up; it resets at 00:00 UTC.")
    scope_papers(cfg, scope)   # fail fast, before anything is recorded
    if con.execute("SELECT 1 FROM chat_messages WHERE scope=? AND status='running'", (scope,)).fetchone():
        raise ChatError("Still answering the previous question; wait for it to finish.")
    history = _history(scope)
    con.execute("INSERT INTO chat_messages(scope, role, text) VALUES(?,?,?)", (scope, "user", question))
    mid = con.execute("INSERT INTO chat_messages(scope, role, status) VALUES(?,?,?)", (scope, "assistant", "running")).lastrowid
    con.commit()
    if background:
        threading.Thread(target=_run, args=(cfg, scope, question, mid, history), daemon=True).start()
    else:
        _run(cfg, scope, question, mid, history)
    return mid


def reset_interrupted() -> None:
    """After a restart no job is running any more."""
    con = _con()
    con.execute("UPDATE chat_messages SET status='error', text='Interrupted by a restart; ask again.' WHERE status='running'")
    con.commit()
