"""Manually added items (from the web UI): DOI / arXiv ID / URL / uploaded PDF -> a queued candidate.
Candidates added here have digest='web' and status 'queued'; `ingest_manual` (hourly, or right away
from the web UI) turns them into paper pages through the same path as ticked digest items."""
import hashlib
import ipaddress
import json
import re
import socket
import time
from datetime import date
from urllib.parse import urljoin, urlparse

import requests

from . import textract, vault
from .common import db, log
from .discover import UA, ingest_candidate, openalex_work, parse_arxiv
from .ingest import NoContent, find_page
from .llm import BudgetExceeded

MAX_UPLOAD = 60 * 1024 * 1024
ARXIV_ID = re.compile(r"(?:arxiv[:.\s]*|arxiv\.org/(?:abs|pdf)/)?"
                      r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?\b", re.I)
DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)


class AddError(Exception):
    """Shown to the user as-is."""


# ---------- safe fetching of user-supplied URLs ----------

def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified or ip in ipaddress.ip_network("100.64.0.0/10")):  # CGNAT = tailnet
            return False
    return bool(infos)


def fetch_public(url: str, timeout: int = 60, max_redirects: int = 5) -> requests.Response:
    """GET a URL, refusing non-http(s) schemes and private/loopback/tailnet hosts at every redirect hop."""
    for _ in range(max_redirects + 1):
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname or not _public(u.hostname):
            raise AddError(f"refusing to fetch {url!r} (not a public http(s) address)")
        r = requests.get(url, timeout=timeout, headers=UA, allow_redirects=False)
        if r.is_redirect and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            continue
        return r
    raise AddError("too many redirects")


# ---------- resolving ----------

def classify(text: str) -> tuple[str, str]:
    t = text.strip()
    if m := DOI.search(t):
        return "doi", m.group(1).rstrip(".,;)").lower()
    if m := ARXIV_ID.search(t):
        if "arxiv" in t.lower() or re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", t):
            return "arxiv", m.group(1)
    if re.match(r"https?://", t):
        return "url", t
    raise AddError("Couldn't recognise that as a DOI, arXiv ID or http(s) URL.")


def _resolve_arxiv(aid: str) -> dict:
    for attempt in range(3):  # arXiv rate-limits (429) aggressively
        r = requests.get("https://export.arxiv.org/api/query", params={"id_list": aid}, headers=UA, timeout=30)
        if r.status_code != 429:
            break
        time.sleep(4 * (attempt + 1))
    r.raise_for_status()
    found = [c for c in parse_arxiv(r.content) if c["title"] and c["title"] != "Error"]
    if not found:
        raise AddError(f"arXiv has no paper {aid!r}.")
    return found[0]


def _resolve_doi(doi: str) -> dict:
    r = requests.get(f"https://api.openalex.org/works/https://doi.org/{doi}", headers=UA, timeout=30)
    if r.status_code == 404:
        raise AddError(f"OpenAlex doesn't know DOI {doi!r}; try the paper's URL, or upload the PDF.")
    r.raise_for_status()
    c = openalex_work(r.json())
    c["id"], c["doi"] = f"doi:{doi}", doi
    return c


def _resolve_url(url: str) -> dict:
    r = fetch_public(url, timeout=30)
    r.raise_for_status()
    if r.content[:5] == b"%PDF-":
        name = urlparse(url).path.rsplit("/", 1)[-1].removesuffix(".pdf") or "document"
        return {"id": f"url:{url}", "title": name.replace("_", " ").replace("-", " "), "authors": [],
                "abstract": "", "date": date.today().isoformat(), "year": None, "venue": urlparse(url).hostname,
                "url": url, "pdf_url": url, "doi": "", "kind": "preprint", "title_from_doc": True}
    html = r.text[:200_000]
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = " ".join(re.sub(r"<[^>]+>", "", m.group(1)).split()) if m else ""
    return {"id": f"url:{url}", "title": title or url, "authors": [], "abstract": "",
            "date": date.today().isoformat(), "year": date.today().year, "venue": urlparse(url).hostname,
            "url": url, "pdf_url": "", "doi": "", "kind": "blog", "title_from_doc": not title}


