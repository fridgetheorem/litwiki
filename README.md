# litwiki

A lightweight literature-review wiki: a Markdown vault that you read and edit in Obsidian and browse on the web,
built automatically from your Zotero library and a literature search of arXiv, OpenAlex and RSS feeds.
Claude summarises each paper with page-cited, verified quotes; you tick what to ingest.

```
Zotero (read-only key) ──┐
                         ├─► claude -p (summary + verified quotes) ─► <vault>/papers/*.md
arXiv / OpenAlex / RSS ─► triage ─► digests/*.md ─[tick in Obsidian / web / Discord]─┘      │
                                                                 concept backlinks ◄────────┘
<vault> ◄─► Syncthing ◄─► Obsidian (your devices)
<vault> ─► MkDocs (hourly, if changed) ─► Caddy (localhost) ─► tailscale serve / SSH tunnel ─► your browser
```

## Set up your own deployment

**Everything you need to change lives in one file, `config.yaml`** (copied from `config.example.yaml`), plus
secrets in `~/.env`. No tracked file contains paths, hostnames or IDs of any particular machine; the Caddyfile,
`mkdocs.yml` and the helper scripts take their paths and ports from `config.yaml`.

### Prerequisites
A Linux host (or always-on machine) with: Python ≥ 3.11, `git`, `cron`,
[Claude Code](https://docs.claude.com/en/docs/claude-code) (the `claude` CLI, on `PATH` as `~/.local/bin/claude`),
[Caddy](https://caddyserver.com/docs/install), and optionally [Syncthing](https://syncthing.net/) (to sync the vault to
Obsidian on your devices) and [Tailscale](https://tailscale.com/) (to reach the site privately from your devices).
You also need a Zotero account and an Anthropic API key (console.anthropic.com).

### Steps
1. **Install**
   ```
   git clone <this repo> ~/litwiki && cd ~/litwiki
   python3 -m venv .venv && .venv/bin/pip install -e .
   ```
   (`bin/` scripts find the repo from their own location, so it can live anywhere.)
2. **Configure.** `cp config.example.yaml config.yaml` and edit the "Set these" section: `vault` (where the Markdown
   lives), `zotero.user_id`, `zotero.exclude_collections`, optionally `site_url`. Ports, Syncthing, schedule and the
   LLM budget caps are below it with defaults. The budget caps are real money limits: check `budget:` before the first run.
3. **Add secrets.** `cp .env.example ~/.env && chmod 600 ~/.env`, then fill in:
   - `ZOTERO_API_KEY`: Zotero → Settings → Feeds/API → new key, **read-only** access to your library.
   - `ANTHROPIC_API_KEY`: an API key. **This matters:** without it `claude -p` silently uses a Claude subscription login,
     which hits session limits (summaries fail with "You've hit your session limit") and makes the dollar budgets meaningless.
   - `LITWIKI_WEB_PASSWORD`: any long random string; the password for the `/app/` web UI.
   - optional `DISCORD_WEBHOOK_URL` / `DISCORD_BOT_TOKEN` (see "Discord bot" below).
4. **Smoke-test.**
   ```
   .venv/bin/litwiki sync                 # pulls your Zotero library into state/state.db (no LLM use)
   .venv/bin/litwiki status               # items queued, spend so far
   .venv/bin/litwiki bootstrap            # one LLM call: proposes ~20-35 concept pages from your library
   .venv/bin/litwiki ingest --limit 3     # summarise 3 papers (~$0.05-0.15 each); open them in <vault>/papers/
   ```
5. **Schedule it.** Install the cron entries (they are generated from `schedule:` in `config.yaml`):
   ```
   (crontab -l 2>/dev/null; .venv/bin/litwiki cron) | crontab -
   bin/watchdog.sh                        # starts Caddy, the web UI, Syncthing now (cron then keeps them alive)
   ```
   Hourly: Zotero sync → ingest → site rebuild. Daily: discovery digest. The rest of your library is summarised in the
   background, limited per day by `budget.backfill_daily_usd`; `litwiki status` shows progress.
6. **Reach the site.** Caddy listens on `127.0.0.1:<ports.site>` only. Either
   - `sudo tailscale serve --bg --http=8080 http://127.0.0.1:8080` and open `http://<host>.<your-tailnet>.ts.net:8080`
     (tailnet-only; the web UI additionally needs `LITWIKI_WEB_PASSWORD`), or
   - `ssh -L 8080:localhost:8080 you@host` and open `http://localhost:8080`.
7. **Open the vault in Obsidian (optional).** Syncthing's admin page is `http://127.0.0.1:<syncthing.gui_address>` on the host
   (tunnel it the same way; the first visit asks you to set a GUI password). Share the vault folder with your device,
   install Syncthing there, accept, and open the synced folder as an Obsidian vault. Sync uses TCP/UDP 22000 and UDP 21027;
   relays are on by default, so no port forwarding is needed.

### Troubleshooting
- **"You've hit your session limit" in `litwiki status`:** `ANTHROPIC_API_KEY` is missing or not being read (step 3). Then `litwiki retry`.
- **Blank page through Tailscale:** the Caddyfile must match any Host header (it does by default); restart Caddy after editing it.
- **After editing `config.yaml`:** ports and paths are read by the watchdog at start; `pkill caddy` (and `pkill -f "bin/litwiki serve"`) and run `bin/watchdog.sh`.
- Logs: `logs/{hourly,daily,manual,watchdog}.log`, `logs/{caddy,web,discord}.out`.

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

## Attaching a PDF to a paper
For pages summarised from the abstract only (flagged *Missing PDFs* on the status page), for Zotero items that never got a page, or to
replace a bad PDF:
```
litwiki attach-pdf PAGE-OR-ZOTERO-KEY paper.pdf [--force]
```
or use the **Attach PDF** button on the paper page (shown on abstract-only pages) or the *attach PDF* links on the status page
(`/app/attach/<page or key>` in the web UI). The PDF is saved as `_pdfs/<key>.pdf`, then the page is re-summarised from the full text
in place (about $0.05-0.15; page-cited, verified quotes). The page keeps its filename, tags, collections, concepts, reviews, aliases,
merged Zotero keys and notes; the summary body, `pdf`, `fulltext`, `needs_pdf` and `claims_verified` are regenerated. If the Zotero item
turns out to be a duplicate of an existing page, the PDF goes to that page. A page you edited by hand (frontmatter `status` other than
`auto`) is refused unless `--force` / "Overwrite my edits". The PDF needs selectable text (scanned PDFs: OCR first). The web upload runs
as a background job; failures are sent as notifications and logged in `logs/web-jobs.log`. Zotero is not modified.

## Projects (grow a literature review over time)
A **project** is a review page (`reviews/<slug>.md`) whose paper set grows as you work. Create one with `litwiki review-new "Title"`, write its question
and scope, then open **Projects** in the web UI (`/app/projects`) or press *Screen papers for this project* on its wiki page.
`/app/project/<slug>` is a grid of cards for bulk decisions (visually distinct from the swipe view): **In library** (papers you already have that are not
linked yet), **New** (candidates from the project's searches, see *Literature reviews*) and **Linked**. Tap cards to select them, then **Link to project**
(free), **Ingest new** (summarises them, about $0.05-0.15 each, and they join the project), **Dismiss new** or **Unlink**. **Analyze** runs a cheap Haiku pass
(about $0.002 per 12 papers, so a whole library costs a few cents) that gives each card a 0-10 *fit* score and 2-3 bullets on how it relates to the project;
results are cached, library papers are analysed most-keyword-overlap first, 120 at a time.

**Daily project digests.** Set `daily: true` on a project's page (or press *Turn on daily digest* on `/app/projects`; `status: archived` pauses it). After the
global discovery, the daily job searches each such project's `search_terms` for recent papers only (arXiv: last `discovery.lookback_days` days; OpenAlex: its longer
window), scores them with the triage model against the project's question and scope, and writes `digests/project-<slug>-<date>.md` (nothing is written when
nothing qualifies). Papers already seen for the project, ingested, or dismissed are skipped. The picks appear in the swipe queue, Inbox, Discord and the project's
*New* tab; ingesting one links it to the project. `litwiki search <slug> --recent` runs the same thing now. Cost: a few triage calls (a fraction of a cent each)
per project per day; per-term result cap: `discovery.project_daily_per_term` (default 25).

**Project notes (whiteboard).** `/app/project/<slug>/notes` is a free-form markdown scratchpad per project, stored in `notes/projects/<slug>.md` (so Obsidian can edit
it too). New notes start with *Ideas / Questions / Outline / To do* headings; the box above the editor is a quick-add that appends a dated bullet; the side panel lists
the project's linked papers and inserts a `[[papers/...|Title]]` link at the cursor when you click one. It autosaves a few seconds after you stop typing (no site
rebuild), **Save & publish** (or Ctrl/⌘+S) also rebuilds the wiki, where the notes appear on the project's page under *Notes*. If the file changed elsewhere (Obsidian)
since you opened it, a save is refused and you choose *Keep mine* or *Use theirs*, so nothing is silently overwritten. Membership is the `reviews:` list in a paper's frontmatter (so
`tag:#review/<slug>` works in Obsidian), and the review page keeps an auto-managed *Linked papers* list.

