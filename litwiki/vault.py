"""Vault layout, page rendering, and concept-page backlink maintenance."""
import re
from datetime import date
from pathlib import Path

from .common import get_block, read_page, replace_block, slugify, write_page

PAPERS, CONCEPTS, DIGESTS, PDFS, REVIEWS = "papers", "concepts", "digests", "_pdfs", "reviews"


def concepts(vault: Path) -> dict[str, dict]:
    """slug -> frontmatter (+ 'path') for every concept page."""
    out = {}
    for p in sorted((vault / CONCEPTS).glob("*.md")):
        fm, _ = read_page(p)
        fm["path"] = p
        fm.setdefault("title", p.stem)
        out[p.stem] = fm
    return out


def reviews(vault: Path) -> dict[str, dict]:
    """slug -> frontmatter (+ 'path', 'text' = the page body you wrote) for every review page."""
    out = {}
    for p in sorted((vault / REVIEWS).glob("*.md")):
        fm, body = read_page(p)
        fm.update(path=p, text=body)
        fm.setdefault("title", p.stem)
        out[p.stem] = fm
    return out


def create_review(vault: Path, title: str) -> str:
    """Scaffold reviews/<slug>.md for you to fill in (research question, scope, search terms)."""
    slug = slugify(title, 50)
    path = vault / REVIEWS / f"{slug}.md"
    if path.exists():
        return slug
    fm = {
        "title": title,
        "search_terms": [],          # phrases searched on arXiv + OpenAlex; add several, 1-4 words each
        "arxiv_categories": [],      # optional, e.g. cs.CR, cs.SE
        "since_year": date.today().year - 5,
        "max_per_term": 100,         # candidates fetched per term per source
        "exclude": "",               # topics / paper types to score down, in plain words
        "run": False,                # set to true in Obsidian to run a search at the next hourly job
        "created": date.today().isoformat(),
    }
    body = (f"# {title}\n\n## Research question\n\n_What is this review trying to find out?_\n\n"
            "## Scope\n\n_What is in scope, and what is not. Candidates are scored against this text._\n\n"
            f"## How to use\n\nFill in `search_terms` above, then set `run: true` (or run `litwiki search {slug}`). "
            f"A digest appears under `digests/`; tick items to ingest them. Papers ingested from it are tagged "
            f"`#review/{slug}`.\n")
    write_page(path, fm, body)
    return slug


def concept_catalog(vault: Path) -> str:
    lines = []
    for slug, fm in concepts(vault).items():
        alias = f" (aka {', '.join(fm['aliases'])})" if fm.get("aliases") else ""
        lines.append(f"- {slug}: {fm['title']}{alias} — {fm.get('description', '')}")
    return "\n".join(lines) or "(no concepts yet)"


def resolve_concept(vault: Path, title_or_slug: str) -> str | None:
    """Match a proposed concept to an existing one by slug, title, or alias."""
    want = slugify(title_or_slug)
    for slug, fm in concepts(vault).items():
        names = [slug, fm["title"], *(fm.get("aliases") or [])]
        if want in {slugify(n) for n in names}:
            return slug
    return None


def create_concept(vault: Path, title: str, description: str, search_terms: list[str],
                   arxiv_categories: list[str] | None = None, origin: str = "") -> str:
    slug = slugify(title, 50)
    path = vault / CONCEPTS / f"{slug}.md"
    if path.exists():
        return slug
    fm = {
        "title": title,
        "aliases": [],
        "description": description,
        "search_terms": search_terms,
        "arxiv_categories": arxiv_categories or [],
        "discover": True,
        "created": date.today().isoformat(),
    }
    if origin:
        fm["origin"] = origin
    body = (f"# {title}\n\n{description}\n\n"
            "## Overview\n\n_Your synthesis goes here. litwiki only edits the auto-managed list below._\n\n"
            "## Papers\n\n")
    body = replace_block(body, "papers", "")
    write_page(path, fm, body)
    return slug


_LINE_TARGET = re.compile(r"\[\[([^\]|#]+)")
_LINE_YEAR = re.compile(r"\((\d{4}|n\.d\.)\)")


def add_backlink(vault: Path, concept_slug: str, paper_page: str, title: str, year, why: str) -> bool:
    """Add a paper line to a concept's auto block (append-only; user edits kept)."""
    path = vault / CONCEPTS / f"{concept_slug}.md"
    if not path.exists():
        return False
    fm, body = read_page(path)
    target = paper_page.removesuffix(".md")
    lines = [l for l in get_block(body, "papers").splitlines() if l.strip()]
    if any((m := _LINE_TARGET.search(l)) and m.group(1).strip() == target for l in lines):
        return False
    lines.append(f"- [[{target}|{title}]] ({year or 'n.d.'}) — {why}")

    def year_of(l):
        m = _LINE_YEAR.search(l)
        return m.group(1) if m and m.group(1).isdigit() else "0000"
    lines.sort(key=year_of, reverse=True)
    body = replace_block(body, "papers", "\n".join(lines))
    fm["paper_count"] = len(lines)
    write_page(path, fm, body)
    return True