def resolve(text: str) -> dict:
    kind, val = classify(text)
    try:
        if kind == "arxiv":
            return _resolve_arxiv(val)
        if kind == "doi":
            return _resolve_doi(val)
        return _resolve_url(val)
    except requests.RequestException as e:
        raise AddError(f"Lookup failed: {e}")


# ---------- queueing ----------

def _validate_choices(cfg, review: str, concept: str) -> None:
    if review and review not in vault.reviews(cfg["vault"]):
        raise AddError(f"Unknown review {review!r}.")
    if concept and concept not in vault.concepts(cfg["vault"]):
        raise AddError(f"Unknown concept {concept!r}.")


def _queue(cfg, c: dict, review: str, concept: str) -> str:
    """Insert/refresh the candidate as queued. Returns a status message for the user."""
    con = db()
    v = cfg["vault"]
    path, _ = find_page(v, c.get("doi", ""), c["title"] if not c.get("title_from_doc") else "")
    if path:
        raise AddError(f"Already in the wiki: papers/{path.name}")
    c.update(manual=True, review=review or None, concept=concept or None)
    row = con.execute("SELECT status FROM candidates WHERE id=?", (c["id"],)).fetchone()
    if row and row["status"] == "ingested":
        raise AddError("Already ingested.")
    if row and row["status"] == "queued":
        raise AddError("Already in the queue.")
    con.execute("INSERT INTO candidates(id, title, data, score, digest, status) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, data=excluded.data, "
                "digest='web', status='queued'",
                (c["id"], c["title"], json.dumps(c), 10, "web", "queued"))
    con.commit()
    return c["title"]


def add_text(cfg, text: str, review: str = "", concept: str = "") -> str:
    _validate_choices(cfg, review, concept)
    return _queue(cfg, resolve(text), review, concept)


def add_pdf(cfg, filename: str, data: bytes, review: str = "", concept: str = "") -> str:
    _validate_choices(cfg, review, concept)
    if len(data) > MAX_UPLOAD:
        raise AddError("That file is over 60 MB.")
    if data[:5] != b"%PDF-":
        raise AddError("Only PDF uploads are supported.")
    h = hashlib.sha256(data).hexdigest()[:16]
    rel = f"{vault.PDFS}/upload-{h}.pdf"
    dest = cfg["vault"] / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(data)
    try:
        pages = textract.pdf_pages(dest)
    except Exception as e:
        raise AddError(f"Couldn't read that PDF: {e}")
    if sum(len(p) for p in pages) < 1500:
        raise AddError("That PDF has no extractable text (scanned?). Run OCR on it first.")
    stem = re.sub(r"\.pdf$", "", filename or "upload", flags=re.I)
    c = {"id": f"file:{h}", "title": stem.replace("_", " ").replace("-", " ").strip() or "Uploaded PDF",
         "authors": [], "abstract": "", "date": date.today().isoformat(), "year": None, "venue": "",
         "url": "", "pdf_url": "", "doi": "", "kind": "preprint", "local_file": rel, "title_from_doc": True}
    return _queue(cfg, c, review, concept)


def retry(cfg, cid: str) -> None:
    con = db()
    con.execute("UPDATE candidates SET status='queued' WHERE id=? AND digest='web' AND status IN ('failed','rejected')",
                (cid,))
    con.commit()


def ingest_manual(cfg) -> dict:
    con = db()
    done = failed = 0
    for row in con.execute("SELECT id, data FROM candidates WHERE digest='web' AND status='queued' "
                           "ORDER BY seen").fetchall():
        cid, c = row["id"], json.loads(row["data"])
        try:
            page = ingest_candidate(cfg, cid, c)
            c.update(page=page, error=None)
            con.execute("UPDATE candidates SET status='ingested', data=? WHERE id=?", (json.dumps(c), cid))
            done += 1
        except BudgetExceeded:
            break
        except (NoContent, AddError) as e:
            c["error"] = str(e)
            con.execute("UPDATE candidates SET status='rejected', data=? WHERE id=?", (json.dumps(c), cid))
            log.warning("manual %s not ingested: %s", cid, e)
        except Exception as e:
            log.exception("manual ingest failed for %s", cid)
            c["error"] = str(e)[:300]
            con.execute("UPDATE candidates SET status='failed', data=? WHERE id=?", (json.dumps(c), cid))
            failed += 1
        con.commit()
    return {"done": done, "failed": failed}
