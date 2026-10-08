# litwiki

A lightweight literature-review wiki: a Markdown vault (`~/vault`) that you read and edit in Obsidian
and browse on the web, built automatically from your Zotero library and a daily literature search.

```
Zotero (cloud, read-only key) ──┐
                                ├─► claude -p (summary + verified quotes) ─► ~/vault/papers/*.md
arXiv / OpenAlex / RSS ─► triage ─► digests/*.md ─[tick in Obsidian]─┘           │
                                                          concept backlinks ◄────┘
~/vault ◄─► Syncthing ◄─► Obsidian (your devices)
~/vault ─► MkDocs (hourly, if changed) ─► Caddy :8080 (localhost) ─► `sudo tailscale serve --bg --http=8080 http://127.0.0.1:8080`
```

## Vault layout

| Path | What | Who edits |
|---|---|---|
| `papers/<author><year>-<title>.md` | One page per paper | Created once by litwiki; **body is never rewritten**. Only Zotero-owned frontmatter fields (title, authors, tags, collections…) are refreshed. |
| `concepts/<slug>.md` | One page per broad concept | You. litwiki only edits between `<!-- litwiki:papers:start/end -->` (append-only list). `search_terms` / `arxiv_categories` / `discover` in the frontmatter steer the daily search. |
| `reviews/<slug>.md` | One page per literature review: research question, scope, `search_terms`, `since_year`, `exclude` | You. Set `run: true` to search (next hourly run); litwiki resets it. |
| `digests/YYYY-MM-DD.md`, `digests/search-<review>-<date>.md` | Daily / per-review search digest | Tick `- [x]` to ingest an item (next hourly run). |
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