def backlink_why(vault: Path, concept_slug: str, paper_page: str) -> str | None:
    """The ' — why' text of a paper's line in a concept page, if it has one."""
    path = vault / CONCEPTS / f"{concept_slug}.md"
    if not path.exists():
        return None
    target = paper_page.removesuffix(".md")
    _, body = read_page(path)
    for l in get_block(body, "papers").splitlines():
        if (m := _LINE_TARGET.search(l)) and m.group(1).strip() == target and " — " in l:
            return l.split(" — ", 1)[1].strip()
    return None


def remove_backlinks(vault: Path, paper_page: str) -> None:
    target = paper_page.removesuffix(".md")
    for p in (vault / CONCEPTS).glob("*.md"):
        fm, body = read_page(p)
        lines = [l for l in get_block(body, "papers").splitlines() if l.strip()]
        keep = [l for l in lines if not ((m := _LINE_TARGET.search(l)) and m.group(1).strip() == target)]
        if len(keep) != len(lines):
            fm["paper_count"] = len(keep)
            write_page(p, fm, replace_block(body, "papers", "\n".join(keep)))


def paper_filename(vault: Path, meta: dict, key: str) -> str:
    first = meta["authors"][0].split()[-1] if meta.get("authors") else "anon"
    stem = f"{slugify(first, 20)}{meta.get('year') or ''}-{slugify(meta['title'], 50)}"
    path = vault / PAPERS / f"{stem}.md"
    if path.exists():
        fm, _ = read_page(path)
        if fm.get("key") != key:
            stem = f"{stem}-{key.lower()[-6:]}"
    return f"{PAPERS}/{stem}.md"


def _cite(c: dict, pdf_rel: str | None, unit: str) -> str:
    page = c.get("page")
    status = c.get("status", "unverified")
    if page and pdf_rel and unit == "page":
        loc = f"[[{pdf_rel}#page={page}|p. {page}]]"
    elif page:
        loc = f"{unit} {page}"
    else:
        loc = "no location"
    if c.get("section"):
        loc += f", {c['section']}"
    flag = "" if status in ("verified", "moved") else " ⚠ _quote not found in text — check_"
    return loc + flag


def render_paper(meta: dict, s: dict, concept_titles: dict[str, str], pdf_rel: str | None,
                 unit: str, truncated: bool, fulltext: bool) -> str:
    links = []
    if pdf_rel:
        links.append(f"[[{pdf_rel}|{'PDF' if unit == 'page' else 'Snapshot'}]]")
    if meta.get("zotero_key"):
        links.append(f"[Zotero](zotero://select/library/items/{meta['zotero_key']})")
    if meta.get("doi"):
        links.append(f"[DOI](https://doi.org/{meta['doi']})")
    if meta.get("url"):
        links.append(f"[Web]({meta['url']})")
    authors = ", ".join(meta.get("authors", [])[:6]) + (" et al." if len(meta.get("authors", [])) > 6 else "")
    venue = " · ".join(x for x in [meta.get("venue"), str(meta.get("year") or "")] if x)

    out = [f"# {meta['title']}", "", f"*{authors}*  ", f"{venue}  ", " · ".join(links), ""]
    if not fulltext:
        out += ["> [!warning] Abstract only", "> No full text was available; this summary is based on "
                "the abstract/metadata only.", ""]
    elif truncated:
        out += ["> [!note] Partial text", "> The document was truncated before summarisation; "
                "later sections may not be reflected.", ""]
    out += ["## TL;DR", "", s["tldr"], "", "## Problem", "", s["problem"], "", "## Approach", "", s["approach"], ""]
    out += ["## Key findings", ""]
    for i, c in enumerate(s["findings"], 1):
        out.append(f"{i}. **{c['claim']}** — {_cite(c, pdf_rel, unit)}")
        if c.get("quote"):
            out.append(f"    > \"{c['quote']}\"")
    out.append("")
    if s.get("limitations"):
        out += ["## Limitations", ""]
        for c in s["limitations"]:
            out.append(f"- {c['claim']} — {_cite(c, pdf_rel, unit)}")
        out.append("")
    out += ["## Concepts", ""]
    for c in s["concepts"]:
        out.append(f"- [[concepts/{c['slug']}|{concept_titles.get(c['slug'], c['slug'])}]] — {c['why']}")
    out += ["", "## Notes", "", "_Your notes. litwiki never rewrites this page's body after creation; "
            "only Zotero metadata in the frontmatter is refreshed._", ""]
    return "\n".join(out)


def ingested_ts(fm: dict) -> str:
    """When a paper page was created, as a sortable ISO string (exact if `ingested_at` is set, else the day)."""
    return str(fm.get("ingested_at") or (f"{fm['ingested']}T00:00:00Z" if fm.get("ingested") else ""))


def recent_papers(vault: Path, n: int) -> list[dict]:
    """The n most recently ingested papers, newest first: [{stem, title, authors, year, ts}]."""
    rows = []
    for p in (vault / PAPERS).glob("*.md"):
        fm, _ = read_page(p)
        if not fm.get("title"):
            continue
        au = [str(a) for a in (fm.get("authors") or [])]
        rows.append({"stem": p.stem, "title": str(fm["title"]), "year": fm.get("year"), "ts": ingested_ts(fm),
                     "authors": ", ".join(au[:2]) + (" et al." if len(au) > 2 else "")})
    rows.sort(key=lambda r: (r["ts"], r["stem"]), reverse=True)
    return rows[:n]
