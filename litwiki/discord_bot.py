"""Discord bot: posts each new digest item as a message with Ingest / Dismiss buttons, and offers /add,
/digest and /status. Autonomous: it polls state.db for new digest items (no coupling to the cron job),
picks a channel by itself, keeps messages up to date as items are ingested (by the bot, the web UI or
Obsidian), and survives restarts (persistent buttons). Only the application's owner / team (plus any
`discord.allowed_user_ids` in config.yaml) can press buttons or run commands."""
import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from urllib.parse import quote

import discord
from discord import app_commands
from discord.ext import tasks

from . import ask as askmod, manual, vault
from .common import db, kv_get, kv_set, log, read_page
from .discover import TICKED, tick_items
from .llm import spent

SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_posts (
    token TEXT PRIMARY KEY,          -- sha1(cid)[:16], used in button custom_ids
    cid TEXT UNIQUE, channel_id INTEGER, message_id INTEGER,
    state TEXT,                      -- pending | approved | done | dismissed | failed | rejected
    note TEXT, ts TEXT DEFAULT (datetime('now'))
);"""
PREFERRED = ("litwiki", "digest", "digests", "literature", "papers", "research", "bot", "bots", "general")
COLOURS = {"pending": 0x5865F2, "approved": 0xE67E22, "done": 0x2ECC71, "dismissed": 0x95A5A6,
           "failed": 0xE74C3C, "rejected": 0xE74C3C}
MAX_PER_DIGEST = 25


def token_for(cid: str) -> str:
    return hashlib.sha1(cid.encode()).hexdigest()[:16]


def _con():
    con = db()
    con.executescript(SCHEMA)
    return con


def site_url(cfg) -> str:
    return (cfg.get("site_url") or "").rstrip("/")


# ---------- message rendering ----------

def render(cfg, c: dict, state: str, note: str = "", page: str | None = None) -> tuple[discord.Embed, discord.ui.View | None, bool]:
    """(embed, link-only view, has_buttons) for a candidate in a given state."""
    emb = discord.Embed(title=c["title"][:250], url=c.get("url") or None, colour=COLOURS[state])
    byline = " — ".join(x for x in [", ".join(c.get("authors", [])[:3]) + (" et al." if len(c.get("authors", [])) > 3 else ""),
                                    f"*{c['venue']}*, {c.get('date', '')}" if c.get("venue") else c.get("date", "")] if x)
    emb.description = "\n".join(x for x in [byline, c.get("why", "")] if x)[:1500] or None
    status = {"pending": "", "approved": f"⏳ Ingesting… {note}", "dismissed": f"✖ Dismissed {note}",
              "failed": f"❌ Ingest failed — retry from the web UI. {note}",
              "rejected": f"❌ No usable content — add the PDF with `/add`. {note}", "done": "✅ Ingested"}[state]
    if state == "done" and page and site_url(cfg):
        status = f"✅ Ingested → [open page]({site_url(cfg)}/{quote(page.removesuffix('.md'))}/)"
    if status:
        emb.add_field(name="​", value=status.strip(), inline=False)
    if c.get("score") not in (None, ""):
        emb.set_footer(text=f"relevance {c['score']}/10 · {'abstract-only' if not c.get('pdf_url') and c.get('kind') == 'article' else c.get('kind', '')}")
    view = discord.ui.View(timeout=None)
    if state == "pending":
        view.add_item(ApproveButton(token_for(c["id"])))
        view.add_item(DismissButton(token_for(c["id"])))
    if c.get("pdf_url"):
        view.add_item(discord.ui.Button(label="PDF", url=c["pdf_url"]))
    return emb, (view if view.children else None), state == "pending"


# ---------- buttons (DynamicItem => they keep working across restarts) ----------

class ApproveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lw:approve:(?P<token>[0-9a-f]{16})"):
    def __init__(self, token: str):
        super().__init__(discord.ui.Button(label="Ingest", style=discord.ButtonStyle.success,
                                           custom_id=f"lw:approve:{token}"))
        self.token = token

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["token"])

    async def callback(self, interaction: discord.Interaction):
        await handle_button(interaction, "approve", self.token)


class DismissButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lw:dismiss:(?P<token>[0-9a-f]{16})"):
    def __init__(self, token: str):
        super().__init__(discord.ui.Button(label="Dismiss", style=discord.ButtonStyle.secondary,
                                           custom_id=f"lw:dismiss:{token}"))
        self.token = token

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["token"])

    async def callback(self, interaction: discord.Interaction):
        await handle_button(interaction, "dismiss", self.token)


class Bot(discord.Client):
    def __init__(self, cfg):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.tree = app_commands.CommandTree(self)
        self.allowed: set[int] = set(int(x) for x in (cfg.get("discord") or {}).get("allowed_user_ids") or [])
        self.channel_lock = asyncio.Lock()

    async def setup_hook(self):
        self.add_dynamic_items(ApproveButton, DismissButton)
        register_commands(self)
        info = await self.application_info()
        owners = {m.id for m in info.team.members} if info.team else {info.owner.id}
        self.allowed |= owners
        log.info("discord: allowed users %s", sorted(self.allowed))
        self.loop_task.start()

    async def on_ready(self):
        log.info("discord: logged in as %s in %d guild(s); invite: %s", self.user, len(self.guilds), invite_url(self))
        for g in self.guilds:
            await self.sync_guild(g)

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)
        await self.ensure_channel(announce=True)

    async def sync_guild(self, guild):
        try:  # guild-scoped sync is instant (global sync can take an hour to show up)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception:
            log.exception("command sync failed for %s", guild)

    # ----- channel selection -----

    async def ensure_channel(self, announce: bool = False):
        """The configured / remembered channel, or an automatically chosen one."""
        async with self.channel_lock:
            wanted = (self.cfg.get("discord") or {}).get("channel_id") or await asyncio.to_thread(lambda: kv_get(_con(), "discord_channel"))
            if wanted:
                ch = self.get_channel(int(wanted))
                if ch:
                    return ch
            for g in self.guilds:
                cands = [c for c in g.text_channels if c.permissions_for(g.me).send_messages and c.permissions_for(g.me).embed_links]
                cands.sort(key=lambda c: (PREFERRED.index(c.name) if c.name in PREFERRED
                                          else len(PREFERRED) + (c != g.system_channel), c.position))
                if cands:
                    ch = cands[0]
                    await asyncio.to_thread(lambda: kv_set(_con(), "discord_channel", ch.id))
                    log.info("discord: using #%s in %s for digests", ch.name, g.name)
                    if announce:
                        await ch.send("👋 litwiki is set up here: new digest items will appear in this channel with "
                                      "**Ingest** / **Dismiss** buttons. Use `/here` in another channel to move me, "
                                      "`/add` to add papers, `/status` for the queue.")
                    return ch
            return None

    # ----- main loop -----

    @tasks.loop(seconds=60)
    async def loop_task(self):
        try:
            ch = await self.ensure_channel()
            if ch:
                await self.post_new(ch)
                await self.reconcile()
        except Exception:
            log.exception("discord loop iteration failed")

    @loop_task.before_loop
    async def _wait(self):
        await self.wait_until_ready()

    async def post_new(self, ch):
        items = await asyncio.to_thread(new_items, self.cfg)
        sent_header = set()
        for digest, cid, c in items:
            if digest not in sent_header:
                sent_header.add(digest)
                if not await asyncio.to_thread(lambda: kv_get(_con(), f"discord_header:{digest}")):
                    await ch.send(await asyncio.to_thread(header_text, self.cfg, digest, sum(1 for i in items if i[0] == digest)))
                    await asyncio.to_thread(lambda: kv_set(_con(), f"discord_header:{digest}", 1))
        per: dict[str, int] = {}
        for digest, cid, c in items:
            per[digest] = per.get(digest, 0) + 1
            if per[digest] > MAX_PER_DIGEST:
                continue  # the rest stay in the web Inbox
            emb, view, _ = render(self.cfg, c, "pending")
            msg = await ch.send(embed=emb, view=view)
            await asyncio.to_thread(record_post, cid, ch.id, msg.id)

    async def reconcile(self):
        """Bring messages in line with reality (ingest finished, approved/dismissed elsewhere)."""
        for token, cid, channel_id, message_id, state, new_state, c, page in await asyncio.to_thread(transitions, self.cfg):
            try:
                ch = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                msg = ch.get_partial_message(message_id)
                emb, view, _ = render(self.cfg, c, new_state, page=page)
                await msg.edit(embed=emb, view=view)
            except discord.NotFound:
                log.info("discord: message for %s is gone", cid)
            except Exception:
                log.exception("discord: could not update message for %s", cid)
                continue
            await asyncio.to_thread(set_state, token, new_state, "")


def invite_url(bot) -> str:
    # View channels, send messages, embed links, read history
    return (f"https://discord.com/oauth2/authorize?client_id={bot.user.id}&scope=bot%20applications.commands"
            f"&permissions={1024 + 2048 + 16384 + 65536}")


# ---------- DB helpers (all run in worker threads) ----------

_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")


def new_items(cfg) -> list[tuple[str, str, dict]]:
    """Unposted, still-pending digest items from recent digests: [(digest, cid, candidate)]."""
    con = _con()
    days = int((cfg.get("discord") or {}).get("backfill_days", 2))
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    ticked = set()
    for p in (cfg["vault"] / vault.DIGESTS).glob("*.md"):
        ticked.update(TICKED.findall(p.read_text(encoding="utf-8")))
    out = []
    for r in con.execute("SELECT id, data, digest FROM candidates WHERE status='digested' AND digest!='web' "
                         "AND id NOT IN (SELECT cid FROM discord_posts) ORDER BY digest, score DESC"):
        m = _DATE.search(r["digest"] or "")
        if not m or m.group(1) < cutoff or r["id"] in ticked:
            continue
        out.append((r["digest"], r["id"], json.loads(r["data"])))
    return out


def header_text(cfg, digest: str, n: int) -> str:
    fm, _ = read_page(cfg["vault"] / digest)
    title = fm.get("title", digest)
    base = site_url(cfg)
    links = f" · [full digest]({base}/{digest.removesuffix('.md')}/) · [Inbox]({base}/app/inbox)" if base else ""
    return f"📚 **{title}** — {n} new item(s){links}"


def record_post(cid: str, channel_id: int, message_id: int, state: str = "pending") -> None:
    con = _con()
    con.execute("INSERT OR REPLACE INTO discord_posts(token, cid, channel_id, message_id, state) VALUES(?,?,?,?,?)",
                (token_for(cid), cid, channel_id, message_id, state))
    con.commit()


def set_state(token: str, state: str, note: str = "") -> None:
    con = _con()
    con.execute("UPDATE discord_posts SET state=?, note=? WHERE token=?", (state, note, token))
    con.commit()


def page_of(cfg, cid: str, c: dict, digest: str | None) -> str | None:
    if c.get("page"):
        return c["page"]
    if digest and digest != "web":
        m = re.search(r"\[\[([^\]|]+)\|ingested\]\] <!-- cand:" + re.escape(cid) + " -->",
                      (cfg["vault"] / digest).read_text(encoding="utf-8"))
        return m.group(1) + ".md" if m else None
    return None


def transitions(cfg) -> list[tuple]:
    """Posts whose message needs updating: (token, cid, channel, message, old_state, new_state, candidate, page)."""
    con = _con()
    ticked = set()
    for p in (cfg["vault"] / vault.DIGESTS).glob("*.md"):
        ticked.update(TICKED.findall(p.read_text(encoding="utf-8")))
    out = []
    for r in con.execute("SELECT p.*, c.status cstatus, c.data, c.digest FROM discord_posts p JOIN candidates c ON c.id=p.cid "
                         "WHERE p.state IN ('pending','approved') AND p.ts > datetime('now','-30 days')"):
        c = json.loads(r["data"])
        new = r["state"]
        if r["cstatus"] == "ingested":
            new = "done"
        elif r["cstatus"] == "rejected":
            new = "rejected" if r["state"] == "approved" or r["digest"] == "web" else "dismissed"
        elif r["cstatus"] == "failed":
            new = "failed"
        elif r["state"] == "pending" and r["cid"] in ticked:
            new = "approved"
        if new != r["state"]:
            out.append((r["token"], r["cid"], r["channel_id"], r["message_id"], r["state"], new, c,
                        page_of(cfg, r["cid"], c, r["digest"]) if new == "done" else None))
    return out


def act(cfg, kind: str, token: str) -> tuple[str, dict | None]:
    """Perform approve/dismiss. Returns (new_state or 'missing'/'stale', candidate)."""
    con = _con()
    row = con.execute("SELECT p.cid, c.data, c.status FROM discord_posts p JOIN candidates c ON c.id=p.cid "
                      "WHERE p.token=?", (token,)).fetchone()
    if not row:
        return "missing", None
    c = json.loads(row["data"])
    if row["status"] != "digested":
        return "stale", c
    if kind == "approve":
        tick_items(cfg, [row["cid"]])
        set_state(token, "approved")
        return "approved", c
    con.execute("UPDATE candidates SET status='rejected' WHERE id=? AND status='digested'", (row["cid"],))
    con.commit()
    set_state(token, "dismissed")
    return "dismissed", c


# ---------- interactions ----------

async def deny_unless_allowed(interaction) -> bool:
    if interaction.user.id in interaction.client.allowed:
        return False
    await interaction.response.send_message("Only the litwiki owner can do that.", ephemeral=True)
    return True


async def handle_button(interaction: discord.Interaction, kind: str, token: str):
    bot: Bot = interaction.client
    if await deny_unless_allowed(interaction):
        return
    state, c = await asyncio.to_thread(act, bot.cfg, kind, token)
    if state in ("missing", "stale"):
        await interaction.response.send_message("That item has already been handled (the message will update shortly)."
                                                if state == "stale" else "I no longer track that item.", ephemeral=True)
        return
    emb, view, _ = render(bot.cfg, c, state, note=f"by {interaction.user.display_name}")
    await interaction.response.edit_message(embed=emb, view=view)
    if state == "approved":
        await asyncio.to_thread(manual.spawn_queue)


def register_commands(bot: Bot):
    tree, cfg = bot.tree, bot.cfg

    @tree.command(name="add", description="Add a paper: DOI, arXiv ID or URL (or attach a PDF) and ingest it")
    @app_commands.describe(item="DOI, arXiv ID or URL", file="A PDF to ingest instead", review="Review slug to tag it with",
                           concept="Concept slug to file it under")
    async def add(interaction: discord.Interaction, item: str | None = None, file: discord.Attachment | None = None,
                  review: str = "", concept: str = ""):
        if await deny_unless_allowed(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            if file is not None:
                if file.size > manual.MAX_UPLOAD:
                    raise manual.AddError("That file is over 60 MB.")
                cid, title = await asyncio.to_thread(manual.add_pdf, cfg, file.filename, await file.read(), review, concept)
            elif item:
                cid, title = await asyncio.to_thread(manual.add_text, cfg, item.strip(), review, concept)
            else:
                raise manual.AddError("Give me a DOI, arXiv ID or URL, or attach a PDF.")
        except manual.AddError as e:
            await interaction.followup.send(f"⚠️ {e}")
            return
        c = {"id": cid, "title": title, "authors": [], "kind": "manual"}
        emb, view, _ = render(cfg, c, "approved")
        msg = await interaction.followup.send(embed=emb, wait=True)
        await asyncio.to_thread(record_post, cid, msg.channel.id, msg.id, "approved")
        await asyncio.to_thread(manual.spawn_queue)

    @tree.command(name="ask", description="Ask a question about your whole paper library")
    @app_commands.describe(question="e.g. What major topics do my papers share?", concept="Only papers in this concept (slug)",
                           review="Only papers tagged with this review (slug)", since="Only papers from this year on")
    async def ask_cmd(interaction: discord.Interaction, question: str, concept: str = "", review: str = "",
                      since: int | None = None):
        if await deny_unless_allowed(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            r = await asyncio.to_thread(askmod.ask, cfg, question, concept, review, since)
        except (askmod.AskError, askmod.BudgetExceeded) as e:
            await interaction.followup.send(f"⚠️ {e}")
            return
        except Exception as e:
            log.exception("discord /ask failed")
            await interaction.followup.send(f"⚠️ Something went wrong: {e}"[:500])
            return
        chunks = split_answer(r["answer"])
        for i, chunk in enumerate(chunks):
            emb = discord.Embed(title=question[:250] if i == 0 else None, description=chunk, colour=0x5865F2)
            if i == len(chunks) - 1:
                emb.set_footer(text=f"{r['n_papers']} papers read · {len(r['cited'])} cited · ${r['cost']:.2f}")
            await interaction.followup.send(embed=emb)

    @tree.command(name="status", description="Queue, pending digest items and LLM spend")
    async def status(interaction: discord.Interaction):
        if await deny_unless_allowed(interaction):
            return
        def q():
            con = _con()
            pend = con.execute("SELECT COUNT(*) FROM candidates WHERE status='digested' AND digest!='web'").fetchone()[0]
            web = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM candidates WHERE digest='web' GROUP BY status")}
            z = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM items WHERE status!='child' GROUP BY status")}
            return pend, web, z, spent(con, "day"), spent(con, "month")
        pend, web, z, day, month = await asyncio.to_thread(q)
        base = site_url(cfg)
        await interaction.response.send_message(
            f"**Pending digest items:** {pend}" + (f" ([Inbox]({base}/app/inbox))" if base else "") +
            f"\n**Added items:** {', '.join(f'{k} {v}' for k, v in web.items()) or 'none'}"
            f"\n**Zotero queue:** {', '.join(f'{k} {v}' for k, v in sorted(z.items()))}"
            f"\n**LLM spend:** ${day:.2f} today · ${month:.2f} this month (cap ${cfg['budget']['monthly_usd']:.0f})")

    @tree.command(name="digest", description="Post any new digest items now (otherwise checked every minute)")
    async def digest(interaction: discord.Interaction):
        if await deny_unless_allowed(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        ch = await bot.ensure_channel()
        if not ch:
            await interaction.followup.send("I can't find a channel I'm allowed to post in.")
            return
        n = len(await asyncio.to_thread(new_items, cfg))
        await bot.post_new(ch)
        await bot.reconcile()
        await interaction.followup.send(f"Posted {n} new item(s) in {ch.mention}." if n else "Nothing new to post.")

    @tree.command(name="here", description="Use this channel for digests and alerts")
    async def here(interaction: discord.Interaction):
        if await deny_unless_allowed(interaction):
            return
        await asyncio.to_thread(lambda: kv_set(_con(), "discord_channel", interaction.channel_id))
        await interaction.response.send_message("📚 Digests and alerts will be posted in this channel from now on.")


def split_answer(text: str, limit: int = 3800) -> list[str]:
    """Split on paragraph boundaries so each piece fits an embed description (4096 chars)."""
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:  # pathological single paragraph
            cut = para.rfind(" ", 0, limit) or limit
            chunks.append(para[:cut]); para = para[cut:].lstrip()
        if len(cur) + len(para) + 2 > limit and cur:
            chunks.append(cur); cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    return [*chunks, cur] if cur else chunks or ["(empty answer)"]


def run(cfg) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set in ~/.env")
    _con()
    dl = logging.getLogger("discord")  # route the library's own warnings to our log file
    dl.handlers[:] = log.handlers
    dl.setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.ERROR)  # 'voice not supported' noise
    Bot(cfg).run(token, log_handler=None)
