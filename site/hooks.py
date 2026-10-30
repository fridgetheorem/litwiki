"""MkDocs hooks: make an Obsidian vault render faithfully.
- [[wikilinks]] (path or shortest-name, |alias, #heading) -> relative Markdown links
- [[_pdfs/X.pdf#page=4|p. 4]] -> /_pdfs/X.pdf#page=4 (served directly, not copied into the site)
- > [!type] Title callouts -> Material admonitions
- a Backlinks section listing pages that link here (minus pages this page already links to)
"""
import html
import os
import posixpath
from urllib.parse import quote
import re
from collections import defaultdict

from mkdocs.structure.files import Files

WIKI = re.compile(r"(!?)\[\[([^\]|#]+)?(#[^\]|]*)?(?:\|([^\]]*))?\]\]")
FM = re.compile(r"\A---\n.*?\n---\n?", re.S)
_index: dict[str, str] = {}          # lowercase stem/path -> src_uri
_titles: dict[str, str] = {}         # src_uri -> title
_back: dict[str, set] = defaultdict(set)
_out: dict[str, set] = defaultdict(set)
_bib: dict[str, str] = {}           # paper stem -> BibTeX entry
_recent: list[dict] = []            # newest ingested papers, for the sidebar block
_related: dict = {}                 # per-paper citation / similarity neighbours (state/related.json)
RECENT_N = 20


def _resolve(target: str) -> str | None:
    t = target.strip().removesuffix(".md").lower()
    return _index.get(t) or _index.get(posixpath.basename(t))


def _slug(h: str) -> str:
    return re.sub(r"[^\w\- ]", "", h.lower()).strip().replace(" ", "-")


def on_files(files: Files, config):
    _index.clear(); _titles.clear(); _back.clear(); _out.clear(); _bib.clear()
    _recent.clear(); _related.clear()
    try:
        from litwiki import citations
        _related.update(citations.load_related())
    except Exception as e:
        print(f"WARNING: related-papers data unavailable: {e}")
    try:
        from pathlib import Path
        from litwiki.bibtex import build_all
        from litwiki.vault import recent_papers
        _bib.update(build_all(Path(config["docs_dir"])))
        _recent.extend(recent_papers(Path(config["docs_dir"]), RECENT_N))
    except Exception as e:  # these are extras: never break the site build
        print(f"WARNING: bibtex / recent-papers generation failed: {e}")
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


def _with_notes(markdown: str, here: str, config, meta: dict | None = None) -> str:
    """Paper pages: embed notes/<stem>.md (written in the web UI or Obsidian) and link to the editor."""
    stem = posixpath.basename(here).removesuffix(".md")
    note = ""
    try:
        note = FM.sub("", open(os.path.join(config["docs_dir"], "notes", stem + ".md"), encoding="utf-8").read(), 1).strip()
    except OSError:
        pass
    label = "Edit notes" if note else "Add notes"
    attach = f" [Attach PDF](/app/attach/{stem}){{ .md-button }}" if (meta or {}).get("needs_pdf") else ""
    return (markdown.rstrip() + "\n\n## Notes (web)\n\n" + (note + "\n\n" if note else "")
            + f"[{label}](/app/notes/{stem}){{ .md-button }}{attach}\n")


def _with_citations(markdown: str, here: str) -> str:
    """Citations & related papers (the data behind the map's focus mode), before the notes block."""
    try:
        from litwiki.citations import section_markdown
        block = section_markdown(posixpath.basename(here).removesuffix(".md"), _related)
    except Exception as e:  # an extra: never break the page
        print(f"WARNING: citations section failed for {here}: {e}")
        return markdown
    return markdown.rstrip() + "\n\n" + block if block else markdown


def _with_bib(markdown: str, here: str) -> str:
    """Copy-BibTeX button (plus a collapsible preview) before the first section of a paper page."""
    entry = _bib.get(posixpath.basename(here).removesuffix(".md"))
    if not entry:
        return markdown
    stem = posixpath.basename(here).removesuffix(".md")
    block = ('<div class="lw-bib"><button type="button" class="md-button md-button--primary lw-copy">Copy BibTeX</button> '
             f'<a class="md-button" href="/clusters/#{quote(stem)}">Show on map</a> '
             '<details><summary>Show entry</summary><pre><code>' + html.escape(entry, quote=False) + '</code></pre></details></div>')
    i = markdown.find("\n## ")
    return markdown[:i] + "\n\n" + block + "\n" + markdown[i:] if i >= 0 else markdown + "\n\n" + block + "\n"


