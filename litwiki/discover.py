"""Daily discovery: arXiv + OpenAlex (journals/conferences) + RSS feeds -> triage -> digest page.
Ticking `- [x]` next to a digest item (in Obsidian) queues it for ingestion on the next hourly run."""
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import feedparser
import requests

from . import vault
from .common import db, log, read_page, slugify, write_page
from .ingest import NoContent, fetch_document, summarise_and_write
from .llm import BudgetExceeded, claude
from .notify import notify

UA = {"User-Agent": "litwiki/0.1 (personal literature wiki)"}


def title_key(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())[:120]


# ---------- sources ----------

def search_arxiv(terms, cats, since: date, n: int) -> list[dict]:
    q = " OR ".join(f'abs:"{t}"' for t in terms)
    if cats:
        q = f"({q}) AND ({' OR '.join('cat:' + c for c in cats)})"
    r = requests.get("https://export.arxiv.org/api/query", headers=UA, timeout=60, params={
        "search_query": q, "sortBy": "submittedDate", "sortOrder": "descending", "max_results": n})
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(r.content).findall("a:entry", ns):
        published = e.findtext("a:published", "", ns)[:10]
        if published < since.isoformat():
            continue
        aid = e.findtext("a:id", "", ns).rsplit("/abs/", 1)[-1]
        aid = re.sub(r"v\d+$", "", aid)
        out.append({
            "id": f"arxiv:{aid}", "title": " ".join(e.findtext("a:title", "", ns).split()),
            "authors": [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)],
            "abstract": " ".join(e.findtext("a:summary", "", ns).split()),
            "date": published, "venue": "arXiv", "year": int(published[:4]),
            "url": f"https://arxiv.org/abs/{aid}", "pdf_url": f"https://arxiv.org/pdf/{aid}",
            "doi": "", "kind": "preprint",
        })
    return out


def _openalex_abstract(inv):
    if not inv:
        return ""
    pos = sorted((p, w) for w, ps in inv.items() for p in ps)
    return " ".join(w for _, w in pos)


def search_openalex(terms, since: date, n: int, mailto: str) -> list[dict]:
    params = {
        # OpenAlex rejects some punctuation (e.g. "L*"); keep words only.
        "search": " OR ".join(f'"{c}"' for t in terms if (c := " ".join(re.findall(r"[\w-]+", t)))),
        "filter": f"from_publication_date:{since.isoformat()},type:article|proceedings-article|review",
        "sort": "publication_date:desc", "per-page": n,
    }
    if mailto:
        params["mailto"] = mailto
    r = requests.get("https://api.openalex.org/works", params=params, headers=UA, timeout=60)
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        src = ((w.get("primary_location") or {}).get("source") or {})
        if "arxiv" in (src.get("display_name") or "").lower():
            continue  # covered by the arXiv source
        doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
        oa = w.get("best_oa_location") or {}
        out.append({
            "id": f"doi:{doi}" if doi else f"openalex:{w['id'].rsplit('/', 1)[-1]}",
            "title": w.get("display_name") or "", "abstract": _openalex_abstract(w.get("abstract_inverted_index")),
            "authors": [a["author"]["display_name"] for a in w.get("authorships", [])[:12]],
            "date": w.get("publication_date", ""), "year": w.get("publication_year"),
            "venue": src.get("display_name") or "", "doi": doi,
            "url": f"https://doi.org/{doi}" if doi else w["id"],
            "pdf_url": oa.get("pdf_url") or "", "kind": "article",
        })
    return out


def read_feeds(feeds, since: date) -> list[dict]:
    out = []
    for url in feeds:
        try:
            f = feedparser.parse(url, agent=UA["User-Agent"])
        except Exception as e:
            log.warning("feed %s failed: %s", url, e)
            continue
        for e in f.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            d = date(*t[:3]) if t else date.today()
            if d < since:
                continue
            summary = re.sub(r"<[^>]+>", " ", e.get("summary", ""))
            out.append({
                "id": f"url:{e.get('link')}", "title": e.get("title", ""), "abstract": " ".join(summary.split())[:2000],
                "authors": [a.get("name", "") for a in e.get("authors", [])] or [f.feed.get("title", "")],
                "date": d.isoformat(), "year": d.year, "venue": f.feed.get("title", url),
                "url": e.get("link"), "pdf_url": "", "doi": "", "kind": "blog",
            })
    return out


# ---------- triage ----------

