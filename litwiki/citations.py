"""Citation links between library papers, from OpenAlex (no PDF parsing).

`refresh` stores each paper's OpenAlex id, reference list and citation count (papers need a DOI or an arXiv id;
web pages have no citation data). `graph` turns that into, per paper: papers in the library it cites, papers
that cite it, and papers that share references with it (bibliographic coupling, the signal Connected Papers
leans on when direct citations are sparse)."""
import json
import math
import re
import time
from collections import Counter, defaultdict

import requests

from . import vault
from .common import STATE_DIR, db, log, read_page

UA = {"User-Agent": "litwiki/0.1 (personal literature wiki)"}
RETRY_MISSING_DAYS, REFRESH_DAYS = 7, 30
MIN_SHARED = 2          # shared references needed to count as "coupled"
TOP_COUPLED = 8


def paper_doi(fm: dict) -> str:
    """DOI to look up: the page's DOI, or the arXiv-issued DOI for arXiv papers."""
    if fm.get("doi"):
        return str(fm["doi"]).lower().strip()
    for cand in (str(fm.get("key", "")), str(fm.get("url", ""))):
        m = re.search(r"arxiv(?:\.org/(?:abs|pdf)/|:)(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})", cand, re.I)
        if m:
            return f"10.48550/arxiv.{m.group(1).lower()}"
    return ""


