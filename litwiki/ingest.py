"""Turn a source document into a paper page + concept backlinks."""
import json
import re
from datetime import date

import requests

from . import textract, vault
from .common import db, kv_get, log, read_page, write_page
from .llm import BudgetExceeded, claude, spent_purpose_today
from .zotero import Zotero, best_attachment, children, collection_paths

SYSTEM = """You are a careful research assistant building a literature-review wiki.
You summarise ONE document for a security / software-engineering / ML researcher.
Rules:
- Every finding and limitation must be supported by the provided text. Do not use outside knowledge
  about the paper. If the text is abstract-only, keep findings to what the abstract states.
- For each finding give the [page N] (or [section N]) marker number where the support appears,
  the paper's section name/number if visible, and a SHORT VERBATIM quote (8-30 words) copied
  exactly from that page. The quote is machine-checked against the text, so copy it character
  for character (you may drop a hyphenation break).
- Prefer concrete, checkable claims (numbers, comparisons, what was built/evaluated) over vague praise.
- Concepts: pick 1-5 slugs from the existing catalog that this document substantively contributes to.
  Only propose a new concept (max 2) if the document's central topic is not covered by any existing
  concept; new concepts must be broad research areas at the same granularity as the catalog
  (e.g. "Protocol fuzzing", not "Fuzzing MQTT brokers with GPT-4")."""

CLAIM = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "page": {"type": ["integer", "null"]},
        "section": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["claim", "page", "section", "quote"],
}
SCHEMA = {
    "type": "object",
    "properties": {
        "doc_title": {"type": "string", "description": "title as printed in the document"},
        "doc_authors": {"type": "array", "items": {"type": "string"}, "description": "authors as printed (may be empty)"},
        "doc_year": {"type": ["integer", "null"]},
        "tldr": {"type": "string", "description": "2-3 sentences"},
        "problem": {"type": "string", "description": "1 short paragraph"},
        "approach": {"type": "string", "description": "1 short paragraph"},
        "findings": {"type": "array", "items": CLAIM, "minItems": 1, "maxItems": 8},
        "limitations": {"type": "array", "items": CLAIM, "maxItems": 4},
        "concepts": {"type": "array", "items": {
            "type": "object",
            "properties": {"slug": {"type": "string"}, "why": {"type": "string", "description": "<=20 words"}},
            "required": ["slug", "why"]}},
        "new_concepts": {"type": "array", "maxItems": 2, "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string", "description": "one sentence"},
                "search_terms": {"type": "array", "items": {"type": "string"}, "description": "3-6 literature search phrases"},
                "why": {"type": "string", "description": "<=20 words: how this document relates"},
            },
            "required": ["title", "description", "search_terms", "why"]}},
    },
    "required": ["doc_title", "doc_authors", "doc_year", "tldr", "problem", "approach", "findings", "limitations", "concepts", "new_concepts"],
}


# ---------- metadata ----------

def zotero_meta(con, d: dict) -> dict:
    creators = [c for c in d.get("creators", []) if c.get("creatorType") in ("author", "programmer", "presenter", "editor")]
    authors = [c.get("name") or f"{c.get('firstName', '')} {c.get('lastName', '')}".strip() for c in creators]
    m = re.search(r"\b(19|20)\d{2}\b", d.get("date", ""))
    venue = next((d[k] for k in ("publicationTitle", "proceedingsTitle", "bookTitle", "websiteTitle",
                                 "conferenceName", "repository", "publisher", "university") if d.get(k)), "")
    title = d.get("title") or d.get("filename") or "Untitled"
    return {
        "title": title, "authors": authors, "year": int(m.group(0)) if m else None, "venue": venue,
        "doi": d.get("DOI", ""), "url": d.get("url", ""), "abstract": d.get("abstractNote", ""),
        "item_type": d["itemType"], "zotero_key": d["key"],
        "zotero_tags": sorted({t["tag"] for t in d.get("tags", [])}),
        "collections": collection_paths(con, d),
        "date_added": d.get("dateAdded", "")[:10],
    }


MANAGED_TAG_PREFIXES = ("col/", "zt/", "concept/", "source/", "review/")


def _tag_slug(text: str) -> str:
    """Obsidian tag segment: letters, digits, _ and -, no spaces; must not be all digits."""
    t = re.sub(r"[^\w-]+", "-", text.replace("'", "").replace("\u2019", "").lower(), flags=re.UNICODE).strip("-_")
    return t if t and not t.isdigit() else ""


