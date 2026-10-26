"""Home/status pages + MkDocs build with change detection and atomic swap."""
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import vault
from .common import ROOT, db, get_block, kv_get, kv_set, log, read_page, replace_block, run_lock, write_page

SITE = ROOT / "site"


def _write_if_changed(path: Path, fm: dict, block: str, intro: str = "") -> None:
    if path.exists():
        old_fm, body = read_page(path)
        if get_block(body, "auto") == block.strip():
            return
        fm = {**fm, **old_fm}
    else:
        body = intro
    write_page(path, fm, replace_block(body, "auto", block))


def update_home(cfg) -> None:
    from .llm import spent
    v, con = cfg["vault"], db()
    cs = vault.concepts(v)
    concept_lines = [f"- [[concepts/{s}|{fm['title']}]] ({fm.get('paper_count', 0)})"
                     for s, fm in sorted(cs.items(), key=lambda x: -int(x[1].get("paper_count", 0)))]
    papers = sorted((v / vault.PAPERS).glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    recent = [f"- [[papers/{r['stem']}|{r['title']}]] ({r['year'] or 'n.d.'})" for r in vault.recent_papers(v, 15)]
    rvs = vault.reviews(v)
    review_lines = [f"- [[reviews/{s}|{fm['title']}]]" for s, fm in rvs.items()]
    digests = sorted((v / vault.DIGESTS).glob("*.md"), reverse=True)[:7]
    dig = [f"- [[digests/{p.stem}|{p.stem}]]" for p in digests]
    block = "\n".join([
        f"**{len(papers)} papers · {len(cs)} concepts**", "",
        "## Literature reviews", "", *(review_lines or ["_none yet — `litwiki review-new <title>`_"]), "",
        "## Recent digests", "", *(dig or ["_none yet_"]), "",
        "## Concepts", "", *concept_lines, "",
        "## Recently added papers", "", *(recent or ["_none yet_"]),
    ])
    _write_if_changed(v / "index.md", {"title": "Home"}, block,
                      "# Literature Wiki\n\n_Edit freely above or below the auto-managed block._\n\n")

    rows = con.execute("SELECT status, COUNT(*) n FROM items WHERE status!='child' GROUP BY status").fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    flagged = con.execute("SELECT key, data, note, page FROM items WHERE note LIKE 'needs_pdf%' ORDER BY note").fetchall()
    skipped = con.execute("SELECT key, data, note FROM items WHERE status='failed' AND note IS NOT NULL").fetchall()
    import json
    lines = [
        "| Zotero items | count |", "|---|---|",
        *[f"| {k} | {n} |" for k, n in sorted(counts.items())], "",
        f"LLM spend this month: **${spent(con, 'month'):.2f}** of ${cfg['budget']['monthly_usd']:.0f}",
        "", f"## Missing PDFs ({len(flagged)})", "",
        "No file in Zotero cloud storage. Pages marked *abstract-only* were summarised from the abstract; "
        "the rest have no page yet. Attach a PDF in Zotero (and let it sync) then run `litwiki redo KEY`.", "",
    ]
    for r in flagged:
        d = json.loads(r["data"])
        title = d.get("title") or d.get("filename") or r["key"]
        page = f" → [[{r['page'].removesuffix('.md')}|page]]" if r["page"] else ""
        lines.append(f"- [{title}](zotero://select/library/items/{r['key']}) `{r['key']}` — "
                     f"{'abstract-only' if r['page'] else 'no page'}{page}")
    lines += ["", "## Failed", ""]
    for r in skipped:
        d = json.loads(r["data"])
        lines.append(f"- [{d.get('title') or d.get('filename') or r['key']}](zotero://select/library/items/{r['key']}) — {r['note']}")
    # Rounded so it only changes when something meaningful happens.
    _write_if_changed(v / "status.md", {"title": "Status"}, "\n".join(lines), "# Pipeline status\n\n")


def _fingerprint(cfg) -> str:
    h = hashlib.sha1()
    v = cfg["vault"]
    for p in sorted(v.rglob("*.md")):
        rel = p.relative_to(v).as_posix()
        if rel.startswith((".", "_pdfs/")) or ".sync-conflict-" in rel:
            continue
        st = p.stat()
        h.update(f"{rel}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    h.update(repr(cfg.get("clusters")).encode())
    h.update(repr(tuple(db().execute("SELECT COUNT(*), COALESCE(MAX(fetched),''), COALESCE(SUM(cited_by),0) FROM cite_meta").fetchone())).encode())
    for f in ("mkdocs.yml", "hooks.py"):
        h.update((SITE / f).read_bytes())
    return h.hexdigest()


def build(cfg, force: bool = False) -> dict:
    with run_lock("build"):  # cron jobs and the web UI may both ask for a build
        return _build(cfg, force)


def _build(cfg, force: bool) -> dict:
    update_home(cfg)
    con = db()
    fp = _fingerprint(cfg)
    if not force and fp == kv_get(con, "site_fingerprint"):
        return {"built": False}
    t0 = time.time()
    out = SITE / f"build-{int(t0)}"
    mkdocs = Path(sys.executable).with_name("mkdocs")
    proc = subprocess.run([str(mkdocs), "build", "-q", "-f", str(SITE / "mkdocs.yml"), "-d", str(out)],
                          capture_output=True, text=True, cwd=SITE)
    if proc.returncode != 0:
        shutil.rmtree(out, ignore_errors=True)
        raise RuntimeError(f"mkdocs build failed: {proc.stderr[-1500:]}")
    try:  # the paper map is an extra: never let it break the site build
        from .cluster import write_page
        write_page(cfg, out)
    except Exception:
        log.exception("paper map failed")
    # Atomic swap: Caddy serves site/current -> build-<ts>.
    link = SITE / "current"
    tmp = SITE / "current.tmp"
    if tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(out.name)
    os.replace(tmp, link)
    for old in SITE.glob("build-*"):
        if old != out:
            shutil.rmtree(old, ignore_errors=True)
    kv_set(con, "site_fingerprint", fp)
    secs = time.time() - t0
    log.info("site built in %.1fs", secs)
    return {"built": True, "seconds": round(secs, 1)}