def refresh(cfg, force: bool = False) -> dict:
    """Fetch citation data for papers that have none (or stale data). Cheap: 50 papers per request."""
    con = db()
    mailto = (cfg.get("discovery") or {}).get("openalex_mailto", "")
    known = {r["page"]: r for r in con.execute("SELECT page, found, fetched, "
                                               "(julianday('now') - julianday(fetched)) AS age FROM cite_meta")}
    todo: dict[str, str] = {}      # doi -> page
    for p in sorted((cfg["vault"] / vault.PAPERS).glob("*.md")):
        fm, _ = read_page(p)
        page = f"{vault.PAPERS}/{p.name}"
        doi = paper_doi(fm)
        row = known.get(page)
        if not doi:
            if not row:
                con.execute("INSERT OR REPLACE INTO cite_meta(page, found) VALUES(?, 0)", (page,))
            continue
        stale = row and (row["age"] > (RETRY_MISSING_DAYS if not row["found"] else REFRESH_DAYS))
        if force or not row or stale:
            todo[doi] = page
    con.commit()
    found = missing = 0
    dois = list(todo)
    for i in range(0, len(dois), 50):
        chunk = dois[i:i + 50]
        params = {"filter": "doi:" + "|".join(chunk), "per-page": 50,
                  "select": "id,doi,referenced_works,cited_by_count"}
        if mailto:
            params["mailto"] = mailto
        try:
            r = requests.get("https://api.openalex.org/works", params=params, headers=UA, timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("citations: OpenAlex batch failed (%s); will retry next run", e)
            continue
        got = {(w.get("doi") or "").replace("https://doi.org/", "").lower(): w for w in r.json().get("results", [])}
        for doi in chunk:
            w = got.get(doi)
            if w:
                con.execute("INSERT OR REPLACE INTO cite_meta(page, oa_id, refs, cited_by, found) VALUES(?,?,?,?,1)",
                            (todo[doi], w["id"], json.dumps(w.get("referenced_works") or []), w.get("cited_by_count") or 0))
                found += 1
            else:
                con.execute("INSERT OR REPLACE INTO cite_meta(page, found) VALUES(?, 0)", (todo[doi],))
                missing += 1
        con.commit()
        time.sleep(0.2)
    if found or missing:
        log.info("citations: %d fetched, %d not in OpenAlex", found, missing)
    return {"fetched": found, "missing": missing}


def graph(cfg) -> dict[str, dict]:
    """{page stem: {cb, cd, cites: [stems], citedby: [stems], coup: [(stem, shared, score)]}} for papers with data."""
    con = db()
    rows = con.execute("SELECT page, oa_id, refs, cited_by FROM cite_meta WHERE found=1").fetchall()
    stem = lambda page: page.rsplit("/", 1)[-1].removesuffix(".md")
    by_oa = {r["oa_id"]: stem(r["page"]) for r in rows}
    refs = {stem(r["page"]): set(json.loads(r["refs"] or "[]")) for r in rows}
    out = {stem(r["page"]): {"cb": r["cited_by"], "cd": 1, "cites": [], "citedby": [], "coup": []} for r in rows}
    for s, rs in refs.items():
        for ref in rs:
            t = by_oa.get(ref)
            if t and t != s:
                out[s]["cites"].append(t)
                out[t]["citedby"].append(s)
    holders: dict[str, list[str]] = defaultdict(list)     # reference -> library papers citing it
    for s, rs in refs.items():
        for ref in rs:
            holders[ref].append(s)
    shared: Counter = Counter()
    for ref, ss in holders.items():
        if 1 < len(ss) <= 60:                              # skip works everything cites
            for i in range(len(ss)):
                for j in range(i + 1, len(ss)):
                    shared[(ss[i], ss[j])] += 1
    pairs: dict[str, list] = defaultdict(list)
    for (a, b), n in shared.items():
        if n >= MIN_SHARED:
            score = n / math.sqrt(len(refs[a]) * len(refs[b]))
            pairs[a].append((b, n, score))
            pairs[b].append((a, n, score))
    for s, lst in pairs.items():
        out[s]["coup"] = sorted(lst, key=lambda x: (-x[2], -x[1]))[:TOP_COUPLED]
    return out


# ---------- per-paper "Citations & related papers" section (rendered into the site, not the vault) ----------

RELATED = STATE_DIR / "related.json"


def related_from_map(data: dict) -> dict:
    """Per-paper neighbours (as page stems) from the paper-map data, for the site's paper pages."""
    ps = data["papers"]
    stem = lambda i: ps[i]["id"]
    return {
        "papers": {p["id"]: {"cb": p["cb"], "cd": p["cd"], "cites": [stem(i) for i in p["cites"]],
                             "citedby": [stem(i) for i in p["citedby"]],
                             "coup": [[stem(i), n] for i, n in p["coup"]], "sim": [[stem(i), sc] for i, sc in p["sim"]]}
                   for p in ps},
        "meta": {p["id"]: [p["t"], p["y"]] for p in ps},
    }


def load_related() -> dict:
    try:
        return json.loads(RELATED.read_text())
    except (OSError, ValueError):
        return {}


def _link(stem: str, meta: dict) -> str:
    title, year = meta.get(stem, [stem, None])
    title = re.sub(r"([\[\]])", r"\\\1", str(title))
    return f"[{title}]({stem}.md)" + (f" ({year})" if year else "")


def section_markdown(stem: str, related: dict) -> str:
    """Markdown for the citations block of one paper page ('' when there is nothing to show)."""
    r = (related.get("papers") or {}).get(stem)
    if not r:
        return ""
    meta = related.get("meta") or {}
    parts = []
    head = []
    if r.get("cd") and r.get("cb") is not None:
        head.append(f"Cited by **{r['cb']:,}** works (OpenAlex)")
    elif not r.get("cd"):
        head.append("_No citation data: OpenAlex needs a DOI or arXiv id for this paper._")
    head.append(f"[Show on map](/clusters/#{stem})")
    parts.append(" · ".join(head))
    for title, items, fmt in (
        ("Cites (in this library)", r.get("cites") or [], lambda s: _link(s, meta)),
        ("Cited by (in this library)", r.get("citedby") or [], lambda s: _link(s, meta)),
        ("Shared references", r.get("coup") or [], lambda x: f"{_link(x[0], meta)} — {x[1]} shared references"),
        ("Similar text", r.get("sim") or [], lambda x: f"{_link(x[0], meta)} — {round(x[1] * 100)}% similar"),
    ):
        if items:
            parts.append(f"### {title}\n\n" + "\n".join(f"- {fmt(i)}" for i in items))
    if len(parts) == 1 and not r.get("cd"):
        return ""  # nothing but "no data": don't add an empty section
    return "## Citations & related papers\n\n" + "\n\n".join(parts) + "\n"
