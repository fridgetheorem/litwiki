"""Paper map: a 2-D layout of every paper, written as a static page (clusters/index.html) into the site build.

Two layouts, switchable in the page:
  tldr     TF-IDF over the chosen page sections (default: the TL;DR) -> SVD -> t-SNE
  concept  concept membership (binary) -> SVD -> t-SNE
Points are coloured by each paper's primary (first-listed) concept. Layouts are cached in state/ and only
recomputed when a paper's features change; new layouts are rotated/scaled (Procrustes) onto the previous
one so the map doesn't flip around every time a paper is added."""
import hashlib
import json
import re
from pathlib import Path

import numpy as np

from . import vault
from .common import STATE_DIR, log, read_page

CACHE = STATE_DIR / "clusters.json"
TOP_SIMILAR, MIN_SIMILARITY = 8, 0.08
SECTION = re.compile(r"^## (.+?)\s*$", re.M)


def section_text(body: str, names: list[str]) -> str:
    """Text of the named '## Heading' sections of a paper page, concatenated."""
    marks = [(m.group(1), m.start(), m.end()) for m in SECTION.finditer(body)]
    out = []
    for i, (name, _, end) in enumerate(marks):
        if name in names:
            out.append(body[end:marks[i + 1][1] if i + 1 < len(marks) else len(body)])
    text = " ".join(out)
    text = re.sub(r"\[\[[^\]|]*\|?([^\]]*)\]\]", r"\1", text)      # wikilinks -> label
    text = re.sub(r"<!--.*?-->|[>*_`#]|\[[^\]]*\]\([^)]*\)", " ", text, flags=re.S)
    return " ".join(text.split())


def load_papers(cfg) -> list[dict]:
    names = (cfg.get("clusters") or {}).get("sections") or ["TL;DR"]
    papers = []
    for p in sorted((cfg["vault"] / vault.PAPERS).glob("*.md")):
        fm, body = read_page(p)
        if not fm.get("title"):
            continue
        cs = [c for c in (fm.get("concepts") or []) if isinstance(c, str)]
        papers.append({
            "id": p.stem, "title": str(fm["title"]), "year": fm.get("year"), "concepts": cs,
            "authors": ", ".join(map(str, (fm.get("authors") or [])[:3])) + (" et al." if len(fm.get("authors") or []) > 3 else ""),
            "authors_list": [str(a) for a in (fm.get("authors") or []) if not str(a).strip().isdigit()],
            "text": section_text(body, names),
        })
    return papers


def _layout(X, seed: int = 0) -> np.ndarray:
    """Dense/sparse feature matrix -> (n, 2) t-SNE coordinates."""
    from sklearn.decomposition import TruncatedSVD
    from sklearn.manifold import TSNE
    n = X.shape[0]
    k = max(2, min(50, n - 1, X.shape[1] - 1))
    Z = TruncatedSVD(n_components=k, random_state=seed).fit_transform(X)
    perplexity = max(2.0, min(30.0, (n - 1) / 3))
    Y = TSNE(n_components=2, perplexity=perplexity, init="pca", random_state=seed,
             learning_rate="auto", metric="euclidean").fit_transform(Z)
    # Identical feature vectors (e.g. same concept set) would sit exactly on top of each other.
    Y += np.random.default_rng(seed).normal(scale=0.02 * (Y.std() or 1), size=Y.shape)
    return Y


def _normalise(Y: np.ndarray) -> np.ndarray:
    Y = Y - Y.mean(axis=0)
    return Y / (np.abs(Y).max() or 1)


def _align(Y: np.ndarray, ids: list[str], prev: dict) -> np.ndarray:
    """Rotate/reflect/scale Y onto the previous layout of the papers both have (keeps the map stable)."""
    from scipy.linalg import orthogonal_procrustes
    common = [i for i, pid in enumerate(ids) if pid in prev]
    if len(common) < 10:
        return Y
    A = Y[common]
    B = np.array([prev[ids[i]] for i in common])
    ca, cb = A.mean(axis=0), B.mean(axis=0)
    R, _ = orthogonal_procrustes(A - ca, B - cb)
    scale = np.linalg.norm(B - cb) / (np.linalg.norm(A - ca) or 1)
    return (Y - ca) @ R * scale + cb