def on_page_markdown(markdown: str, page, config, files):
    here = page.file.src_uri
    if here.startswith("papers/"):
        markdown = _with_bib(markdown, here)
        markdown = _with_citations(markdown, here)
        markdown = _with_notes(markdown, here, config, page.meta)

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


_LINK_STYLE = ('style="align-self:center;font-size:.8rem;font-weight:600;text-decoration:none;color:inherit;'
               'padding:0 .8rem"')
_APP_LINK = (f'<a class="md-header__button" href="/clusters/" title="Visual map of papers" {_LINK_STYLE}>Map</a>\n        '
             f'<a class="md-header__button" href="/app/" title="Add papers, approve digests, edit notes" {_LINK_STYLE}>Manage</a>\n        ')


def on_post_page(output: str, page, config):
    """Header links from the wiki to the paper map and the web UI (their navbars link back)."""
    marker = '<form class="md-header__option" data-md-component="palette">'
    if marker in output:
        output = output.replace(marker, _APP_LINK + marker, 1)
    nav = '<ul class="md-nav__list" data-md-scrollfix>'  # first one is the primary (left) navigation
    if (block := _recent_block()) and nav in output:
        output = output.replace(nav, block + nav, 1).replace("</head>", _RECENT_CSS + "</head>", 1)
    return output.replace("</body>", _COPY_JS + "</body>", 1)


# Delegated click handler, on every page so it survives Material's instant navigation. The site is plain HTTP
# on the tailnet (not a secure context), so navigator.clipboard is often unavailable: fall back to execCommand.
_COPY_JS = """<script>if(!window.__lwBib){window.__lwBib=1;document.addEventListener('click',function(e){
var b=e.target.closest&&e.target.closest('.lw-copy');if(!b)return;var box=b.closest('.lw-bib'),txt=box.querySelector('code').textContent;
function done(ok){b.textContent=ok?'Copied \\u2713':'Copy failed \\u2014 select the text below';if(!ok)box.querySelector('details').open=true;
setTimeout(function(){b.textContent='Copy BibTeX'},ok?2000:6000)}
function fallback(){var t=document.createElement('textarea');t.value=txt;t.style.cssText='position:fixed;opacity:0';document.body.appendChild(t);
t.select();var ok=false;try{ok=document.execCommand('copy')}catch(_){}document.body.removeChild(t);done(ok)}
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(txt).then(function(){done(true)},fallback)}else fallback()})}</script>"""


_RECENT_CSS = """<style>
.lw-recent{margin:.4rem .6rem .8rem;padding:.4rem .6rem;border:1px solid var(--md-default-fg-color--lightest);border-radius:6px;font-size:.68rem}
.lw-recent summary{cursor:pointer;font-weight:700;font-size:.72rem;color:var(--md-default-fg-color--light)}
.lw-recent ol{list-style:none;margin:.4rem 0 0;padding:0}.lw-recent li{margin:0;padding:.18rem 0;line-height:1.25;border-top:1px solid var(--md-default-fg-color--lightest)}
.lw-recent li:first-child{border-top:0}.lw-recent a{display:block;color:var(--md-typeset-a-color);text-decoration:none;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.lw-recent a:hover{text-decoration:underline}.lw-recent small{color:var(--md-default-fg-color--light)}
</style>"""


def _recent_block() -> str:
    if not _recent:
        return ""
    items = []
    for r in _recent:
        day = r["ts"][5:10] if len(r["ts"]) >= 10 else ""
        meta = " · ".join(x for x in [r["authors"], str(r["year"] or ""), day] if x)
        title = html.escape(r["title"], quote=True)
        items.append(f'<li><a href="/papers/{quote(r["stem"])}/" title="{title}">{title}</a><small>{html.escape(meta)}</small></li>')
    return (f'<details class="lw-recent" open><summary>Recently added ({len(_recent)})</summary><ol>' + "".join(items)
            + "</ol></details>")