def managed_tags(fm: dict) -> list[str]:
    """Tags derived from frontmatter, in Obsidian's native `tags` property so tag search / the
    tag pane work. Nested (a/b/c): searching `tag:#col/masters` also matches `col/masters/thesis/...`."""
    tags = []
    for c in fm.get("collections") or []:
        segs = [_tag_slug(x) for x in str(c).split(" / ")]
        if all(segs):
            tags += ["col/" + "/".join(segs[:i]) for i in range(1, len(segs) + 1)]
    tags += [f"zt/{t}" for t in map(_tag_slug, fm.get("zotero_tags") or []) if t]
    tags += [f"concept/{c}" for c in fm.get("concepts") or []]
    tags += [f"review/{r}" for r in fm.get("reviews") or []]
    if fm.get("source"):
        tags.append(f"source/{_tag_slug(str(fm['source']).split(':')[0])}")
    return list(dict.fromkeys(tags))


def apply_tags(fm: dict) -> bool:
    """Refresh the managed tags in fm['tags'], keeping any tags you added yourself. True if changed."""
    old = fm.get("tags") or []
    if isinstance(old, str):
        old = [old]
    mine = [t for t in old if not str(t).startswith(MANAGED_TAG_PREFIXES)]
    new = mine + managed_tags(fm)
    if new == old:
        return False
    fm["tags"] = new
    return True


def frontmatter(meta: dict, source: str, pdf_rel, concepts: list[str], key: str, stats: dict,
                fulltext: bool = True) -> dict:
    fm = {
        "title": meta["title"], "authors": meta.get("authors", []), "year": meta.get("year"),
        "venue": meta.get("venue", ""), "doi": meta.get("doi", ""), "url": meta.get("url", ""),
        "key": key, "source": source,
    }
    if meta.get("zotero_key"):
        fm["zotero_key"] = meta["zotero_key"]
        fm["zotero_tags"] = meta.get("zotero_tags", [])
        fm["collections"] = meta.get("collections", [])
    fm.update({"pdf": pdf_rel, "concepts": concepts, "ingested": date.today().isoformat(),
               "fulltext": fulltext, "needs_pdf": pdf_rel is None, "claims_verified": f"{stats['ok']}/{stats['total']}", "status": "auto"})
    apply_tags(fm)
    return fm


# ---------- documents ----------

def load_document(cfg, path, unit_hint=None):
    if path.suffix == ".pdf":
        return textract.pdf_pages(path), "page"
    return textract.html_pages(path.read_text(errors="ignore")), "section"


def zotero_document(cfg, con, d: dict):
    """Download the best attachment; return (pages, unit, pdf_rel) or (None, None, None)."""
    att = best_attachment([d]) if d["itemType"] == "attachment" else best_attachment(children(con, d["key"]))
    if not att:
        return None, None, None
    ext = ".pdf" if att.get("contentType") == "application/pdf" else ".html"
    rel = f"{vault.PDFS}/{d['key']}{ext}"
    dest = cfg["vault"] / rel
    if not dest.exists() and not Zotero(cfg).download_file(att["key"], dest):
        return None, None, None
    if ext == ".html" and dest.read_bytes()[:2] == b"PK":  # zipped snapshot
        import io, zipfile
        if not zipfile.is_zipfile(dest):  # truncated/corrupt download: fetch once more
            dest.unlink()
            if not Zotero(cfg).download_file(att["key"], dest) or not zipfile.is_zipfile(dest):
                dest.unlink(missing_ok=True)
                raise NoContent(f"snapshot {att['key']} is a corrupt zip")
        with zipfile.ZipFile(io.BytesIO(dest.read_bytes())) as zf:
            name = max((n for n in zf.namelist() if n.endswith((".html", ".htm"))), key=lambda n: zf.getinfo(n).file_size)
            dest.write_bytes(zf.read(name))
    pages, unit = load_document(cfg, dest)
    return pages, unit, rel


def fetch_document(cfg, url: str, stem: str, guard: bool = False):
    """Fetch an open-access PDF or web page for a discovered item. guard=True (user-supplied
    URLs) refuses private/loopback/tailnet addresses, including via redirects."""
    if guard:
        from .manual import fetch_public
        r = fetch_public(url, timeout=120)
    else:
        r = requests.get(url, timeout=120, headers={"User-Agent": "litwiki/0.1 (personal literature wiki)"})
    r.raise_for_status()
    is_pdf = r.content[:5] == b"%PDF-"
    rel = f"{vault.PDFS}/{stem}{'.pdf' if is_pdf else '.html'}"
    dest = cfg["vault"] / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    pages, unit = load_document(cfg, dest)
    return pages, unit, rel


