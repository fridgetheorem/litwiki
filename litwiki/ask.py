"""Ask questions about the whole library ("what topics do these papers share?", "which papers use X?").

The library is small enough to put in one prompt: title, authors, year, concepts and TL;DR for every paper
(~40k tokens at 375 papers). If the model decides the TL;DRs aren't enough (numbers, evaluation details,
limitations) it names up to MAX_DETAIL papers, and a second call gets their findings, limitations and your
notes. Papers are cited as [@page-stem]; we turn those into links ourselves, so URLs can't be invented, and
unknown ids are flagged. Past a few thousand papers this needs retrieval (embeddings) instead."""
import re
import secrets
from html import escape
from urllib.parse import quote

from . import vault
from .cluster import section_text
from .common import db, log, read_page
from .llm import BudgetExceeded, claude, spent_purpose_today  # noqa: F401  (BudgetExceeded re-exported)

MAX_DETAIL = 30
MAX_CORPUS_CHARS = 500_000          # ~125k tokens; beyond this, ask the user to filter
DETAIL_SECTIONS = ["TL;DR", "Problem", "Approach", "Key findings", "Limitations"]
CITE = re.compile(r"\[@([\w.\-]+)\]")


class AskError(Exception):
    """Shown to the user as-is."""


SYSTEM = """You answer questions about a personal library of research papers. You are given, for each paper, an id,
title, authors, year, concept tags and a short TL;DR written from the paper. Rules:
- Use ONLY the supplied material. Never use outside knowledge about these papers; if the material doesn't
  answer the question (or only partly), say so plainly.
- Cite the papers behind every claim as [@id] using the id exactly as given, e.g. "[@smith2023-foo-bar]".
  Several citations: "[@a] [@b]". Never write URLs or invent ids.
- For "major topics / themes" questions: group the papers into themes, name each theme, say roughly how many
  papers support it and cite representative ones. Prefer a few well-supported themes over a long list.
- Be concise (aim for under 500 words), use Markdown (short headings or bullets), no preamble."""

STAGE1_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Markdown answer with [@id] citations. If need_detail is non-empty "
                   "this is a provisional answer."},
        "need_detail": {"type": "array", "maxItems": MAX_DETAIL, "items": {"type": "string"},
                        "description": "Ids of papers whose findings/limitations/notes are needed to answer properly. "
                                       "Empty if the TL;DRs suffice."},
    },
    "required": ["answer", "need_detail"],
}
STAGE2_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}

STAGE1_INSTR = ("Answer the question from the TL;DRs. IMPORTANT: if the question asks for specifics the TL;DRs do not "
                "contain (numbers, evaluation setups, datasets, limitations, method details) you must NOT answer that "
                "you lack them: instead put the up-to-%d most relevant paper ids in need_detail (a second pass will "
                "give you their full findings, limitations and the user's notes) and give only a short provisional "
                "answer. Leave need_detail empty only when the TL;DRs fully answer the question.\n\nQUESTION: " % MAX_DETAIL)
STAGE2_INSTR = ("Here are the detailed sections (and the user's own notes, if any) for the papers you asked about. "
                "Give the final answer to the question, using these details and the TL;DRs.\n\nQUESTION: ")


# ---------- corpus ----------

def load_corpus(cfg, concept: str = "", review: str = "", since: int | None = None) -> list[dict]:
    v = cfg["vault"]
    if concept and concept not in vault.concepts(v):
        raise AskError(f"Unknown concept {concept!r}.")
    if review and review not in vault.reviews(v):
        raise AskError(f"Unknown review {review!r}.")
    out = []
    for p in sorted((v / vault.PAPERS).glob("*.md")):
        fm, body = read_page(p)
        if not fm.get("title"):
            continue
        cs = [c for c in (fm.get("concepts") or []) if isinstance(c, str)]
        if concept and concept not in cs:
            continue
        if review and review not in (fm.get("reviews") or []):
            continue
        if since and not (fm.get("year") and int(fm["year"]) >= since):
            continue
        authors = [str(a) for a in (fm.get("authors") or [])]
        out.append({"id": p.stem, "title": str(fm["title"]), "year": fm.get("year"), "concepts": cs,
                    "authors": authors, "tldr": section_text(body, ["TL;DR"]), "body": body})
    return out


def corpus_text(cfg, papers: list[dict]) -> str:
    titles = {s: fm["title"] for s, fm in vault.concepts(cfg["vault"]).items()}
    used = sorted({c for p in papers for c in p["concepts"]})
    lines = ["CONCEPT TAGS (slug: name):", *(f"- {c}: {titles.get(c, c)}" for c in used), "", "PAPERS:"]
    for p in papers:
        au = ", ".join(p["authors"][:3]) + (" et al." if len(p["authors"]) > 3 else "")
        lines.append(f"[@{p['id']}] {p['title']} — {au or 'unknown authors'}, {p['year'] or 'n.d.'} | "
                     f"concepts: {', '.join(p['concepts']) or 'none'}\n  TL;DR: {p['tldr'] or '(none)'}")
    return "\n".join(lines)