## Duplicates
The same paper can appear twice (added twice in Zotero, a bare arXiv link next to the real item, a preprint and its published
version). Pages count as duplicates when they share a **DOI**, an **arXiv id**, or an identical title (>= 20 characters, with
overlapping authors or none); similar-but-different titles such as "Lecture 02" / "Lecture 03" are never matched.
```
litwiki dupes                      # list duplicate groups and which page would be kept (changes nothing)
litwiki dedupe [--yes]             # merge every group (dry run unless --yes)
litwiki merge KEEP DROP... [--dry-run]   # merge specific pages (stems or Zotero keys), duplicates or not
```
The kept page is the one you edited or took notes on, else the one with a real title, DOI and full text. Merging unions collections,
Zotero tags, concepts and `review/` tags; records the other Zotero keys in `also_zotero_keys` (so syncs never recreate or overwrite
the page); appends the dropped pages' notes; rewrites `[[links]]` to the dropped pages; keeps their old names as Obsidian `aliases`;
and fixes the concept pages' paper lists. The kept page's body is not rewritten. New Zotero items that duplicate an existing page are
attached to it automatically (no new summary, no LLM cost). Zotero itself is never modified (the API key is read-only), so the
duplicates stay in Zotero; delete them there if you like, and the merged page is unaffected.

