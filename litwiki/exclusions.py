"""Topics the daily discovery digest should not show.

Two layers, because they fail differently:
- `keywords`: phrases matched (whole words, case-insensitive) against a candidate's title and abstract BEFORE triage.
  Deterministic and free; matches are dropped and remembered (status 'excluded') so they are never fetched again.
- `topics`: plain-words description handed to the triage model, which scores matching papers low. Catches what a
  keyword list cannot (a paper about the topic that never uses the word).

Rules come from `discovery.exclude_topics` / `discovery.exclude_keywords` in config.yaml plus state/exclusions.json,
which the web Settings page edits (the host's config.yaml is not something web users can change).
They apply to the global daily digest only: a project has its own `exclude:` and may want what is excluded globally."""
import json
import re

from .common import STATE_DIR, write_atomic

FILE = STATE_DIR / "exclusions.json"
MAX_KEYWORDS = 100


def clean_keywords(raw) -> list[str]:
    items = re.split(r"[\n,;]+", raw) if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    for k in items:
        k = " ".join(str(k).split())[:60]
        if len(k) >= 3 and k.lower() not in {o.lower() for o in out}:  # 1-2 letter keywords would match everything
            out.append(k)
    return out[:MAX_KEYWORDS]


def _saved() -> dict:
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}


def load(cfg) -> dict:
    """{'topics': str, 'keywords': [..]} merged from config.yaml and the web-edited file."""
    dc = cfg.get("discovery") or {}
    saved = _saved()
    topics = " ".join(p for p in (str(dc.get("exclude_topics") or "").strip(), str(saved.get("topics") or "").strip()) if p)
    return {"topics": topics, "keywords": clean_keywords([*(dc.get("exclude_keywords") or []), *(saved.get("keywords") or [])])}


def load_saved() -> dict:
    """Only the web-edited part (what the Settings page shows and saves)."""
    s = _saved()
    return {"topics": str(s.get("topics") or ""), "keywords": clean_keywords(s.get("keywords"))}


def save(topics: str, keywords) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    write_atomic(FILE, json.dumps({"topics": " ".join(str(topics).split())[:1000], "keywords": clean_keywords(keywords)}, indent=1))


def matcher(keywords):
    """Compiled whole-word, case-insensitive matcher over a keyword list, or None when there are none."""
    if not keywords:
        return None
    return re.compile("|".join(rf"(?<!\w){re.escape(k)}(?!\w)" for k in keywords), re.I)


def split(cands: list[dict], keywords) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(kept, [(dropped candidate, keyword that matched)])."""
    rx = matcher(keywords)
    if rx is None:
        return cands, []
    kept, dropped = [], []
    for c in cands:
        m = rx.search(f"{c.get('title', '')}\n{c.get('abstract', '')}")
        if m:
            dropped.append((c, m.group(0).lower()))
        else:
            kept.append(c)
    return kept, dropped
