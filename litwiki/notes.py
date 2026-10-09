"""Per-paper notes live in their own files (vault/notes/<paper-stem>.md), never in the paper page,
so litwiki's 'paper bodies are never rewritten' guarantee holds. The site build embeds them in the paper page."""
import re
from datetime import datetime, timezone
from pathlib import Path

from . import vault
from .common import read_page, write_page

NOTES = "notes"
STEM = re.compile(r"^[\w.\-]+$")


class Conflict(Exception):
    pass


def paper_path(v: Path, stem: str) -> Path:
    if not STEM.match(stem) or stem.startswith("."):
        raise FileNotFoundError(stem)
    p = v / vault.PAPERS / f"{stem}.md"
    if not p.is_file():
        raise FileNotFoundError(stem)
    return p


def note_path(v: Path, stem: str) -> Path:
    paper_path(v, stem)  # validates the stem
    return v / NOTES / f"{stem}.md"


def read_note(v: Path, stem: str) -> tuple[str, int]:
    """(text, mtime_ns); mtime is 0 when there is no note yet."""
    p = note_path(v, stem)
    if not p.exists():
        return "", 0
    _, body = read_page(p)
    return body.strip("\n"), p.stat().st_mtime_ns


def save_note(v: Path, stem: str, text: str, expected_mtime: int) -> None:
    """Write the note body. Raises Conflict if the file changed (e.g. edited in Obsidian) since the form
    was loaded. Empty text deletes the note."""
    p = note_path(v, stem)
    current = p.stat().st_mtime_ns if p.exists() else 0
    if current != expected_mtime:
        raise Conflict()
    text = text.replace("\r\n", "\n").strip("\n")
    if not text.strip():
        p.unlink(missing_ok=True)
        return
    fm = read_page(p)[0] if p.exists() else {}
    pfm, _ = read_page(paper_path(v, stem))
    fm.update(title=f"Notes: {pfm.get('title', stem)}", paper=f"papers/{stem}", type="note",
              updated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    write_page(p, fm, text + "\n")


def recent(v: Path, n: int = 10) -> list[tuple[str, str]]:
    """(stem, paper title) for the most recently edited notes."""
    out = []
    for p in sorted((v / NOTES).glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]:
        pp = v / vault.PAPERS / p.name
        if pp.is_file():
            out.append((p.stem, read_page(pp)[0].get("title", p.stem)))
    return out