## Tags (Obsidian)
Paper pages carry a native `tags:` property: `col/<collection path>` (nested, so `tag:#col/projects` matches
everything below it), `zt/<zotero tag>`, `concept/<slug>`, `source/<origin>` and `review/<slug>`. litwiki only
rewrites tags with those prefixes; tags you add yourself are kept. `litwiki retag` refreshes all pages.

## Literature reviews
`litwiki review-new "My review"` creates `reviews/my-review.md`. Write the research question and scope, fill in
`search_terms`, then set `run: true` (or `litwiki search my-review [--since 2019] [--terms ...]`). It searches
arXiv + OpenAlex back to `since_year`, scores candidates against your question/scope, and writes a digest to
tick. Papers ingested from it get `#review/my-review`. Runs are manual only; the daily job is unchanged.

## Web UI (`/app/`)
Log in at `<your site>/app/` with `LITWIKI_WEB_PASSWORD` from `~/.env` (the static site itself stays open to the tailnet).
- **Add**: paste DOIs / arXiv IDs / URLs (one per line) or upload PDFs, optionally attaching a review or concept. Items are queued
  (`candidates`, `digest='web'`) and ingested immediately via `litwiki web-queue` (also swept by the hourly job). Failed items get a Retry button.
  User-supplied URLs are fetched only from public addresses (no loopback / private / tailnet, checked on every redirect).
- **Swipe** (`/app/swipe`): screen the pending digest items one card at a time, on your phone or desktop. Swipe (or drag, or press →) right to
  ingest, left (←) to dismiss, down to skip for now, `Z` to undo, `Enter` to expand the abstract. Cards come from every pending digest, daily and
  review-search, newest digest first and best triage score first. Each choice is saved immediately but only applied when you press **Finish**
  (ingesting costs money, so you can undo until then; the same applies when the deck runs out). Choices you never finish are applied by the hourly job
  once they are 10 minutes old. Ingest = ticking the item in its digest (like Obsidian), dismiss = rejected, so the Discord bot and Inbox stay in sync.
- **Inbox**: digest items as buttons: Approve ticks `- [x]` in the digest file (same as Obsidian) and ingests now; Dismiss rejects.
- **Notes**: `notes/<paper-stem>.md` per paper (so paper bodies stay untouched). The build embeds them in the paper page under "Notes (web)"
  and saving rebuilds the site. If the note was edited elsewhere (Obsidian) since the form loaded, the save is refused rather than overwriting.
