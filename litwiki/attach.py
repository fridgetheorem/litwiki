"""Attach a PDF to a paper that has none (or a bad one): re-summarise it from the full text, in place.

Works on an existing paper page (an abstract-only page, or any page whose PDF you want to replace) and on a Zotero
item that never got a page because it had no file. The page keeps its filename, tags, collections, concept links,
merged Zotero keys, reviews and notes; only the generated summary body, `pdf`, `fulltext`, `needs_pdf` and
`claims_verified` change. Pages you edited by hand (status other than `auto`) are refused unless forced."""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import date
from pathlib import Path

from . import dedupe, textract, vault
from .common import LOG_DIR, ROOT, STATE_DIR, db, log, read_page, slugify, write_page

MAX_UPLOAD = 60 * 1024 * 1024
MIN_TEXT = 1500  # characters of extractable text below which a PDF is treated as scanned


class AttachError(Exception):
    pass


def validate(data: bytes) -> int:
    """Check an uploaded PDF (header, size, extractable text). Returns the number of text characters."""
    if len(data) > MAX_UPLOAD:
        raise AttachError("That file is over 60 MB.")
    if data[:5] != b"%PDF-":
        raise AttachError("Only PDF files are supported.")
    with tempfile.NamedTemporaryFile(suffix=".pdf", dir=STATE_DIR) as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            pages = textract.pdf_pages(Path(tmp.name))
        except Exception:
            raise AttachError("Couldn't read that PDF (it may be corrupt, encrypted or not really a PDF).")
    n = sum(len(p) for p in pages)
    if n < MIN_TEXT:
        raise AttachError("That PDF has no extractable text (scanned?). Run OCR on it first.")
    return n


def resolve_target(v: Path, con, ref: str) -> tuple[str, str]:
    """('page', stem) for an existing paper page, or ('item', zotero key) for a Zotero item with no page yet."""
    stem = Path(ref.strip()).name.removesuffix(".md")
    if (v / vault.PAPERS / f"{stem}.md").exists():
        return "page", stem
    try:
        return "page", dedupe.resolve(v, ref)
    except SystemExit:
        pass
    row = con.execute("SELECT key, page, status FROM items WHERE key=?", (ref.strip(),)).fetchone()
    if row and row["status"] != "child":
        if row["page"]:
            return "page", Path(row["page"]).name.removesuffix(".md")
        return "item", row["key"]
    raise AttachError(f"{ref!r} is not a paper page or a Zotero item key.")


def pdf_rel_for(key: str) -> str:
    """Same naming as Zotero downloads (_pdfs/KEY.pdf), so a later Zotero sync finds the file instead of refetching."""
    return f"{vault.PDFS}/{key if re.fullmatch(r'[A-Za-z0-9]{8}', key) else slugify(key, 80)}.pdf"


# Frontmatter that summarising regenerates; everything else on the page (tags, collections, reviews, aliases,
# merged keys, ingest times, ...) is carried over from the old page.
REGENERATED = ("pdf", "fulltext", "needs_pdf", "claims_verified", "status", "concepts", "tags")


def attach_pdf(cfg, ref: str, src: Path, force: bool = False, con=None) -> dict:
    from .ingest import apply_tags, summarise_and_write, zotero_meta
    v, con = cfg["vault"], con or db()
    kind, ident = resolve_target(v, con, ref)
    validate(src.read_bytes())

    if kind == "item":  # the item may be a duplicate of a paper that already has a page: use that page
        from .ingest import find_page
        row = con.execute("SELECT data FROM items WHERE key=?", (ident,)).fetchone()
        imeta = zotero_meta(con, json.loads(row["data"]))
        epath, efm = find_page(v, imeta["doi"], imeta["title"])
        if epath and dedupe.same_paper(efm, imeta):
            dedupe.attach_zotero_item(epath, ident, imeta)
            con.execute("UPDATE items SET page=?, status='done', note=? WHERE key=?",
                        (f"{vault.PAPERS}/{epath.name}", f"duplicate of {epath.stem}", ident))
            con.commit()
            kind, ident = "page", epath.stem

    old_fm = path = page_rel = None
    if kind == "page":
        path = v / vault.PAPERS / f"{ident}.md"
        page_rel = f"{vault.PAPERS}/{ident}.md"
        old_fm, _ = read_page(path)
        if old_fm.get("status") not in (None, "auto") and not force:
            raise AttachError(f"{ident} was edited by you (status: {old_fm.get('status')}); re-summarising would "
                              "overwrite your edits. Use --force (or the checkbox) to do it anyway.")
        key = str(old_fm.get("key") or old_fm.get("zotero_key") or ident)
        meta = {"title": old_fm.get("title") or ident, "authors": old_fm.get("authors") or [], "year": old_fm.get("year"),
                "venue": old_fm.get("venue", ""), "doi": old_fm.get("doi", ""), "url": old_fm.get("url", ""),
                "abstract": "", "collections": old_fm.get("collections") or [], "zotero_tags": old_fm.get("zotero_tags") or []}
        source = old_fm.get("source") or "zotero"
    else:
        row = con.execute("SELECT data FROM items WHERE key=?", (ident,)).fetchone()
        meta = zotero_meta(con, json.loads(row["data"]))
        key, source = ident, "zotero"

    rel = pdf_rel_for(key)
    dest, backup = v / rel, None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        backup = dest.with_suffix(".pdf.bak")
        os.replace(dest, backup)
    tmp = dest.with_suffix(".pdf.part")
    tmp.write_bytes(src.read_bytes())
    os.replace(tmp, dest)
    try:
        pages = textract.pdf_pages(dest)
        new_rel = summarise_and_write(cfg, key=key, meta=meta, pages=pages, unit="page", pdf_rel=rel, source=source,
                                      purpose="summary:attach", page_rel=page_rel)
    except BaseException:
        os.replace(backup, dest) if backup else dest.unlink(missing_ok=True)  # leave things as they were
        raise
    if backup:
        backup.unlink(missing_ok=True)

    path = v / new_rel
    nfm, nbody = read_page(path)
    if old_fm is not None:
        merged = dict(nfm)
        for k, val in old_fm.items():
            if k not in REGENERATED:
                merged[k] = val
        merged["concepts"] = list(dict.fromkeys([*(old_fm.get("concepts") or []), *(nfm.get("concepts") or [])]))
        merged["attached_pdf_on"] = date.today().isoformat()
        nfm = merged
    apply_tags(nfm)
    write_page(path, nfm, nbody)

    con.execute("UPDATE items SET page=?, status='done', note=NULL WHERE key=? AND page IS NULL", (new_rel, key))
    con.execute("UPDATE items SET note=NULL WHERE page=? AND note LIKE 'needs_pdf%'", (new_rel,))
    con.commit()
    log.info("attached %s to %s (%s)", rel, new_rel, nfm.get("claims_verified"))
    return {"page": new_rel, "created": kind == "item", "claims_verified": nfm.get("claims_verified"),
            "fulltext": nfm.get("fulltext")}


def spawn(ref: str, pdf: Path, force: bool = False) -> None:
    """Run `litwiki attach-pdf` detached (web UI): it waits for the pipeline lock, re-summarises, rebuilds the site."""
    cmd = [sys.executable, "-m", "litwiki.cli", "attach-pdf", ref, str(pdf), "--move"] + (["--force"] if force else [])
    out = open(LOG_DIR / "web-jobs.log", "a")
    p = subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True, cwd=ROOT)
    threading.Thread(target=p.wait, daemon=True).start()
    out.close()
