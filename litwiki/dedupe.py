"""Find and merge duplicate paper pages: the same paper listed twice in Zotero, saved once as a bare URL and once
as the real item, a preprint next to its published version, or in Zotero and in a discovery digest.

Merging keeps one page and folds the others into it: collections, Zotero tags, concepts and review tags are
unioned, the other Zotero keys are remembered (so later Zotero syncs don't recreate or overwrite anything), notes
are concatenated, links to the dropped pages are rewritten, and the dropped page's stem stays resolvable as an
Obsidian alias. The kept page's body is never rewritten."""
import re
from datetime import date
from pathlib import Path

from . import notes as notesmod, vault
from .common import log, read_page, write_atomic, write_page

_ARXIV = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|10\.48550/arxiv\.)(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})", re.I)
_ARXIV_DOI = "10.48550/"
MIN_TITLE = 20  # shorter normalised titles ("Introduction", "Home") are too generic to match on


def norm(t) -> str:
    return re.sub(r"[^a-z0-9]", "", str(t or "").lower())


def identity(fm: dict) -> tuple[str, set, str]:
    """(doi, arxiv ids, normalised title) for a page's frontmatter or Zotero metadata."""
    doi = str(fm.get("doi") or "").lower().strip()
    ids = {m.group(1).lower() for s in (str(fm.get("url") or ""), doi) for m in _ARXIV.finditer(s)}
    return doi, ids, norm(fm.get("title"))


def _surnames(fm: dict) -> set:
    return {norm(str(a).split()[-1]) for a in (fm.get("authors") or []) if str(a).split()}


def same_paper(a: dict, b: dict) -> str | None:
    """Why a and b are the same paper, or None. Deliberately exact: similar titles ("Lecture 02" vs "Lecture 03")
    are different papers, so only DOI, arXiv id or an identical (long) title count."""
    da, ia, ta = identity(a)
    db, ib, tb = identity(b)
    if da and da == db:
        return "same DOI"
    if ia & ib:
        return "same arXiv id"
    if ta and ta == tb and len(ta) >= MIN_TITLE:
        real = lambda d: d and not d.startswith(_ARXIV_DOI)  # a preprint DOI may differ from the published one
        sa, sb = _surnames(a), _surnames(b)
        authors_ok = not sa or not sb or bool(sa & sb)  # same title by different people = different documents
        if authors_ok and not (real(da) and real(db) and da != db):
            return "same title"
    return None


def load_pages(v: Path) -> list[dict]:
    out = []
    for p in sorted((v / vault.PAPERS).glob("*.md")):
        fm, _ = read_page(p)
        if fm:
            fm["_stem"] = p.stem
            out.append(fm)
    return out


def keeper_score(v: Path, fm: dict) -> float:
    """Higher = better page to keep. A page you edited or took notes on always wins."""
    stem, s = fm["_stem"], 0.0
    if fm.get("status") not in (None, "auto"):
        s += 100  # you edited the page
    if (v / notesmod.NOTES / f"{stem}.md").exists():
        s += 100
    if not str(fm.get("title", "")).lower().startswith("http"):
        s += 30   # a bare URL as the title means a web-page item, not the paper itself
    s += 10 * bool(fm.get("doi")) + 10 * bool(fm.get("fulltext")) + 5 * bool(fm.get("zotero_key"))
    m = re.match(r"(\d+)/(\d+)", str(fm.get("claims_verified", "")))
    if m and int(m.group(2)):
        s += 5 * int(m.group(1)) / int(m.group(2))
    return s - 0.01 * len(stem)  # on a tie prefer the shortest name (no collision suffix)