- Runs as `litwiki serve` on 127.0.0.1:8081, started by the watchdog; Caddy proxies `/app/*`. Logs: `logs/serve.log`, `logs/web-jobs.log`.
- Change the password by editing `~/.env` and restarting (`pkill -f "bin/litwiki serve"`; the watchdog restarts it); all sessions are then logged out.

## Paper map (`/clusters/`)
Interactive 2-D map of every paper (pan/zoom, hover for TL;DR, click to open), built with the site and linked from both navbars.
Papers are shaded by main concept in translucent regions that overlap like a Venn diagram, with each concept's name above its region (drawn in front of the dots; it
sticks to the top of the screen while any of the region is visible). Built for touch as well as mouse: pinch/spread to zoom, one finger to pan, tap a paper to focus it,
plus +/−/⌂ buttons (and `+`/`-` keys). On narrow screens (under 900 px) the details panel starts hidden so the map gets the full height; it opens when you pick a paper, or with **Details**.
Switch the layout between **TL;DR text similarity** (TF-IDF → SVD → t-SNE over the page sections in `config.yaml` → `clusters.sections`)
and **concept membership**. Colour = the paper's primary (first-listed) concept; filter by concept, year or text; click legend rows to hide groups.
Layouts are cached in `state/clusters.json` and recomputed only when a paper's text or concepts change; new layouts are aligned
to the previous one so the map keeps its orientation. To cluster on more than the TL;DR, edit `sections` and run `litwiki build --force`.

## Discord bot (`litwiki discord`)
Started by the watchdog when `DISCORD_BOT_TOKEN` is in `~/.env`. Invite it with
`https://discord.com/oauth2/authorize?client_id=<application id>&scope=bot%20applications.commands&permissions=84992`.
- Polls `state.db` every minute; each new digest item (from digests of the last `discord.backfill_days` days) is posted as an embed with
  **Ingest** / **Dismiss** buttons (plus a PDF link). Ingest ticks the digest line and runs `web-queue`; messages update to ✅ + a link when the
  page exists, and also follow approvals/dismissals made in the web UI or Obsidian. Buttons survive restarts.
