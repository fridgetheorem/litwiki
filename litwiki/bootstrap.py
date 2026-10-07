"""One-off: propose an initial set of broad concept pages from the Zotero library."""
import json
from collections import Counter

from . import vault
from .common import db, log
from .ingest import zotero_meta
from .llm import claude

SYSTEM = """You design the concept taxonomy for a researcher's literature-review wiki.
Given their library (titles grouped by Zotero collection), propose broad research-area concept pages.
- 20-35 concepts. Each should plausibly cover >= 4 papers in the library and be a recognised research
  area or method family (e.g. "Protocol fuzzing", "Active automata learning", "Saliency-based explanation").
- Avoid course codes, project names, and administrative groupings.
- Concepts may overlap; a paper will link to several.
- search_terms: 3-6 phrases suitable for arXiv/OpenAlex keyword search to find NEW papers in that area.
- arxiv_categories: 0-3 relevant arXiv categories (e.g. cs.CR, cs.SE, cs.LG, cs.FL)."""

SCHEMA = {
    "type": "object",
    "properties": {"concepts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string", "description": "one sentence"},
            "search_terms": {"type": "array", "items": {"type": "string"}},
            "arxiv_categories": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "description", "search_terms", "arxiv_categories"]}}},
    "required": ["concepts"],
}


def bootstrap(cfg) -> list[str]:
    con = db()
    groups: dict[str, list[str]] = {}
    tags = Counter()
    for r in con.execute("SELECT data FROM items WHERE status IN ('queued','done')"):
        m = zotero_meta(con, json.loads(r["data"]))
        for c in m["collections"] or ["(unfiled)"]:
            groups.setdefault(c, []).append(m["title"])
        tags.update(m["zotero_tags"])
    lines = []
    for c in sorted(groups):
        lines.append(f"\n## {c}")
        lines += [f"- {t}" for t in groups[c]]
    prompt = ("LIBRARY:\n" + "\n".join(lines) +
              "\n\nMOST COMMON TAGS: " + ", ".join(t for t, _ in tags.most_common(60)))
    out = claude(cfg, prompt, system=SYSTEM, model=cfg["llm"]["summary_model"], schema=SCHEMA,
                 purpose="bootstrap", ref="concepts")
    slugs = []
    for c in out["concepts"]:
        slugs.append(vault.create_concept(cfg["vault"], c["title"], c["description"], c["search_terms"],
                                          c["arxiv_categories"], origin="bootstrap"))
    log.info("created %d concepts", len(slugs))
    return slugs