def find_groups(v: Path) -> list[dict]:
    """[{keep, drop: [...], reasons: [...]}] for every set of pages that are the same paper."""
    pages = load_pages(v)
    parent = list(range(len(pages)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    why: dict[int, set] = {}
    for i in range(len(pages)):
        for j in range(i + 1, len(pages)):
            if (r := same_paper(pages[i], pages[j])):
                parent[find(j)] = find(i)
                why.setdefault(i, set()).add(r)
                why.setdefault(j, set()).add(r)
    comps: dict[int, list[int]] = {}
    for i in range(len(pages)):
        comps.setdefault(find(i), []).append(i)
    groups = []
    for idx in comps.values():
        if len(idx) < 2:
            continue
        ranked = sorted((pages[i] for i in idx), key=lambda fm: -keeper_score(v, fm))
        reasons = sorted(set().union(*(why.get(i, set()) for i in idx)))
        groups.append({"keep": ranked[0], "drop": ranked[1:], "reasons": reasons})
    return sorted(groups, key=lambda g: g["keep"]["_stem"])


def _union(*lists) -> list:
    return list(dict.fromkeys(x for l in lists for x in (l or [])))


def attach_zotero_item(path: Path, key: str, meta: dict) -> None:
    """A new Zotero item turned out to be a paper we already have a page for: file it under that page
    (collections, tags, key) instead of summarising it again."""
    from .ingest import apply_tags
    fm, body = read_page(path)
    fm["also_zotero_keys"] = _union(fm.get("also_zotero_keys"), [key] if key != fm.get("zotero_key") else [])
    fm["collections"] = _union(fm.get("collections"), meta.get("collections"))
    fm["zotero_tags"] = sorted(set(fm.get("zotero_tags") or []) | set(meta.get("zotero_tags") or []))
    for k in ("doi", "url", "venue", "year"):
        if not fm.get(k) and meta.get(k):
            fm[k] = meta[k]
    apply_tags(fm)
    write_page(path, fm, body)


def _stem(ref: str) -> str:
    return Path(ref.strip()).name.removesuffix(".md")


def resolve(v: Path, ref: str) -> str:
    """A page stem (with or without papers/ and .md) or a Zotero key -> stem."""
    stem = _stem(ref)
    if (v / vault.PAPERS / f"{stem}.md").exists():
        return stem
    for fm in load_pages(v):
        if ref.strip() in (fm.get("zotero_key"), fm.get("key")) or ref.strip() in (fm.get("also_zotero_keys") or []):
            return fm["_stem"]
    raise SystemExit(f"no paper page or Zotero key matches {ref!r}")


def _rewrite_links(v: Path, drop_stems: list[str], keep_stem: str) -> int:
    pats = [re.compile(r"\[\[(papers/)?" + re.escape(d) + r"(?=[\]|#])") for d in drop_stems]
    n = 0
    for p in v.rglob("*.md"):
        rel = p.relative_to(v).as_posix()
        if rel.startswith((".", "_")) or ".sync-conflict-" in rel or p.stem in drop_stems and rel.startswith("papers/"):
            continue
        text = p.read_text(encoding="utf-8")
        new = text
        for pat in pats:
            new = pat.sub(lambda m: f"[[{m.group(1) or ''}{keep_stem}", new)
        if new != text:
            write_atomic(p, new)
            n += 1
    return n


def merge(cfg, con, keep: str, drops: list[str], dry_run: bool = False) -> dict:
    """Fold the pages `drops` into `keep` (all stems). Returns a summary of what changed."""
    from .ingest import apply_tags
    v = cfg["vault"]
    drops = [d for d in dict.fromkeys(drops) if d != keep]
    kpath = v / vault.PAPERS / f"{keep}.md"
    kfm, kbody = read_page(kpath)
    keep_rel = f"{vault.PAPERS}/{keep}.md"
    summary = {"keep": keep, "dropped": drops, "concepts_added": [], "notes_merged": [], "links_rewritten": 0}
    dropped = []
    for d in drops:
        dpath = v / vault.PAPERS / f"{d}.md"
        if not dpath.exists():
            raise SystemExit(f"{d}: no such paper page")
        dfm, _ = read_page(dpath)
        dropped.append((d, dpath, dfm))

    all_concepts = _union(kfm.get("concepts"), *(dfm.get("concepts") for _, _, dfm in dropped))
    summary["concepts_added"] = [c for c in all_concepts if c not in (kfm.get("concepts") or [])]
    summary["notes_merged"] = [d for d, _, _ in dropped if (v / notesmod.NOTES / f"{d}.md").exists()]
    if dry_run:
        return summary

    # Concept lines: carry over the dropped page's "why" for concepts the kept page lacked, then remove its lines.
    why = {}
    for d, _, dfm in dropped:
        for c in dfm.get("concepts") or []:
            why.setdefault(c, vault.backlink_why(v, c, f"{vault.PAPERS}/{d}") or "merged duplicate")
        vault.remove_backlinks(v, f"{vault.PAPERS}/{d}.md")

    kfm["concepts"] = all_concepts
    kfm["collections"] = _union(kfm.get("collections"), *(dfm.get("collections") for _, _, dfm in dropped))
    kfm["zotero_tags"] = sorted(set(kfm.get("zotero_tags") or []).union(*(set(dfm.get("zotero_tags") or []) for _, _, dfm in dropped)))
    kfm["reviews"] = sorted(set(kfm.get("reviews") or []).union(*(set(dfm.get("reviews") or []) for _, _, dfm in dropped)))
    if not kfm["reviews"]:
        kfm.pop("reviews")
    other_keys = [k for _, _, dfm in dropped for k in [dfm.get("zotero_key"), *(dfm.get("also_zotero_keys") or [])] if k]
    kfm["also_zotero_keys"] = [k for k in _union(kfm.get("also_zotero_keys"), other_keys) if k != kfm.get("zotero_key")]
    if not kfm["also_zotero_keys"]:
        kfm.pop("also_zotero_keys")
    kfm["aliases"] = _union(kfm.get("aliases"), [d for d, _, _ in dropped],  # old links keep working in Obsidian
                            [dfm["title"] for _, _, dfm in dropped if dfm.get("title") and dfm["title"] != kfm.get("title")
                             and not str(dfm["title"]).startswith("http")])
    kfm["merged_from"] = _union(kfm.get("merged_from"), [d for d, _, _ in dropped])
    kfm["merged_on"] = date.today().isoformat()
    for k in ("doi", "url", "venue", "year"):
        for _, _, dfm in dropped:
            if not kfm.get(k) and dfm.get(k):
                kfm[k] = dfm[k]
    if not kfm.get("pdf"):
        for _, _, dfm in dropped:
            if dfm.get("pdf"):
                kfm["pdf"], kfm["needs_pdf"] = dfm["pdf"], False
                break
    apply_tags(kfm)
    write_page(kpath, kfm, kbody)
    for c in all_concepts:
        vault.add_backlink(v, c, keep_rel, kfm["title"], kfm.get("year"), why.get(c, "merged duplicate"))

    # Notes: append the dropped pages' notes to the kept page's note file.
    for d, _, _ in dropped:
        src = v / notesmod.NOTES / f"{d}.md"
        if not src.exists():
            continue
        dst = v / notesmod.NOTES / f"{keep}.md"
        sfm, sbody = read_page(src)
        if dst.exists():
            dfm2, dbody = read_page(dst)
            write_page(dst, dfm2, dbody.rstrip("\n") + f"\n\n---\n*Merged from `{d}`:*\n\n" + sbody.strip("\n") + "\n")
        else:
            write_page(dst, sfm, sbody)
        src.unlink()

    summary["links_rewritten"] = _rewrite_links(v, [d for d, _, _ in dropped], keep)

    # Database: dropped Zotero items now live on the kept page (so syncs/refreshes don't recreate or overwrite it).
    for d, _, dfm in dropped:
        page = f"{vault.PAPERS}/{d}.md"
        con.execute("UPDATE items SET page=?, note=? WHERE page=?", (keep_rel, f"merged into {keep}", page))
        con.execute("DELETE FROM cite_meta WHERE page=?", (page,))
    con.commit()
    for _, dpath, _ in dropped:
        dpath.unlink()
    log.info("merged %s into %s", drops, keep)
    return summary