TRIAGE_SYSTEM = """You screen newly published literature for a researcher's literature-review wiki.
Their interests are exactly the concept catalog provided. For each candidate, score relevance 0-10:
9-10 = squarely on a catalog concept and likely important; 6-8 = clearly relevant; 3-5 = tangential; 0-2 = off-topic.
Be strict: keyword overlap alone is not relevance. Penalise low-substance items (announcements, listicles).
Also weigh venue quality: subtract 2-3 points for obscure regional journals, predatory-looking venues, or
student-project-like work; established security/SE/ML/formal-methods venues and substantive arXiv preprints
need no penalty."""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "n": {"type": "integer"}, "score": {"type": "integer"},
            "concepts": {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string", "description": "<=25 words"},
        },
        "required": ["n", "score", "concepts", "why"]}}},
    "required": ["items"],
}


def triage(cfg, cands: list[dict]) -> None:
    catalog = vault.concept_catalog(cfg["vault"])
    for i in range(0, len(cands), 40):
        batch = cands[i:i + 40]
        listing = "\n\n".join(
            f"[{n}] {c['title']}\nVenue: {c['venue']} ({c['kind']})\n{c['abstract'][:700]}"
            for n, c in enumerate(batch))
        out = claude(cfg, f"CONCEPT CATALOG:\n{catalog}\n\nCANDIDATES:\n{listing}", system=TRIAGE_SYSTEM,
                     model=cfg["llm"]["triage_model"], schema=TRIAGE_SCHEMA, purpose="triage",
                     ref=f"batch{i // 40}")
        for r in out["items"]:
            if 0 <= r["n"] < len(batch):
                batch[r["n"]].update(score=r["score"], concepts=r["concepts"], why=r["why"])


# ---------- digest ----------

def discover(cfg) -> dict:
    con = db()
    v, dc = cfg["vault"], cfg["discovery"]
    since = date.today() - timedelta(days=dc["lookback_days"])
    since_oa = date.today() - timedelta(days=dc["openalex_lookback_days"])
    known = {r["id"] for r in con.execute("SELECT id FROM candidates")}
    known_titles = {title_key(r["title"]) for r in con.execute("SELECT title FROM candidates")}
    for r in con.execute("SELECT data FROM items WHERE status!='child'"):
        known_titles.add(title_key(json.loads(r["data"]).get("title", "")))

    found: dict[str, dict] = {}
    errors = []
    for slug, fm in vault.concepts(v).items():
        if not fm.get("discover", True) or not fm.get("search_terms"):
            continue
        terms = fm["search_terms"][:6]
        for name, fn in (("arxiv", lambda: search_arxiv(terms, fm.get("arxiv_categories"), since, dc["max_per_concept"])),
                         ("openalex", lambda: search_openalex(terms, since_oa, dc["max_per_concept"], dc.get("openalex_mailto", "")))):
            try:
                for c in fn():
                    c.setdefault("query_concepts", []).append(slug)
                    found.setdefault(c["id"], c)
            except Exception as e:
                errors.append(f"{name}/{slug}: {e}")
                log.warning("%s search failed for %s: %s", name, slug, e)
            time.sleep(3.1 if name == "arxiv" else 0.2)  # arXiv API etiquette
    for c in read_feeds(dc.get("feeds") or [], since):
        found.setdefault(c["id"], c)

    fresh = [c for c in found.values()
             if c["id"] not in known and title_key(c["title"]) not in known_titles and c["title"]]
    log.info("discovery: %d raw, %d new", len(found), len(fresh))
    if len(errors) > 10:
        notify(f"Discovery: {len(errors)} source queries failed (e.g. {errors[0][:200]})", level="warn")
    if fresh:
        try:
            triage(cfg, fresh)
        except BudgetExceeded:
            return {"raw": len(found), "new": len(fresh), "digest": 0}
    for c in fresh:
        con.execute("INSERT OR IGNORE INTO candidates(id, title, data, score, status) VALUES(?,?,?,?,?)",
                    (c["id"], c["title"], json.dumps(c), c.get("score", 0), "triaged"))
    picked = sorted((c for c in fresh if c.get("score", 0) >= dc["min_score"]),
                    key=lambda c: -c["score"])[:dc["digest_max"]]
    path = write_digest(cfg, picked)
    for c in picked:
        con.execute("UPDATE candidates SET status='digested', digest=? WHERE id=?", (path, c["id"]))
    con.commit()
    if picked:
        url = cfg.get("site_url", "").rstrip("/")
        link = f"\n{url}/{path.removesuffix('.md')}/" if url else ""
        top = "\n".join(f"• {c['title']} ({c['score']}/10)" for c in picked[:5])
        notify(f"Daily digest: {len(picked)} new item(s). Tick the ones to ingest in Obsidian.{link}\n{top}")
    return {"raw": len(found), "new": len(fresh), "digest": len(picked)}


