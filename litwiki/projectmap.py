"""The live map of one project's papers (/app/project/<slug>/map): the same page as the site-wide paper map, restricted to the
project's papers and coloured by their project tags. A paper's first tag is its primary tag (its dot colour); every tag also
gets a translucent region around all papers that carry it, so a paper with several tags sits where their regions overlap.

Layouts, switchable on the page:
  tldr     TF-IDF over the paper pages' chosen sections -> SVD -> t-SNE (the project's papers only)
  concept  concept membership -> SVD -> t-SNE (only offered when some paper has concepts)
  tags     tag membership, the primary tag counting double -> SVD -> t-SNE
Nothing is stored: tags change often, so the data is built on each request (cached in memory while nothing changed)."""
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from . import citations, cluster, project, vault
from .common import log, read_page

MIN_PAPERS = 3
MAX_TAG_COLOURS = len(cluster.PALETTE)
_CACHE: dict[str, dict] = {}


def _ring(n: int) -> np.ndarray:
    """Fallback layout (too few papers or features for t-SNE): papers evenly on a circle."""
    a = np.arange(n) * 2 * math.pi / max(n, 1)
    return np.c_[np.cos(a), np.sin(a)] * 0.8


def _lay(X, n: int, name: str) -> np.ndarray:
    try:
        return cluster._normalise(cluster._layout(X))
    except Exception as e:      # e.g. one feature only, or identical papers
        log.info("project map: %s layout fell back to a ring (%s)", name, e)
        return _ring(n)


def _features(cfg, rows: list[dict]):
    from sklearn.feature_extraction.text import TfidfVectorizer
    names = (cfg.get("clusters") or {}).get("sections") or ["TL;DR"]
    papers = []
    for r in rows:
        fm, body = read_page(cfg["vault"] / vault.PAPERS / f"{r['stem']}.md")
        papers.append({**r, "fm": fm, "text": cluster.section_text(body, names),
                       "concepts": [c for c in (fm.get("concepts") or []) if isinstance(c, str)]})
    tfidf = None
    for df in (int((cfg.get("clusters") or {}).get("min_df", 2)), 1):
        try:
            m = TfidfVectorizer(stop_words="english", sublinear_tf=True, ngram_range=(1, 2), min_df=df, max_df=0.5
                                ).fit_transform([p["text"] for p in papers])
            if m.shape[1] > 2:
                tfidf = m
                break
        except ValueError:
            continue
    return papers, tfidf


def _membership(papers: list[dict], key, weight_first: float = 1.0) -> np.ndarray:
    names = sorted({x for p in papers for x in key(p)})
    M = np.zeros((len(papers), max(len(names), 1)))
    for i, p in enumerate(papers):
        for j, x in enumerate(key(p)):
            M[i, names.index(x)] = weight_first if j == 0 else 1.0
    M *= np.log((1 + len(papers)) / (1 + (M > 0).sum(axis=0)))      # common tags say less about where a paper belongs
    return M, names


def build(cfg, slug: str) -> dict:
    rows = project.paper_list(cfg, slug)
    if len(rows) < MIN_PAPERS:
        raise project.ProjectError(f"The map needs at least {MIN_PAPERS} papers in the project (it has {len(rows)}).")
    rv = project.get(cfg, slug)
    papers, tfidf = _features(cfg, rows)
    key = hashlib.sha1(json.dumps([(p["stem"], p["text"], p["concepts"], p["tags"]) for p in papers]).encode()).hexdigest()
    if key not in _CACHE:
        if len(_CACHE) > 8:
            _CACHE.clear()
        n = len(papers)
        lay, similar = {}, {}
        lay["tldr"] = _lay(tfidf, n, "tldr") if tfidf is not None else _ring(n)
        if tfidf is not None:
            S = (tfidf @ tfidf.T).toarray()
            np.fill_diagonal(S, 0)
            for i, p in enumerate(papers):
                top = [j for j in np.argsort(-S[i])[:cluster.TOP_SIMILAR] if S[i, j] >= cluster.MIN_SIMILARITY]
                similar[p["stem"]] = [[int(j), round(float(S[i, j]), 3)] for j in top]
        if any(p["concepts"] for p in papers):
            lay["concept"] = _lay(_membership(papers, lambda p: p["concepts"])[0], n, "concept")
        M, tag_names = _membership(papers, lambda p: p["tags"], weight_first=2.0)
        lay["tags"] = _lay(M, n, "tags") if len(tag_names) >= 2 else _ring(n)
        _CACHE[key] = {"lay": lay, "similar": similar}
    cached = _CACHE[key]

    try:
        cg = citations.graph(cfg)
    except Exception:
        log.exception("citation graph unavailable; project map built without it")
        cg = {}
    idx = {p["stem"]: i for i, p in enumerate(papers)}
    use: dict[str, int] = {}
    first: dict[str, int] = {}
    for p in papers:
        for j, t in enumerate(p["tags"]):
            use[t] = use.get(t, 0) + 1
            if j == 0:
                first[t] = first.get(t, 0) + 1
    top = sorted(use, key=lambda t: (-use[t], -first.get(t, 0), t))[:MAX_TAG_COLOURS]
    out = []
    for i, p in enumerate(papers):
        c = cg.get(p["stem"], {})
        au = [str(a) for a in (p["fm"].get("authors") or []) if not str(a).strip().isdigit()]
        last = au[0].split()[-1] if au else ""
        out.append({
            "id": p["stem"], "t": p["title"], "a": p["authors"], "y": int(p["year"]) if str(p["year"]).isdigit() else None,
            "c": p["tags"], "p": p["tags"][0] if p["tags"] else "", "s": p["text"][:420],
            "lab": f"{last}{' et al.' if len(au) > 1 else ''} {p['year'] or ''}".strip() or cluster._short(p["title"], 24),
            "st": cluster._short(p["title"], 60),
            "cb": c.get("cb"), "cd": 1 if c else 0,
            "cites": [idx[t] for t in c.get("cites", []) if t in idx], "citedby": [idx[t] for t in c.get("citedby", []) if t in idx],
            "coup": [[idx[t], n] for t, n, _ in c.get("coup", []) if t in idx],
            "sim": cached["similar"].get(p["stem"], []),
            "xy": {name: [round(float(Y[i][0]), 4), round(float(Y[i][1]), 4)] for name, Y in cached["lay"].items()},
        })
    names = {"tldr": "TL;DR text similarity", "concept": "Concept membership", "tags": "Tag membership"}
    return {
        "papers": out, "concepts": {t: t for t in use}, "top": top, "colors": dict(zip(top, cluster.PALETTE)),
        "layouts": {k: names[k] for k in cached["lay"]}, "default_layout": "tags" if len(use) >= 2 else "tldr",
        "project": {"title": rv["title"], "back": f"/app/project/{slug}/papers"},
        "filterLabel": "Tag", "otherLabel": "Untagged / other tags", "regionOf": "all",
        "help": "Dot colour = the paper's primary (first) tag; each shaded region surrounds every paper with that tag, so papers with several tags sit where regions overlap.",
    }


def page(cfg, slug: str) -> str:
    data = build(cfg, slug)
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return Path(cluster.__file__).with_name("map_page.html").read_text(encoding="utf-8").replace("__DATA__", blob)
