"""Concept pages managed from the web app: what Obsidian was needed for (editing a concept's search terms, whether it
is searched daily, and the Overview text you write). Slugs are fixed once created, since paper pages link to them."""
import hashlib
import json
import re

from . import vault
from .common import read_page, write_page
from .notes import Conflict
from .project import ProjectError, clean_terms

PLACEHOLDER = "_Your synthesis goes here. litwiki only edits the auto-managed list below._"
ARXIV_CAT = re.compile(r"^[A-Za-z][A-Za-z\-]*(\.[A-Za-z\-]+)?$")   # cs.CR, math.ST, cond-mat.mes-hall, hep-th


def get(cfg, slug: str) -> dict:
    fm = vault.concepts(cfg["vault"]).get(slug)
    if not fm:
        raise KeyError(slug)
    return fm


def clean_categories(raw) -> list[str]:
    items = re.split(r"[\s,;]+", raw.strip()) if isinstance(raw, str) else list(raw or [])
    out = []
    for c in items:
        c = str(c).strip()
        if not c:
            continue
        if not ARXIV_CAT.match(c):
            raise ProjectError(f"'{c}' is not an arXiv category (examples: cs.CR, cs.SE, stat.ML).")
        if c not in out:
            out.append(c)
    return out[:10]


def summary(cfg) -> list[dict]:
    return [{"slug": s, "title": fm["title"], "description": str(fm.get("description") or ""),
             "terms": len(fm.get("search_terms") or []), "discover": fm.get("discover", True) is not False,
             "papers": int(fm.get("paper_count") or 0)} for s, fm in vault.concepts(cfg["vault"]).items()]


def _version(f: dict) -> str:
    key = [f["title"], f["description"], f["aliases"], f["terms"], f["categories"], f["discover"], f["overview"]]
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:16]


def read(cfg, slug: str) -> dict:
    fm = get(cfg, slug)
    _, body = read_page(fm["path"])
    f = {"slug": slug, "title": fm["title"], "description": str(fm.get("description") or ""),
         "aliases": [str(a) for a in (fm.get("aliases") or [])], "terms": list(fm.get("search_terms") or []),
         "categories": list(fm.get("arxiv_categories") or []), "discover": fm.get("discover", True) is not False,
         "overview": vault.get_section(body, "Overview", PLACEHOLDER)}
    f["version"] = _version(f)
    return f


def create(cfg, title: str, description: str = "", terms=(), categories="") -> str:
    from .common import slugify
    title = " ".join(title.split())
    if not title:
        raise ProjectError("Give the concept a title.")
    if len(title) > 100:
        raise ProjectError("Keep the title under 100 characters.")
    slug = slugify(title, 50)
    if slug in vault.concepts(cfg["vault"]):
        raise ProjectError(f"A concept called '{slug}' already exists.")
    return vault.create_concept(cfg["vault"], title, " ".join(description.split())[:500], clean_terms(terms),
                                clean_categories(categories), origin="web")


def save(cfg, slug: str, title: str, description: str, aliases, terms, categories, discover: bool,
         overview: str, expected_version: str) -> str:
    """Save the concept form (frontmatter + the Overview section; the auto-managed paper list is untouched).
    Raises notes.Conflict if those fields changed since the form was loaded. Returns the new version."""
    title = " ".join(title.split())
    if not title:
        raise ProjectError("A concept needs a title.")
    cats = clean_categories(categories)
    if read(cfg, slug)["version"] != expected_version:
        raise Conflict()
    fm, body = read_page(get(cfg, slug)["path"])
    fm["title"] = title[:100]
    fm["description"] = " ".join(description.split())[:500]
    fm["aliases"] = clean_terms(aliases)
    fm["search_terms"] = clean_terms(terms)
    fm["arxiv_categories"] = cats
    fm["discover"] = bool(discover)
    write_page(get(cfg, slug)["path"], fm, vault.set_section(body, "Overview", overview[:20000], PLACEHOLDER))
    return read(cfg, slug)["version"]
