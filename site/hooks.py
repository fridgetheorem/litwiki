"""MkDocs hooks: make an Obsidian vault render faithfully.
- [[wikilinks]] (path or shortest-name, |alias, #heading) -> relative Markdown links
- [[_pdfs/X.pdf#page=4|p. 4]] -> /_pdfs/X.pdf#page=4 (served directly, not copied into the site)
- > [!type] Title callouts -> Material admonitions
- a Backlinks section listing pages that link here (minus pages this page already links to)
"""
import os
import posixpath
import re
from collections import defaultdict

from mkdocs.structure.files import Files

WIKI = re.compile(r"(!?)\[\[([^\]|#]+)?(#[^\]|]*)?(?:\|([^\]]*))?\]\]")
FM = re.compile(r"\A---\n.*?\n---\n?", re.S)
_index: dict[str, str] = {}          # lowercase stem/path -> src_uri
_titles: dict[str, str] = {}         # src_uri -> title
_back: dict[str, set] = defaultdict(set)
_out: dict[str, set] = defaultdict(set)


def _resolve(target: str) -> str | None:
    t = target.strip().removesuffix(".md").lower()
    return _index.get(t) or _index.get(posixpath.basename(t))


def _slug(h: str) -> str:
    return re.sub(r"[^\w\- ]", "", h.lower()).strip().replace(" ", "-")


def on_files(files: Files, config):
    _index.clear(); _titles.clear(); _back.clear(); _out.clear()
    docs = config["docs_dir"]
    md = [f for f in files.documentation_pages()]
    for f in md:
        stem = f.src_uri.removesuffix(".md").lower()
        _index[stem] = f.src_uri
        _index.setdefault(posixpath.basename(stem), f.src_uri)
    for f in md:
        try:
            text = open(os.path.join(docs, f.src_path), encoding="utf-8").read()
        except OSError:
            continue
        m = re.search(r"^title:\s*['\"]?(.+?)['\"]?\s*$", text[:3000], re.M)
        _titles[f.src_uri] = m.group(1) if m else posixpath.basename(f.src_uri).removesuffix(".md")
        for _, target, _, _ in WIKI.findall(FM.sub("", text, 1)):
            if target and (dst := _resolve(target)) and dst != f.src_uri:
                _back[dst].add(f.src_uri)
                _out[f.src_uri].add(dst)
    return files


def _callouts(md: str) -> str:
    out, lines, i = [], md.split("\n"), 0
    while i < len(lines):
        m = re.match(r"^>\s*\[!(\w+)\][+-]?\s*(.*)$", lines[i])
        if not m:
            out.append(lines[i]); i += 1; continue
        kind, title = m.group(1).lower(), m.group(2).strip()
        out.append(f'!!! {kind} "{title}"' if title else f"!!! {kind}")
        i += 1
        while i < len(lines) and lines[i].startswith(">"):
            out.append("    " + lines[i][1:].lstrip()); i += 1
        out.append("")
    return "\n".join(out)


def _with_notes(markdown: str, here: str, config) -> str:
    """Paper pages: embed notes/<stem>.md (written in the web UI or Obsidian) and link to the editor."""
    stem = posixpath.basename(here).removesuffix(".md")
    note = ""
    try:
        note = FM.sub("", open(os.path.join(config["docs_dir"], "notes", stem + ".md"), encoding="utf-8").read(), 1).strip()
    except OSError:
        pass
    label = "Edit notes" if note else "Add notes"
    return (markdown.rstrip() + "\n\n## Notes (web)\n\n" + (note + "\n\n" if note else "")
            + f"[{label}](/app/notes/{stem}){{ .md-button }}\n")


def on_page_markdown(markdown: str, page, config, files):
    here = page.file.src_uri
    if here.startswith("papers/"):
        markdown = _with_notes(markdown, here, config)

    def repl(m):
        embed, target, anchor, alias = m.groups()
        anchor = anchor or ""
        label = (alias or (target or "").rsplit("/", 1)[-1].removesuffix(".md") or anchor.lstrip("#")).strip()
        if target and target.strip().startswith("_pdfs/"):
            return f"[{label}](/{target.strip()}{anchor})"
        if not target:  # same-page heading link
            return f"[{label}](#{_slug(anchor[1:])})"
        dst = _resolve(target)
        if not dst:
            return f'<span class="wikilink-missing" title="missing page">{label}</span>'
        rel = posixpath.relpath(dst, posixpath.dirname(here) or ".")
        frag = "#" + _slug(anchor[1:]) if anchor and not anchor.startswith("#page=") else ""
        return f"[{label}]({rel}{frag})"

    markdown = WIKI.sub(repl, _callouts(markdown))
    back = sorted(_back.get(here, set()) - _out.get(here, set()), key=lambda s: _titles.get(s, s))
    if back:
        items = "\n".join(
            f"- [{_titles.get(s, s)}]({posixpath.relpath(s, posixpath.dirname(here) or '.')})" for s in back)
        markdown += f"\n\n---\n\n## Backlinks\n\n{items}\n"
    return markdown