# ---------- core ----------

class NoContent(Exception):
    pass


def find_page(v, doi: str, title: str):
    """Existing paper page with the same DOI or (normalised) title, if any."""
    norm = lambda t: re.sub(r"[^a-z0-9]", "", (t or "").lower())
    for p in (v / vault.PAPERS).glob("*.md"):
        fm, _ = read_page(p)
        if (doi and (fm.get("doi") or "").lower() == doi.lower()) or (title and norm(fm.get("title")) == norm(title)):
            return p, fm
    return None, None


def summarise_and_write(cfg, *, key: str, meta: dict, pages, unit, pdf_rel, source: str, purpose: str,
                        page_rel: str | None = None) -> str:
    v = cfg["vault"]
    fulltext = bool(pages) and sum(len(p) for p in pages) > 1500
    if fulltext:
        text, truncated = textract.build_prompt_text(pages, cfg["llm"]["max_text_chars"], unit)
    else:
        # No (usable) full text, e.g. a scanned PDF: summarise from the abstract instead.
        if len(meta.get("abstract") or "") < 200:
            raise NoContent("needs_pdf: no file in Zotero storage (or unreadable) and no abstract")
        unit, pages = "abstract", [meta.get("abstract", "")]
        text, truncated = f"=== [abstract 1] ===\n{pages[0] or '(no abstract)'}", False

    prompt = (
        f"EXISTING CONCEPT CATALOG (slug: title — description):\n{vault.concept_catalog(v)}\n\n"
        f"DOCUMENT METADATA:\nTitle: {meta['title']}\nAuthors: {', '.join(meta.get('authors', [])[:10])}\n"
        f"Year: {meta.get('year')}\nVenue: {meta.get('venue')}\n"
        f"{'Zotero collections: ' + '; '.join(meta.get('collections', [])) if meta.get('collections') else ''}\n\n"
        f"DOCUMENT TEXT ({'full text' if fulltext else 'ABSTRACT ONLY'}; markers are [{unit} N]):\n{text}"
    )
    s = claude(cfg, prompt, system=SYSTEM, model=cfg["llm"]["summary_model"], schema=SCHEMA,
               purpose=purpose, ref=key)

    # Bare files (standalone Zotero attachments) have no real metadata: use what's printed in the document.
    if meta.get("item_type") == "attachment" or not meta.get("authors"):
        if s.get("doc_title") and meta.get("item_type") == "attachment":
            meta["title"] = s["doc_title"]
        meta["authors"] = meta.get("authors") or s.get("doc_authors") or []
        meta["year"] = meta.get("year") or s.get("doc_year")

    # Verify every quote against the extracted text; fix page numbers where the quote is elsewhere.
    stats = {"ok": 0, "total": 0}
    for c in s["findings"] + s.get("limitations", []):
        page, status = textract.locate_quote(c.get("quote", ""), pages, c.get("page"))
        c["page"], c["status"] = page, status
        stats["total"] += 1
        stats["ok"] += status != "unverified"

    # Resolve concepts: existing slugs, plus vetted new ones.
    existing = vault.concepts(v)
    chosen = []
    for c in s["concepts"]:
        slug = c["slug"] if c["slug"] in existing else vault.resolve_concept(v, c["slug"])
        if slug and slug not in [x["slug"] for x in chosen]:
            chosen.append({"slug": slug, "why": c["why"]})
    for nc in s.get("new_concepts", []):
        slug = vault.resolve_concept(v, nc["title"]) or vault.create_concept(
            v, nc["title"], nc["description"], nc["search_terms"], origin=f"proposed while ingesting {key}")
        if slug not in [x["slug"] for x in chosen]:
            chosen.append({"slug": slug, "why": nc["why"]})
            log.info("new concept: %s", slug)
    s["concepts"] = chosen
    titles = {k: fm["title"] for k, fm in vault.concepts(v).items()}

    page_rel = page_rel or vault.paper_filename(v, meta, key)
    body = vault.render_paper(meta, s, titles, pdf_rel, unit, truncated, fulltext)
    fm = frontmatter(meta, source, pdf_rel, [c["slug"] for c in chosen], key, stats, fulltext)
    write_page(v / page_rel, fm, body)
    for c in chosen:
        vault.add_backlink(v, c["slug"], page_rel, meta["title"], meta.get("year"), c["why"])
    log.info("wrote %s (%d/%d quotes verified, %d concepts)", page_rel, stats["ok"], stats["total"], len(chosen))
    return page_rel


