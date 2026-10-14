"""BibTeX entries for paper pages. Built from the Zotero item data in state.db when the paper came from
Zotero (real item type, volume, pages, ...), otherwise from the page's frontmatter. Imported by the MkDocs
hook, so keep it light and read-only."""
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

from . import vault
from .common import ROOT, read_page

PARTICLES = {"van", "von", "de", "der", "den", "di", "da", "del", "della", "du", "la", "le", "ter", "ten", "bin", "al", "dos", "das"}
SPECIAL = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_"}
VENUE_WORDS = re.compile(r"proceedings|conference|symposium|workshop|\bconf\b|\bproc\b|congress|meeting", re.I)


def esc(text: str) -> str:
    text = " ".join(str(text).split())
    return "".join(SPECIAL.get(ch, ch) for ch in text)


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def name_bibtex(name: str) -> str:
    """'Jane van der Berg' -> 'van der Berg, Jane'; names already 'Last, First' are kept; one-word names
    (organisations) are braced so BibTeX doesn't read them as a surname."""
    name = " ".join(name.split())
    if "," in name:
        return esc(name)
    parts = name.split(" ")
    if len(parts) == 1:
        return "{" + esc(name) + "}"
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in PARTICLES:
        i -= 1
    return f"{esc(' '.join(parts[i:]))}, {esc(' '.join(parts[:i]))}"


def authors_field(names: list[str], truncated: bool = False) -> str:
    names = [n for n in names if n.strip() and not n.strip().isdigit()]  # junk like a bare numeric id
    return " and ".join([name_bibtex(n) for n in names] + (["others"] if truncated and names else []))


def _zotero_authors(d: dict) -> list[str]:
    out = []
    for c in d.get("creators", []):
        if c.get("creatorType") not in ("author", "programmer", "presenter", "contributor", "interviewee"):
            continue
        if c.get("name"):
            out.append(c["name"])
        elif c.get("lastName"):
            out.append(f"{c['lastName']}, {c.get('firstName', '')}".rstrip(", "))
    return out


def _year(fm: dict, d: dict | None) -> str:
    if d:
        m = re.search(r"\b(?:19|20)\d{2}\b", d.get("date", ""))
        if m:
            return m.group(0)
    return str(fm["year"]) if fm.get("year") else ""


def _ztype(d: dict, fm: dict) -> tuple[str, dict]:
    """(entry type, fields) from a Zotero item."""
    t = d["itemType"]
    f = {"title": d.get("title") or fm.get("title", "")}
    if t == "journalArticle":
        f.update(journal=d.get("publicationTitle"), volume=d.get("volume"), number=d.get("issue"), pages=d.get("pages"),
                 publisher=d.get("publisher"), issn=d.get("ISSN"))
        return "article", f
    if t == "conferencePaper":
        f.update(booktitle=d.get("proceedingsTitle") or d.get("conferenceName"), pages=d.get("pages"),
                 publisher=d.get("publisher"), address=d.get("place") or d.get("eventPlace"), volume=d.get("volume"),
                 series=d.get("series"), isbn=d.get("ISBN"))
        return "inproceedings", f
    if t == "bookSection":
        f.update(booktitle=d.get("bookTitle"), pages=d.get("pages"), publisher=d.get("publisher"),
                 address=d.get("place"), edition=d.get("edition"), isbn=d.get("ISBN"), series=d.get("series"))
        return "incollection", f
    if t == "book":
        f.update(publisher=d.get("publisher"), address=d.get("place"), edition=d.get("edition"), isbn=d.get("ISBN"),
                 series=d.get("series"), volume=d.get("volume"))
        return "book", f
    if t == "thesis":
        kind = (d.get("thesisType") or "").lower()
        f.update(school=d.get("university"), address=d.get("place"), type=d.get("thesisType") or None)
        return ("mastersthesis" if "master" in kind else "phdthesis"), f
    if t == "report":
        f.update(institution=d.get("institution"), number=d.get("reportNumber"), type=d.get("reportType") or None)
        return "techreport", f
    if t == "preprint":
        repo, aid = d.get("repository") or "", re.sub(r"^arxiv:\s*", "", d.get("archiveID") or "", flags=re.I)
        if aid and "arxiv" in (repo + (d.get("archiveID") or "")).lower():
            f.update(eprint=aid, archiveprefix="arXiv")
        else:
            f["howpublished"] = f"{repo}, {aid}".strip(", ") or None
        return "misc", f
    # webpage, blogPost, computerProgram, forumPost, dataset, ... -> @misc
    f.update(howpublished=d.get("websiteTitle") or d.get("blogTitle") or d.get("forumTitle") or d.get("publisher") or None,
             note=None)
    return "misc", f


