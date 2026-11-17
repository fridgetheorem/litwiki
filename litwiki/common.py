"""Shared plumbing: config, secrets, state DB, logging, locking, vault I/O."""
import fcntl
import logging
import logging.handlers
import os
import re
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"

log = logging.getLogger("litwiki")


def setup_logging(name: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    log.handlers[:] = [fh]
    if sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.handlers.append(sh)
    log.setLevel(logging.INFO)


def load_env(path: Path = Path.home() / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


DEPLOY_DEFAULTS = {
    "ports": {"site": 8080, "web": 8081},
    "syncthing": {"enabled": False, "home": "~/.config/syncthing-litwiki", "gui_address": "127.0.0.1:8384"},
    "schedule": {"hourly_minute": 17, "daily_hour_utc": 10},
}


def load_config() -> dict:
    load_env()
    path = ROOT / "config.yaml"
    if not path.exists():
        raise SystemExit(f"{path} not found: copy config.example.yaml to config.yaml and edit it (see README.md)")
    cfg = yaml.safe_load(path.read_text()) or {}
    for section, defaults in DEPLOY_DEFAULTS.items():
        cfg[section] = {**defaults, **(cfg.get(section) or {})}
    cfg["vault"] = Path(os.path.expanduser(cfg["vault"]))
    return cfg


SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS items (
    key TEXT PRIMARY KEY,          -- zotero item key, or 'disc:<id>' for discovery items
    version INTEGER,
    data TEXT,                     -- JSON of zotero item data / candidate
    page TEXT,                     -- vault-relative page path once ingested
    status TEXT,                   -- queued | done | skipped | failed
    note TEXT,
    added TEXT DEFAULT (datetime('now')),
    updated TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS spend (
    ts TEXT DEFAULT (datetime('now')),
    purpose TEXT, model TEXT, usd REAL, ref TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,           -- arxiv:xxxx | doi:xxxx | url:xxxx
    title TEXT, data TEXT,
    seen TEXT DEFAULT (datetime('now')),
    score REAL, digest TEXT, status TEXT  -- triaged | digested | approved | ingested | rejected
);
CREATE TABLE IF NOT EXISTS cite_meta (
    page TEXT PRIMARY KEY,         -- vault-relative paper page
    oa_id TEXT,                    -- OpenAlex work id (https://openalex.org/W...)
    refs TEXT,                     -- JSON list of OpenAlex ids this paper cites
    cited_by INTEGER,              -- OpenAlex citation count
    found INTEGER,                 -- 0 = not in OpenAlex / no DOI
    fetched TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notified (k TEXT PRIMARY KEY, ts TEXT DEFAULT (datetime('now')));
"""


def db() -> sqlite3.Connection:
    STATE_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(STATE_DIR / "state.db", timeout=60)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def kv_get(con, k, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(con, k, v):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    con.commit()


@contextmanager
def run_lock(name="pipeline", wait=True):
    """Serialise jobs: only one LLM-using job at a time (RAM is tight)."""
    STATE_DIR.mkdir(exist_ok=True)
    with open(STATE_DIR / f"{name}.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise SystemExit(f"another '{name}' job is running")
        yield


def slugify(text: str, maxlen: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    if len(text) > maxlen:
        cut = text[:maxlen + 1]
        text = cut[:cut.rfind("-")] if "-" in cut else text[:maxlen]
    return text.strip("-") or "untitled"


# ---------- Markdown + frontmatter ----------

FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


def read_page(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, text[m.end():]


def dump_fm(fm: dict) -> str:
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000).strip() + "\n---\n"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_page(path: Path, fm: dict, body: str) -> None:
    write_atomic(path, dump_fm(fm) + "\n" + body.lstrip("\n"))


def replace_block(body: str, name: str, content: str) -> str:
    """Replace (or append) an auto-managed block delimited by HTML comments.
    Text outside the markers is never touched, so user edits survive."""
    start, end = f"<!-- litwiki:{name}:start -->", f"<!-- litwiki:{name}:end -->"
    block = f"{start}\n{content.strip()}\n{end}"
    if start in body and end in body:
        pre, rest = body.split(start, 1)
        _, post = rest.split(end, 1)
        return pre + block + post
    return body.rstrip() + "\n\n" + block + "\n"


def get_block(body: str, name: str) -> str:
    start, end = f"<!-- litwiki:{name}:start -->", f"<!-- litwiki:{name}:end -->"
    if start in body and end in body:
        return body.split(start, 1)[1].split(end, 1)[0].strip()
    return ""
