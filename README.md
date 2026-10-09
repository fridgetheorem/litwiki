# litwiki

A lightweight literature-review wiki: a Markdown vault (`~/vault`) that you read and edit in Obsidian
and browse on the web, built automatically from your Zotero library and a daily literature search.

```
Zotero (cloud, read-only key) ──┐
                                ├─► claude -p (summary + verified quotes) ─► ~/vault/papers/*.md
arXiv / OpenAlex / RSS ─► triage ─► digests/*.md ─[tick in Obsidian]─┘           │
                                                          concept backlinks ◄────┘
~/vault ◄─► Syncthing ◄─► Obsidian (your devices)
~/vault ─► MkDocs (hourly, if changed) ─► Caddy :8080 (localhost) ─► `tailscale serve --bg --http=8080 http://127.0.0.1:8080`
```

Served on the tailnet at `http://litwiki.example.ts.net:8080/` (the machine's Tailscale name; `tailscale serve` is bound to that name, so after a rename run `tailscale serve reset` and the command above again).

## Vault layout

| Path | What | Who edits |
|---|---|---|
| `papers/<author><year>-<title>.md` | One page per paper | Created once by litwiki; **body is never rewritten**. Only Zotero-owned frontmatter fields (title, authors, tags, collections…) are refreshed. |
| `concepts/<slug>.md` | One page per broad concept | You. litwiki only edits between `<!-- litwiki:papers:start/end -->` (append-only list). `search_terms` / `arxiv_categories` / `discover` in the frontmatter steer the daily search. |
| `reviews/<slug>.md` | One page per literature review: research question, scope, `search_terms`, `since_year`, `exclude` | You. Set `run: true` to search (next hourly run); litwiki resets it. |
| `digests/YYYY-MM-DD.md`, `digests/search-<review>-<date>.md` | Daily / per-review search digest | Tick `- [x]` to ingest an item (next hourly run). |
| `notes/<paper-stem>.md` | Your notes on a paper | You, in the web UI or Obsidian. Embedded into the paper page on the site. |
| `_pdfs/` | PDFs / web snapshots | litwiki. Linked as `[[_pdfs/KEY.pdf#page=7\|p. 7]]` — opens at that page in Obsidian and in the browser. |
| `index.md`, `status.md` | Home and pipeline status | Auto block only. |

### Claims and verification
Each finding/limitation carries the PDF page and a short verbatim quote. After summarisation every quote
is searched for in the extracted text: if it is on another page the page number is corrected; if it can't be
found the claim is marked **⚠ quote not found — check**. `claims_verified: n/m` is in each page's frontmatter.
Page numbers are *PDF* page indices (what `#page=N` opens), not printed page numbers.

## Tags (Obsidian)
Paper pages carry a native `tags:` property: `col/<collection path>` (nested, so `tag:#col/masters` matches
everything below it), `zt/<zotero tag>`, `concept/<slug>`, `source/<origin>` and `review/<slug>`. litwiki only
rewrites tags with those prefixes; tags you add yourself are kept. `litwiki retag` refreshes all pages.

## Literature reviews
`litwiki review-new "My review"` creates `reviews/my-review.md`. Write the research question and scope, fill in
`search_terms`, then set `run: true` (or `litwiki search my-review [--since 2019] [--terms ...]`). It searches
arXiv + OpenAlex back to `since_year`, scores candidates against your question/scope, and writes a digest to
tick. Papers ingested from it get `#review/my-review`. Runs are manual only; the daily job is unchanged.

## Web UI (`/app/`)
Log in at `http://litwiki.example.ts.net:8080/app/` with `LITWIKI_WEB_PASSWORD` from `~/.env` (the static site itself stays open to the tailnet).
- **Add**: paste DOIs / arXiv IDs / URLs (one per line) or upload PDFs, optionally attaching a review or concept. Items are queued
  (`candidates`, `digest='web'`) and ingested immediately via `litwiki web-queue` (also swept by the hourly job). Failed items get a Retry button.
  User-supplied URLs are fetched only from public addresses (no loopback / private / tailnet, checked on every redirect).
- **Inbox**: digest items as buttons: Approve ticks `- [x]` in the digest file (same as Obsidian) and ingests now; Dismiss rejects.
- **Notes**: `notes/<paper-stem>.md` per paper (so paper bodies stay untouched). The build embeds them in the paper page under "Notes (web)"
  and saving rebuilds the site. If the note was edited elsewhere (Obsidian) since the form loaded, the save is refused rather than overwriting.
- Runs as `litwiki serve` on 127.0.0.1:8081, started by the watchdog; Caddy proxies `/app/*`. Logs: `logs/serve.log`, `logs/web-jobs.log`.
- Change the password by editing `~/.env` and restarting (`pkill -f "bin/litwiki serve"`; the watchdog restarts it); all sessions are then logged out.

## Paper map (`/clusters/`)
Interactive 2-D map of every paper (pan/zoom, hover for TL;DR, click to open), built with the site and linked from both navbars.
Switch the layout between **TL;DR text similarity** (TF-IDF → SVD → t-SNE over the page sections in `config.yaml` → `clusters.sections`)
and **concept membership**. Colour = the paper's primary (first-listed) concept; filter by concept, year or text; click legend rows to hide groups.
Layouts are cached in `state/clusters.json` and recomputed only when a paper's text or concepts change; new layouts are aligned
to the previous one so the map keeps its orientation. To cluster on more than the TL;DR, edit `sections` and run `litwiki build --force`.

## Schedule (crontab)
- every 5 min: `bin/watchdog.sh` keeps Caddy + Syncthing alive (no systemd lingering on this host)
- hourly :17: `litwiki hourly` — Zotero sync → ingest approved digest items → ingest Zotero queue → health checks → rebuild site if the vault changed (this is also how Obsidian edits reach the web)
- daily 10:00 UTC: `litwiki daily` — discovery digest + Discord notification

## Budget
All LLM calls go through `litwiki/llm.py`, which records `total_cost_usd` per call in `state/state.db`.
- monthly hard stop $50 (warn at 80%), daily hard stop $4 (anomaly guard), per-call cap $1 (`--max-budget-usd`)
- the pre-existing Zotero backlog is metered at $1.20/day (~11 papers/day at ~$0.11/paper); items added to Zotero after install and approved digest items aren't subject to that meter (only the daily/monthly caps)
- every stop/warning is sent to Discord. Tune in `config.yaml`.

## Commands
```
.venv/bin/litwiki status        # queue, spend by purpose, failures
.venv/bin/litwiki sync|ingest [--limit N]|discover|approved|build [--force]
.venv/bin/litwiki retry         # requeue failed items
.venv/bin/litwiki notify-test   # check Discord
.venv/bin/litwiki bootstrap     # (one-off, already run) propose concept pages from the library
```
Logs: `logs/{hourly,daily,manual}.log`, `logs/notifications.log`.

## Secrets (`~/.env`, mode 600)
`ZOTERO_API_KEY` (read-only), `DISCORD_WEBHOOK_URL`, `SYNCTHING_GUI_USER/PASSWORD`.