def _fm_type(fm: dict) -> tuple[str, dict]:
    """(entry type, fields) from page frontmatter alone (discovery items)."""
    f = {"title": fm["title"]}
    key, venue, source = str(fm.get("key", "")), str(fm.get("venue") or ""), str(fm.get("source", ""))
    if key.startswith("arxiv:"):
        f.update(eprint=key.split(":", 1)[1], archiveprefix="arXiv")
        return "misc", f
    if source.endswith(":blog") or (not fm.get("doi") and not venue):
        return "misc", f
    if VENUE_WORDS.search(venue):
        f["booktitle"] = venue
        return "inproceedings", f
    f["journal"] = venue
    return "article", f


def entry_fields(fm: dict, z: dict | None) -> tuple[str, dict, list[str]]:
    typ, f = _ztype(z, fm) if z else _fm_type(fm)
    names = _zotero_authors(z) if z else []
    if not names:
        names = [str(a) for a in (fm.get("authors") or [])]
    # OpenAlex-sourced discovery items are capped at 12 authors
    truncated = not z and str(fm.get("source", "")).startswith("discovery:article") and len(names) >= 12
    f["author"] = authors_field(names, truncated)
    if y := _year(fm, z):
        f["year"] = y
    doi = (z or {}).get("DOI") or fm.get("doi") or ""
    if doi:
        f["doi"] = doi
    url = (z or {}).get("url") or fm.get("url") or ""
    if url and (typ == "misc" or not doi):
        f["url"] = url
    if z and z.get("accessDate") and typ == "misc" and url:
        f["urldate"] = z["accessDate"][:10]
    return typ, f, names


def _key_base(fm: dict, names: list[str], z: dict | None) -> str:
    if z and (z.get("citationKey") or "").strip():
        return re.sub(r"[^\w:\-.]", "", z["citationKey"].strip())
    first = names[0] if names else ""
    last = first.split(",")[0] if "," in first else (first.split()[-1] if first.split() else "anon")
    last = re.sub(r"[^a-z0-9]", "", _ascii(last).lower()) or "anon"
    words = [w for w in re.findall(r"[a-z0-9]+", _ascii(str(fm.get("title", ""))).lower())
             if w not in {"a", "an", "the", "on", "of", "for", "to", "in", "and", "with", "towards", "toward"}]
    return f"{last}{fm.get('year') or ''}{words[0] if words else ''}"


def render(typ: str, key: str, fields: dict) -> str:
    order = ["author", "title", "journal", "booktitle", "year", "volume", "number", "pages", "publisher", "address",
             "series", "edition", "school", "institution", "type", "howpublished", "eprint", "archiveprefix", "doi",
             "url", "urldate", "isbn", "issn", "note"]
    lines = []
    for k in sorted((k for k, v in fields.items() if v), key=lambda k: order.index(k) if k in order else 99):
        v = fields[k]
        if k == "author":
            val = v                                   # already escaped, names braced where needed
        elif k == "title":
            val = "{" + esc(v) + "}"                  # extra braces preserve capitalisation
        elif k in ("doi", "url", "eprint", "archiveprefix", "isbn", "issn", "urldate"):
            val = str(v).strip()                      # URLs/DOIs: raw (hyperref / url package handle them)
        elif k == "pages":
            val = re.sub(r"\s*[-–—]+\s*", "--", str(v).strip())
        else:
            val = esc(v)
        lines.append(f"  {k} = {{{val}}}")
    return f"@{typ}{{{key},\n" + ",\n".join(lines) + "\n}"


def _zotero_items() -> dict[str, dict]:
    db = ROOT / "state" / "state.db"
    if not db.exists():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        return {k: json.loads(d) for k, d in con.execute("SELECT key, data FROM items WHERE status!='child'")}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def build_all(vault_dir: Path) -> dict[str, str]:
    """{paper stem: BibTeX entry} for every paper page, with citation keys unique across the library."""
    zitems = _zotero_items()
    rows = []
    for p in sorted((vault_dir / vault.PAPERS).glob("*.md")):
        fm, _ = read_page(p)
        if not fm.get("title"):
            continue
        z = zitems.get(str(fm.get("zotero_key") or ""))
        typ, f, names = entry_fields(fm, z)
        rows.append((p.stem, typ, f, _key_base(fm, names, z)))
    seen: dict[str, int] = {}
    out = {}
    for stem, typ, f, base in rows:
        n = seen.get(base, 0)
        seen[base] = n + 1
        key = base if n == 0 else f"{base}{chr(ord('a') + n - 1) if n <= 26 else n}"
        out[stem] = render(typ, key, f)
    return out