def refresh_metadata(cfg, con, row) -> None:
    """Zotero metadata changed for an ingested item: update frontmatter only."""
    d = json.loads(row["data"])
    path = cfg["vault"] / row["page"]
    if path.exists():
        meta = zotero_meta(con, d)
        fm, body = read_page(path)
        changed = False
        for k in ("title", "authors", "year", "venue", "doi", "url", "zotero_tags", "collections"):
            if meta.get(k) not in (None, "") and fm.get(k) != meta[k]:
                fm[k], changed = meta[k], True
        changed = apply_tags(fm) or changed
        if changed:
            write_page(path, fm, body)
    con.execute("UPDATE items SET status='done' WHERE key=?", (row["key"],))
    con.commit()


def ingest_zotero(cfg, limit: int | None = None) -> dict:
    """Process queued Zotero items. Items added after install are 'new' and bypass the
    backfill cap; the historical backlog is metered by budget.backfill_daily_usd."""
    con = db()
    installed = kv_get(con, "installed_at", "9999")
    for row in con.execute("SELECT * FROM items WHERE status='refresh'").fetchall():
        refresh_metadata(cfg, con, row)
    rows = con.execute("SELECT * FROM items WHERE status='queued'").fetchall()
    rows = sorted(rows, key=lambda r: json.loads(r["data"]).get("dateAdded", ""), reverse=True)
    done = failed = 0
    for row in rows:
        if limit is not None and done >= limit:
            break
        d = json.loads(row["data"])
        backlog = d.get("dateAdded", "") < installed
        if backlog and spent_purpose_today(con, "summary:backfill") >= cfg["budget"]["backfill_daily_usd"]:
            log.info("backfill daily cap reached; %d items still queued",
                     con.execute("SELECT COUNT(*) FROM items WHERE status='queued'").fetchone()[0])
            break
        try:
            meta = zotero_meta(con, d)
            page = None
            path, fm = find_page(cfg["vault"], meta["doi"], meta["title"])
            if path and str(fm.get("source", "")).startswith("discovery"):
                # Saved to Zotero after being discovered: adopt the existing page.
                page = f"{vault.PAPERS}/{path.name}"
                if fm.get("fulltext", True):
                    _, body = read_page(path)
                    fm.update(zotero_key=d["key"], zotero_tags=meta["zotero_tags"], collections=meta["collections"])
                    apply_tags(fm)
                    write_page(path, fm, body)
                else:
                    page = None if fm.get("status") != "auto" else page  # user-edited: keep, add a new page
                    pages, unit, pdf_rel = zotero_document(cfg, con, d)
                    page = summarise_and_write(cfg, key=d["key"], meta=meta, pages=pages, unit=unit,
                                               pdf_rel=pdf_rel, source="zotero", purpose="summary:new",
                                               page_rel=page)
            if page is None:
                pages, unit, pdf_rel = zotero_document(cfg, con, d)
                page = summarise_and_write(cfg, key=d["key"], meta=meta, pages=pages, unit=unit, pdf_rel=pdf_rel,
                                           source="zotero", purpose="summary:backfill" if backlog else "summary:new")
            pfm, _ = read_page(cfg["vault"] / page)
            con.execute("UPDATE items SET status='done', page=?, note=?, updated=datetime('now') WHERE key=?",
                        (page, "needs_pdf: abstract-only page" if pfm.get("needs_pdf") else None, d["key"]))
            done += 1
        except BudgetExceeded:
            break
        except NoContent as e:
            con.execute("UPDATE items SET status='skipped', note=? WHERE key=?", (str(e), d["key"]))
        except Exception as e:
            log.exception("ingest failed for %s", d["key"])
            con.execute("UPDATE items SET status='failed', note=? WHERE key=?", (str(e)[:500], d["key"]))
            failed += 1
        con.commit()
    return {"done": done, "failed": failed}


def retag_vault(cfg) -> int:
    """Recompute managed tags on every paper page (frontmatter only; bodies untouched)."""
    n = 0
    for path in sorted((cfg["vault"] / vault.PAPERS).glob("*.md")):
        fm, body = read_page(path)
        if fm and apply_tags(fm):
            write_page(path, fm, body)
            n += 1
    return n
