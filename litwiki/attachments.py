"""Images pasted or dropped into a project's notes. The file is stored in the vault (`_attachments/<project>/<hash>.<ext>`) and the
note gets an ordinary Markdown link to it, relative to the note (`notes/projects/<slug>.md`), so Obsidian, the wiki build and any
other Markdown tool resolve it. Nothing is embedded in the note itself. The server chooses the file name (a hash of the
content, so the same image pasted twice is stored once) and checks the file's own header rather than the type the browser claims."""
import hashlib
import re
from pathlib import Path

DIR = "_attachments"
MAX_BYTES = 10 * 1024 * 1024
NAME = re.compile(r"^[0-9a-f]{16}\.(png|jpg|gif|webp)$")
SLUG = re.compile(r"^[\w.\-]+$")


class ImageError(Exception):
    """Shown to the user as-is."""


def sniff(data: bytes) -> str:
    """The extension for an image we accept, from its header; raises ImageError for anything else."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise ImageError("Only PNG, JPEG, GIF and WebP images can be added.")


def save(vault: Path, slug: str, data: bytes) -> tuple[str, str]:
    """Store the image; returns (file name, Markdown link relative to notes/projects/<slug>.md)."""
    if not SLUG.match(slug) or slug.startswith("."):
        raise ImageError("Bad project.")
    if not data:
        raise ImageError("That image is empty.")
    if len(data) > MAX_BYTES:
        raise ImageError("That image is over %d MB." % (MAX_BYTES // 1024 // 1024))
    ext = sniff(data)
    name = f"{hashlib.sha256(data).hexdigest()[:16]}.{ext}"
    dest = vault / DIR / slug / name
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
    return name, f"![pasted image](../../{DIR}/{slug}/{name})"


def path_of(vault: Path, slug: str, name: str) -> Path | None:
    """The stored file, or None (the name must be one of ours, so nothing outside the folder can be asked for)."""
    if not SLUG.match(slug) or not NAME.match(name):
        return None
    p = vault / DIR / slug / name
    return p if p.is_file() else None