- The digest header message links to the full digest, the swipe page and the Inbox (needs `site_url` in `config.yaml`).
- Slash commands: `/add` (DOI / arXiv / URL or a PDF attachment, optional review/concept), `/status`, `/digest` (post now), `/here` (use this channel).
- Picks a channel itself on joining a server (prefers #litwiki, #digest, #literature… then the system channel) and remembers it in `state.db`;
  `discord.channel_id` in `config.yaml` forces one. Pipeline alerts (budget, crashed jobs) go to the same channel when no webhook is set.
- Only the application's owner/team (and `discord.allowed_user_ids`) can press buttons or run commands.
- State: table `discord_posts`. Logs: `logs/discord.log`, `logs/discord.out`.

## Asking questions (`litwiki ask`, web **Ask**, Discord `/ask`)
Generic questions over the whole library ("what major topics do these papers share?", "which papers evaluate on X?").
`litwiki/ask.py` puts id, title, authors, year, concepts and TL;DR of every paper (~40k tokens at 375 papers) in one prompt. If the model
says it needs specifics the TL;DRs lack, it names ≤30 papers and a second call reads their findings, limitations and **your notes**.
Citations are `[@page-stem]` in the model output that we rewrite into wiki links (ids not in the library show as `[?id]`), so URLs can't be invented.
Filters: `--concept`, `--review`, `--since YEAR`; `--no-detail` forces a single call. Typically $0.05–0.30 per question; capped by
`budget.ask_daily_usd` (separate from the pipeline caps) and the usual daily/monthly stops; spend is logged under purpose `ask`.
Past ~2000 papers the corpus won't fit in one prompt (`MAX_CORPUS_CHARS`); it will then ask you to filter, and retrieval (embeddings) would be the next step.
The web page runs questions in the background and keeps a history (table `asks`).

## BibTeX
Every paper page has a **Copy BibTeX** button (and a collapsible preview) above the TL;DR. Entries are generated at site-build time by
`litwiki/bibtex.py`: for Zotero papers from the Zotero item data in `state.db` (real entry type — article, inproceedings, incollection, book,
thesis, techreport, misc — plus volume/issue/pages/publisher, and your Zotero Citation Key if you set one), for discovery papers from the
page frontmatter (arXiv → `@misc` with `eprint`). Keys are `lastnameYEARfirstword`, unique across the library (`a`, `b` suffixes on collisions).
Titles are double-braced to keep capitalisation; `& % $ # _` are escaped. The site is plain HTTP, so the button falls back to `execCommand`
when `navigator.clipboard` is unavailable. Entries are only as good as the Zotero metadata (web pages come out as `@misc` without authors).
Changing `bibtex.py` needs `litwiki build --force`.

## Citations on paper pages
Each paper page on the site ends with **Citations & related papers**, the same data the map shows when you focus a paper: the OpenAlex
citation count, library papers it **cites** / is **cited by**, papers sharing **references** with it, and papers with **similar text**,
plus a *Show on map* link. It is generated at build time from `state/related.json` (written by the same step as the map data), so it is
site-only: the Markdown in the vault is not changed. Papers without a DOI or arXiv id have no citation data and show only similar text.

## Recently added sidebar
The top of the wiki's left sidebar (and mobile drawer) lists the 20 most recently ingested papers (collapsible; title, authors, year, day).
Order comes from `ingested_at` in each paper's frontmatter (set when the page is created); older pages were backfilled with
`litwiki backfill-ingested` from the pipeline's ingest times (Zotero items), the add time (manual adds), or midnight of the `ingested` day
(digest-approved pages made before the field existed). The home page's "Recently added" list uses the same order. Count: `RECENT_N` in `site/hooks.py`.

**Focus mode.** Click a paper (or arrive from the **Show on map** button on its page, `/clusters/#<page-stem>`) to focus it: the map zooms to it and
draws links to its neighbours, which are also listed in the side panel (click one to move the focus there; ↗ opens its page; Esc or a background click clears).
Neighbour types, each toggleable: **citations** (solid blue; the paper cites it or is cited by it), **shared references** (dashed orange; bibliographic
coupling, ≥2 shared references, top 8) and **similar text** (dotted grey; TF-IDF cosine on the TL;DR, top 8). Citation data comes from OpenAlex
(`litwiki/citations.py`, table `cite_meta`; needs a DOI or arXiv id, so web pages only get text neighbours): `litwiki citations` fetches it, the hourly job
tops it up for new papers. Dot size = citation count; labels are "Author Year" (plus a short title when zoomed or focused) and are placed greedily by
importance without overlapping; concept names mark the map regions. The page template is `litwiki/map_page.html`.

## Schedule (crontab)
- every 5 min: `bin/watchdog.sh` keeps Caddy, Syncthing, the web UI and the Discord bot alive (no systemd needed)
- hourly :17: `litwiki hourly` — Zotero sync → ingest approved digest items → ingest Zotero queue → health checks → rebuild site if the vault changed (this is also how Obsidian edits reach the web)
- daily: `litwiki daily` — discovery digest + Discord notification
(Times come from `schedule:` in `config.yaml`; `litwiki cron` prints the crontab lines.)

## Budget
All LLM calls go through `litwiki/llm.py`, which records `total_cost_usd` per call in `state/state.db`.
- monthly hard stop (warn at 80%), daily hard stop (anomaly guard) and a per-call cap (`--max-budget-usd`); all set under `budget:` / `llm:` in `config.yaml`
- the pre-existing Zotero backlog is metered separately (`budget.backfill_daily_usd`; roughly $0.05-0.11 per paper); items added to Zotero after install and approved digest items aren't subject to that meter (only the daily/monthly caps)
- every stop/warning is sent to Discord. Tune in `config.yaml`.

## Commands
```
.venv/bin/litwiki status        # queue, spend by purpose, failures
.venv/bin/litwiki sync|ingest [--limit N]|discover|approved|build [--force]
.venv/bin/litwiki retry         # requeue failed items
.venv/bin/litwiki notify-test   # check Discord
.venv/bin/litwiki bootstrap     # (one-off) propose concept pages from the library
.venv/bin/litwiki cron          # print crontab lines;  config-env prints the exports the watchdog uses
```
Logs: `logs/{hourly,daily,manual}.log`, `logs/notifications.log`.

## Secrets (`~/.env`, mode 600)
See `.env.example`: `ZOTERO_API_KEY` (read-only), `ANTHROPIC_API_KEY`, `LITWIKI_WEB_PASSWORD`, optional `DISCORD_WEBHOOK_URL`, `DISCORD_BOT_TOKEN`.
