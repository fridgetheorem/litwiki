"""Document text extraction (page-aware) and quote verification."""
import re
from html.parser import HTMLParser
from pathlib import Path

import pymupdf as fitz


def pdf_pages(path: Path) -> list[str]:
    with fitz.open(path) as doc:
        return [p.get_text("text") for p in doc]


class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    text = "".join(p.out)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def html_pages(html: str, chunk: int = 4000) -> list[str]:
    """Split web text into pseudo-pages ('sections') so claims can be located."""
    text = html_text(html)
    pages, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) > chunk and cur:
            pages.append(cur)
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        pages.append(cur)
    return pages


def build_prompt_text(pages: list[str], max_chars: int, unit: str = "page") -> tuple[str, bool]:
    """Join pages with explicit markers the model must cite. Returns (text, truncated)."""
    parts, total = [], 0
    for i, t in enumerate(pages, 1):
        t = re.sub(r"[ \t]+", " ", t).strip()
        chunk = f"\n=== [{unit} {i}] ===\n{t}\n"
        if total + len(chunk) > max_chars:
            parts.append(f"\n=== [truncated: {len(pages) - i + 1} more {unit}s omitted] ===\n")
            return "".join(parts), True
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts), False


# ---------- quote verification ----------

_LIG = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "’": "'", "‘": "'",
                      "“": '"', "”": '"', "–": "-", "—": "-", "−": "-"})


def _norm(s: str) -> str:
    s = s.translate(_LIG).lower()
    s = re.sub(r"-\s*\n\s*", "", s)          # de-hyphenate line breaks
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def _shingle_hit(q: str, page: str, n: int = 3) -> float:
    w = q.split()
    if len(w) < n:
        return 1.0 if q in page else 0.0
    grams = [" ".join(w[i:i + n]) for i in range(len(w) - n + 1)]
    return sum(g in page for g in grams) / len(grams)


def locate_quote(quote: str, pages: list[str], hint: int | None) -> tuple[int | None, str]:
    """Find which page contains `quote`. Returns (page, status) where status is
    'verified' (on cited page), 'moved' (found on another page), or 'unverified'."""
    q = _norm(quote)
    if not q:
        return hint, "unverified"
    normed = [_norm(p) for p in pages]
    order = list(range(len(pages)))
    if hint and 1 <= hint <= len(pages):
        order.remove(hint - 1)
        order.insert(0, hint - 1)
    for i in order:
        if q in normed[i]:
            return i + 1, "verified" if i + 1 == hint else "moved"
    # Quotes may straddle a page break or be lightly paraphrased: shingle overlap.
    best, best_i = 0.0, None
    for i in order:
        joined = normed[i] + " " + (normed[i + 1] if i + 1 < len(normed) else "")
        h = _shingle_hit(q, joined)
        if h > best:
            best, best_i = h, i
    if best >= 0.8:
        return best_i + 1, "verified" if best_i + 1 == hint else "moved"
    return hint, "unverified"