def detail_text(cfg, papers: list[dict], ids: list[str]) -> str:
    from .notes import NOTES
    by_id = {p["id"]: p for p in papers}
    parts = []
    for pid in dict.fromkeys(ids):
        p = by_id.get(pid)
        if not p:
            continue
        text = section_text(p["body"], DETAIL_SECTIONS)[:3500]
        note = ""
        npath = cfg["vault"] / NOTES / f"{pid}.md"
        if npath.exists():
            note = "\n  USER'S NOTES: " + " ".join(read_page(npath)[1].split())[:1500]
        parts.append(f"[@{pid}] {p['title']}\n  {text}{note}")
    return "\n\n".join(parts)


# ---------- rendering citations ----------

def link_citations(cfg, text: str, papers: list[dict]) -> tuple[str, list[str]]:
    """Replace [@id] with markdown links to the wiki; returns (text, cited ids). Unknown ids are flagged."""
    by_id = {p["id"]: p for p in papers}
    base = (cfg.get("site_url") or "").rstrip("/")
    cited: list[str] = []

    def label(p):
        if not p["authors"]:
            return " ".join(p["title"].split()[:4])
        last = p["authors"][0].split()[-1]
        return f"{last}{' et al.' if len(p['authors']) > 1 else ''} {p['year'] or ''}".strip()

    def repl(m):
        pid = m.group(1)
        p = by_id.get(pid)
        if not p:
            return f"[?{pid}]"
        if pid not in cited:
            cited.append(pid)
        return f"[{label(p)}]({base}/papers/{quote(pid)}/)"
    return CITE.sub(repl, text), cited


def sanitize_for_html(text: str) -> str:
    """Model output -> safe Markdown for rendering: no raw HTML, no non-http(s)/relative link targets."""
    text = escape(text, quote=False)
    return re.sub(r"\]\(\s*(?!https?://|/)[^)]*\)", "](#)", text)


# ---------- main entry ----------

def ask(cfg, question: str, concept: str = "", review: str = "", since: int | None = None,
        detail: bool = True) -> dict:
    """Returns {answer (markdown with links), cited (ids), n_papers, detailed (ids), cost}."""
    question = question.strip()
    if not question:
        raise AskError("Ask a question.")
    con = db()
    cap = float((cfg.get("budget") or {}).get("ask_daily_usd", 3.0))
    if spent_purpose_today(con, "ask") >= cap:
        raise AskError(f"Today's question budget (${cap:.2f}) is used up; it resets at 00:00 UTC.")
    papers = load_corpus(cfg, concept, review, since)
    if not papers:
        raise AskError("No papers match those filters.")
    corpus = corpus_text(cfg, papers)
    if len(corpus) > MAX_CORPUS_CHARS:
        raise AskError(f"{len(papers)} papers is too many to read at once; narrow it with a concept, review or year.")

    rid = secrets.token_hex(4)
    model = (cfg.get("llm") or {}).get("ask_model") or cfg["llm"]["summary_model"]
    scope = []
    if concept: scope.append(f"concept {concept}")
    if review: scope.append(f"review {review}")
    if since: scope.append(f"year ≥ {since}")
    header = f"LIBRARY ({len(papers)} papers{', filtered: ' + ', '.join(scope) if scope else ''})\n\n{corpus}\n\n"

    s1 = claude(cfg, header + STAGE1_INSTR + question, system=SYSTEM, model=model, schema=STAGE1_SCHEMA,
                purpose="ask", ref=f"ask:{rid}:1")
    answer, ids = s1["answer"], [i for i in s1.get("need_detail", []) if i in {p["id"] for p in papers}]
    if detail and ids:
        s2 = claude(cfg, header + "DETAILS:\n" + detail_text(cfg, papers, ids) + "\n\n" + STAGE2_INSTR + question,
                    system=SYSTEM, model=model, schema=STAGE2_SCHEMA, purpose="ask", ref=f"ask:{rid}:2")
        answer = s2["answer"]
    else:
        ids = []
    linked, cited = link_citations(cfg, answer, papers)
    cost = con.execute("SELECT COALESCE(SUM(usd),0) FROM spend WHERE ref LIKE ?", (f"ask:{rid}:%",)).fetchone()[0]
    log.info("ask %s: %d papers, %d detailed, %d cited, $%.3f", rid, len(papers), len(ids), len(cited), cost)
    return {"answer": linked, "cited": cited, "n_papers": len(papers), "detailed": ids, "cost": float(cost)}