def compute_layouts(cfg, papers: list[dict]) -> tuple[dict[str, dict[str, list[float]]], dict[str, list]]:
    """({'tldr': {id: [x, y]}, 'concept': {...}}, {id: [[similar id, cosine], ...]}); cached while features are unchanged."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    key = hashlib.sha1(json.dumps([cfg.get("clusters"), [(p["id"], p["text"], p["concepts"]) for p in papers]],
                                  sort_keys=True).encode()).hexdigest()
    prev = {}
    if CACHE.exists():
        cached = json.loads(CACHE.read_text())
        if cached.get("key") == key and "similar" in cached:
            return cached["layouts"], cached["similar"]
        prev = cached.get("layouts", {})
    ids = [p["id"] for p in papers]
    min_df = int((cfg.get("clusters") or {}).get("min_df", 2))

    for df in (min_df, 1):  # tiny libraries may have no term shared by two papers
        try:
            tfidf = TfidfVectorizer(stop_words="english", sublinear_tf=True, ngram_range=(1, 2),
                                    min_df=df, max_df=0.5).fit_transform([p["text"] for p in papers])
            if tfidf.shape[1] > 2:
                break
        except ValueError:
            continue
    slugs = sorted({c for p in papers for c in p["concepts"]})
    member = np.zeros((len(papers), max(len(slugs), 1)))
    for i, p in enumerate(papers):
        for c in p["concepts"]:
            member[i, slugs.index(c)] = 1.0
    # Down-weight very common concepts (IDF) so the layout isn't dominated by one big topic.
    df_c = member.sum(axis=0)
    member = member * np.log((1 + len(papers)) / (1 + df_c))

    layouts = {}
    for name, X in (("tldr", tfidf), ("concept", member)):
        Y = _normalise(_layout(X))
        Y = _normalise(_align(Y, ids, prev.get(name, {})))
        layouts[name] = {pid: [round(float(x), 4), round(float(y), 4)] for pid, (x, y) in zip(ids, Y)}
    # Nearest papers by TL;DR text (TF-IDF rows are L2-normalised, so the dot product is the cosine).
    S = (tfidf @ tfidf.T).toarray()
    np.fill_diagonal(S, 0)
    similar = {}
    for i, pid in enumerate(ids):
        top = [j for j in np.argsort(-S[i])[:TOP_SIMILAR] if S[i, j] >= MIN_SIMILARITY]
        similar[pid] = [[ids[j], round(float(S[i, j]), 3)] for j in top]
    STATE_DIR.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({"key": key, "layouts": layouts, "similar": similar}))
    return layouts, similar


PALETTE = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1", "#ff9da7",
           "#9c755f", "#17becf", "#8c6bb1", "#d4a017"]


def _short(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return (cut or text[:n]).rstrip(",:;-") + "…"


def build_data(cfg) -> dict | None:
    from . import citations
    papers = load_papers(cfg)
    if len(papers) < 5:
        return None
    layouts, similar = compute_layouts(cfg, papers)
    try:
        cg = citations.graph(cfg)
    except Exception:
        log.exception("citation graph unavailable; map built without it")
        cg = {}
    idx = {p["id"]: i for i, p in enumerate(papers)}
    titles = {s: fm["title"] for s, fm in vault.concepts(cfg["vault"]).items()}
    counts: dict[str, int] = {}
    for p in papers:
        if p["concepts"]:
            counts[p["concepts"][0]] = counts.get(p["concepts"][0], 0) + 1
    top = [c for c, _ in sorted(counts.items(), key=lambda x: -x[1])[:len(PALETTE)]]
    out = []
    for p in papers:
        c = cg.get(p["id"], {})
        last = p["authors_list"][0].split()[-1] if p["authors_list"] else ""
        short_title = _short(p["title"], 60)
        out.append({
            "id": p["id"], "t": p["title"], "a": p["authors"], "y": p["year"], "c": p["concepts"],
            "p": p["concepts"][0] if p["concepts"] else "", "s": p["text"][:420],
            "lab": f"{last}{' et al.' if len(p['authors_list']) > 1 else ''} {p['year'] or ''}".strip() or _short(p["title"], 24),
            "st": short_title,
            "cb": c.get("cb"), "cd": 1 if c else 0,
            "cites": [idx[t] for t in c.get("cites", [])], "citedby": [idx[t] for t in c.get("citedby", [])],
            "coup": [[idx[t], n] for t, n, _ in c.get("coup", [])],
            "sim": [[idx[t], sc] for t, sc in similar.get(p["id"], [])],
            "xy": {name: layouts[name][p["id"]] for name in layouts},
        })
    return {
        "papers": out,
        "concepts": {s: titles.get(s, s) for s in sorted({c for p in papers for c in p["concepts"]})},
        "top": top, "colors": dict(zip(top, PALETTE)),
        "layouts": {"tldr": "TL;DR text similarity", "concept": "Concept membership"},
    }


def write_page(cfg, out: Path, data: dict | None = None) -> bool:
    data = data or build_data(cfg)
    if data is None:
        return False
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = out / "clusters" / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(Path(__file__).with_name("map_page.html").read_text(encoding="utf-8").replace("__DATA__", blob), encoding="utf-8")
    log.info("cluster map: %d papers", len(data["papers"]))
    return True