def write_digest(cfg, picked: list[dict]) -> str:
    v, proxy = cfg["vault"], cfg["discovery"].get("proxy_prefix", "")
    titles = {k: fm["title"] for k, fm in vault.concepts(v).items()}
    rel = f"{vault.DIGESTS}/{date.today().isoformat()}.md"
    body = [f"# Literature digest — {date.today():%d %b %Y}", "",
            "Tick `[x]` next to items to ingest them (picked up within the hour). "
            "Paywalled items are ingested from the abstract only; for full text, save them to Zotero "
            "(via your proxy) instead and they will arrive through the Zotero sync.", ""]
    if not picked:
        body.append("_Nothing new above the relevance threshold today._")
    groups: dict[str, list] = {}
    for c in picked:
        main = next((s for s in c.get("concepts", []) if s in titles), None) or \
               next(iter(c.get("query_concepts", [])), "other")
        groups.setdefault(main, []).append(c)
    for slug, cs in groups.items():
        body += [f"## [[concepts/{slug}|{titles.get(slug, slug)}]]", ""]
        for c in cs:
            au = ", ".join(c["authors"][:3]) + (" et al." if len(c["authors"]) > 3 else "")
            links = [f"[abstract]({c['url']})"]
            if c.get("pdf_url"):
                links.append(f"[PDF]({c['pdf_url']})")
            elif c["kind"] == "article":
                links.append("no open-access PDF" + (f" · [via proxy]({proxy}{c['url']})" if proxy else ""))
            body += [f"- [ ] **{c['title']}** · {c['score']}/10 <!-- cand:{c['id']} -->",
                     f"    {au} — *{c['venue']}*, {c['date']}  ",
                     f"    {c.get('why', '')}  ",
                     f"    {' · '.join(links)}", ""]
    write_page(v / rel, {"title": f"Digest {date.today().isoformat()}", "type": "digest",
                         "items": len(picked)}, "\n".join(body))
    return rel


# ---------- approvals ----------

TICKED = re.compile(r"^\s*- \[[xX]\] .*?<!-- cand:(\S+) -->", re.M)


def ingest_approved(cfg) -> dict:
    con = db()
    v = cfg["vault"]
    done = failed = 0
    for p in sorted((v / vault.DIGESTS).glob("*.md")):
        for cid in TICKED.findall(p.read_text(encoding="utf-8")):
            row = con.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
            if not row or row["status"] not in ("digested", "triaged", "failed"):
                continue
            c = json.loads(row["data"])
            try:
                pages = unit = pdf_rel = None
                src = c.get("pdf_url") or (c["url"] if c["kind"] == "blog" else "")
                if src:
                    try:
                        pages, unit, pdf_rel = fetch_document(cfg, src, slugify(cid, 80))
                    except Exception as e:
                        log.warning("fetch %s failed (%s); falling back to abstract", src, e)
                meta = {k: c.get(k) for k in ("title", "authors", "year", "venue", "doi", "url", "abstract")}
                page = summarise_and_write(cfg, key=cid, meta=meta, pages=pages, unit=unit, pdf_rel=pdf_rel,
                                           source=f"discovery:{c['kind']}", purpose="summary:discovery")
                con.execute("UPDATE candidates SET status='ingested' WHERE id=?", (cid,))
                _mark_in_digest(p, cid, page)
                done += 1
            except BudgetExceeded:
                return {"done": done, "failed": failed}
            except NoContent as e:
                con.execute("UPDATE candidates SET status='rejected' WHERE id=?", (cid,))
                log.warning("approved %s has no content: %s", cid, e)
            except Exception:
                log.exception("approved ingest failed for %s", cid)
                con.execute("UPDATE candidates SET status='failed' WHERE id=?", (cid,))
                failed += 1
            con.commit()
    return {"done": done, "failed": failed}


def _mark_in_digest(path, cid, page_rel):
    """Link the new page from the digest line so the user can see it was ingested."""
    fm, body = read_page(path)
    marker = f"<!-- cand:{cid} -->"
    body = body.replace(marker, f"→ [[{page_rel.removesuffix('.md')}|ingested]] {marker}", 1)
    write_page(path, fm, body)
