"""A reading-interest profile learned from what the user accepts and refuses, which the user can then judge.

Signal: candidates the user accepted (status 'ingested': ticked, swiped right, added by hand) and refused ('rejected').
Every so often a model reads the recent decisions and writes a short profile: things the user seems to LIKE, things they
seem to AVOID, and extra search TERMS. Every item must cite the decisions that support it, and the server checks that the
cited decisions exist and are enough in number, so the model cannot invent evidence. The profile page shows the items with
their evidence and lets the user mark each one accurate or wrong, edit it, or add their own. The verdicts matter twice:
wrong items are never used (and are not proposed again), and the daily scoring prompt gets the likes and dislikes that are
not marked wrong. Suggested search terms only enter the daily search once the user has confirmed them."""
import json
import re
import threading
from datetime import datetime, timezone

from .common import db, kv_get, kv_set, log
from .llm import BudgetExceeded, claude, spent_purpose_today

KINDS = ("like", "dislike", "term")                     # what the model may write; "pause" items are computed from the numbers
ALL_KINDS = KINDS + ("pause",)
MIN_SUPPORT = {"like": 3, "dislike": 3, "term": 2}      # decisions that must back an item
MAX_ITEMS = {"like": 8, "dislike": 8, "term": 6}
PAUSE_MIN_DECIDED, PAUSE_MAX_RATE = 10, 0.08            # suggest pausing a concept's daily search below this accept rate
MAX_ACCEPTED, MAX_REFUSED = 110, 150                    # most recent decisions shown to the model
MIN_DECISIONS, MIN_EACH = 20, 5                         # below this there is not enough to learn from
REBUILD_AFTER = 15                                      # new decisions that make the profile stale
JOB = {"running": False, "error": ""}                   # the web app's "Rebuild now"
_norm = lambda t: re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


class ProfileError(Exception):
    """Shown to the user as-is."""


SYSTEM = """You infer a researcher's reading interests from papers they ACCEPTED (added to their wiki) and papers they
REFUSED (dismissed from a daily digest). Write a short profile of their taste.
- "like": a topic, method or kind of paper that recurs among the ACCEPTED ones and is not just as common among the REFUSED ones.
- "dislike": a topic, method or kind of paper that recurs among the REFUSED ones and is rare or absent among the ACCEPTED ones.
  Never call something a dislike if papers on it were also accepted.
- "term": a search phrase of 1-4 words the daily search could add to find more of what they accept; not one already in
  EXISTING SEARCH TERMS.
Each item is one plain sentence (terms: the phrase only), specific rather than generic ("fuzzing of stateful network
protocols", not "security papers"). Every item must list the ids of the papers that support it (like and term: accepted ids
such as A12; dislike: refused ids such as R7), at least 3 ids (terms: 2). Only claim what the listed papers show.
Use the STATISTICS: a topic or venue whose papers are refused far more often than accepted is a real dislike, and one accepted
more often than refused is a real like, even when the individual papers look alike (the "found via" tag says which search
found a paper). Suggest up to 4 terms for themes the accepted papers show that EXISTING SEARCH TERMS do not already cover.
The user has already judged earlier items: CONFIRMED items are right (stay consistent, do not repeat them); items
REJECTED BY THE USER are wrong (never restate or imply them). Write at most 8 likes, 8 dislikes and 6 terms; fewer is fine."""

SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
        "kind": {"type": "string", "enum": list(KINDS)}, "text": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}}},
    "required": ["kind", "text", "evidence"]}}}, "required": ["items"]}


# ---------- the signal ----------

def counts(con) -> dict:
    r = {s: n for s, n in con.execute("SELECT status, COUNT(*) FROM candidates WHERE status IN ('ingested','rejected') GROUP BY status")}
    return {"accepted": r.get("ingested", 0), "refused": r.get("rejected", 0)}


def _as_list(v) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])


def stats(con) -> dict:
    """Exact accept/refuse counts over ALL decisions: {"concepts": {slug: [accepted, refused]}, "venues": {venue: [a, r]}}."""
    conc, ven = {}, {}
    for r in con.execute("SELECT status, data FROM candidates WHERE status IN ('ingested','rejected')"):
        d, i = json.loads(r["data"] or "{}"), 0 if r["status"] == "ingested" else 1
        for k in _as_list(d.get("query_concepts")):
            conc.setdefault(k, [0, 0])[i] += 1
        if d.get("venue"):
            ven.setdefault(str(d["venue"])[:60], [0, 0])[i] += 1
    return {"concepts": conc, "venues": ven}


def _stats_text(st: dict, titles: dict) -> str:
    def fmt(table, minn, names=None):
        rows = sorted(((a + r, k, a, r) for k, (a, r) in table.items() if a + r >= minn), reverse=True)[:18]
        return "\n".join(f"- {(names or {}).get(k, k)}: {a} accepted, {r} refused" for _, k, a, r in rows) or "(none)"
    return ("STATISTICS over ALL decisions (exact). By the concept whose search found the paper:\n" + fmt(st["concepts"], 6, titles)
            + "\nBy venue (6 or more decisions):\n" + fmt(st["venues"], 6))


def decisions(con) -> list[dict]:
    """The most recent accepted and refused candidates, newest first within each: [{id, title, abstract, venue, decision}]."""
    out = []
    for status, decision, cap in (("ingested", "accepted", MAX_ACCEPTED), ("rejected", "refused", MAX_REFUSED)):
        for r in con.execute("SELECT id, title, data, digest FROM candidates WHERE status=? ORDER BY seen DESC, rowid DESC LIMIT ?", (status, cap)):
            d = json.loads(r["data"] or "{}")
            out.append({"id": r["id"], "title": r["title"] or d.get("title", ""), "abstract": str(d.get("abstract") or "")[:420],
                        "venue": str(d.get("venue") or ""), "decision": decision, "by_hand": r["digest"] == "web",
                        "via": ", ".join(_as_list(d.get("query_concepts")))})
    return out


def _listing(ds: list[dict]) -> tuple[str, dict]:
    """Prompt text with short ids (A1.. accepted, R1.. refused) and the id -> decision map."""
    ids, lines = {}, []
    for kind, tag in (("accepted", "A"), ("refused", "R")):
        lines.append(f"\n{kind.upper()} PAPERS:")
        for n, d in enumerate((d for d in ds if d["decision"] == kind), 1):
            ids[f"{tag}{n}"] = d
            lines.append(f"[{tag}{n}] {d['title']}" + (f" ({d['venue']})" if d["venue"] else "") + (" [added by hand]" if d["by_hand"] else "")
                         + (f" [found via: {d['via']}]" if d["via"] else "") + (f"\n    {d['abstract']}" if d["abstract"] else ""))
    return "\n".join(lines), ids


# ---------- items ----------

def items(con=None) -> list[dict]:
    con = con or db()
    return [{**dict(r), "evidence": json.loads(r["evidence"] or "[]")}
            for r in con.execute("SELECT * FROM profile_items ORDER BY CASE kind WHEN 'like' THEN 0 WHEN 'dislike' THEN 1 WHEN 'term' THEN 2 ELSE 3 END, "
                                 "CASE verdict WHEN 'pending' THEN 0 WHEN 'ok' THEN 1 ELSE 2 END, id")]


def set_verdict(item_id: int, verdict: str, cfg=None) -> None:
    """Record the user's judgement. For a "pause" item, Accurate pauses that concept's daily search (discover: false in its
    concept page) and anything else resumes it, so the verdict always matches what the search does."""
    if verdict not in ("pending", "ok", "wrong"):
        raise ProfileError("Unknown verdict.")
    con = db()
    row = con.execute("SELECT kind, evidence FROM profile_items WHERE id=?", (item_id,)).fetchone()
    if row and row["kind"] == "pause" and cfg is not None:
        _set_discover(cfg, json.loads(row["evidence"] or "[]"), verdict != "ok")
    n = con.execute("UPDATE profile_items SET verdict=?, updated=datetime('now') WHERE id=?", (verdict, item_id)).rowcount
    con.commit()          # also when nothing matched: never leave a write transaction (and its lock) open
    if not n:
        raise ProfileError("That item no longer exists.")


def _set_discover(cfg, evidence: list[dict], on: bool) -> None:
    from .common import read_page, write_page
    from . import vault
    slug = next((e.get("concept") for e in evidence if e.get("concept")), None)
    path = cfg["vault"] / vault.CONCEPTS / f"{slug}.md" if slug else None
    if not path or not path.is_file():
        raise ProfileError("That concept page no longer exists.")
    fm, body = read_page(path)
    if bool(fm.get("discover", True)) != on:
        fm["discover"] = on
        write_page(path, fm, body)


def edit(item_id: int, text: str) -> None:
    text = " ".join(text.split())[:240]
    if not text:
        raise ProfileError("The text cannot be empty.")
    con = db()
    if (r := con.execute("SELECT kind FROM profile_items WHERE id=?", (item_id,)).fetchone()) and r["kind"] == "pause":
        con.commit()
        raise ProfileError("A pause suggestion is about one concept; mark it accurate or wrong instead.")
    n = con.execute("UPDATE profile_items SET text=?, source='user', verdict='ok', updated=datetime('now') WHERE id=?", (text, item_id)).rowcount
    con.commit()
    if not n:
        raise ProfileError("That item no longer exists.")


def add(kind: str, text: str) -> None:
    text = " ".join(text.split())[:240]
    if kind not in KINDS:
        raise ProfileError("Unknown kind.")
    if not text:
        raise ProfileError("Write the statement first.")
    con = db()
    con.execute("INSERT INTO profile_items(kind, text, verdict, source) VALUES(?,?,'ok','user')", (kind, text))
    con.commit()


def remove(item_id: int) -> None:
    con = db()
    n = con.execute("DELETE FROM profile_items WHERE id=? AND source='user'", (item_id,)).rowcount
    con.commit()
    if not n:
        raise ProfileError("Only items you wrote can be deleted; mark the others wrong instead.")


def accuracy(its: list[dict]) -> dict:
    """What the user has said about the auto-written items: {judged, ok, wrong, pending}."""
    auto = [i for i in its if i["source"] == "auto"]
    ok, wrong = sum(i["verdict"] == "ok" for i in auto), sum(i["verdict"] == "wrong" for i in auto)
    return {"judged": ok + wrong, "ok": ok, "wrong": wrong, "pending": len(auto) - ok - wrong}


# ---------- using it ----------

def applying(con=None) -> bool:
    return kv_get(con or db(), "profile_apply", "1") != "0"


def set_applying(on: bool) -> None:
    kv_set(db(), "profile_apply", "1" if on else "0")


def prompt_block(con=None) -> str:
    """Text for the daily scoring prompt: likes and dislikes that are not marked wrong. Empty when off or nothing to say."""
    con = con or db()
    if not applying(con):
        return ""
    its = [i for i in items(con) if i["verdict"] != "wrong" and i["kind"] in ("like", "dislike")]
    likes, dislikes = [i["text"] for i in its if i["kind"] == "like"], [i["text"] for i in its if i["kind"] == "dislike"]
    if not likes and not dislikes:
        return ""
    return ("READER PROFILE (learned from what this researcher accepted and refused before). A soft signal on top of the concept "
            "catalog: raise a score by 1-2 for a candidate that clearly matches a LIKE, lower it by 1-2 for one that clearly "
            "matches a DISLIKE; never let it override the catalog or the excluded topics.\n"
            + ("LIKES:\n" + "\n".join(f"- {t}" for t in likes) + "\n" if likes else "")
            + ("DISLIKES:\n" + "\n".join(f"- {t}" for t in dislikes) if dislikes else "")).strip()


def approved_terms(con=None) -> list[str]:
    """Search phrases the user has confirmed, to add to the daily search."""
    con = con or db()
    return [r["text"] for r in con.execute("SELECT text FROM profile_items WHERE kind='term' AND verdict='ok' ORDER BY id")][:MAX_ITEMS["term"]]


# ---------- building it ----------

def status(con=None) -> dict:
    con = con or db()
    c = counts(con)
    built = kv_get(con, "profile_built_at", "")
    n_then = int(kv_get(con, "profile_decisions", 0) or 0)
    enough = c["accepted"] + c["refused"] >= MIN_DECISIONS and min(c.values()) >= MIN_EACH
    return {**c, "built_at": built, "new_since": c["accepted"] + c["refused"] - n_then if built else None, "enough": enough}


def stale(con=None) -> bool:
    s = status(con)
    return s["enough"] and (not s["built_at"] or (s["new_since"] or 0) >= REBUILD_AFTER)


def _existing_terms(cfg) -> list[str]:
    from . import vault
    out = []
    for fm in vault.concepts(cfg["vault"]).values():
        out += [str(t) for t in (fm.get("search_terms") or [])]
    return out


def suggest_pauses(cfg, con) -> list[dict]:
    """Concepts whose daily search keeps turning up things the user refuses: computed from the counts, not by the model."""
    from . import vault
    st, out = stats(con)["concepts"], []
    refused = {}
    for r in con.execute("SELECT title, data FROM candidates WHERE status='rejected' ORDER BY seen DESC, rowid DESC"):
        for k in _as_list(json.loads(r["data"] or "{}").get("query_concepts")):
            refused.setdefault(k, []).append(r["title"])
    for slug, fm in vault.concepts(cfg["vault"]).items():
        a, rj = st.get(slug, [0, 0])
        if not fm.get("discover", True) or a + rj < PAUSE_MIN_DECIDED or a / (a + rj) > PAUSE_MAX_RATE:
            continue
        ev = [{"id": "", "title": f"{a} accepted, {rj} refused so far", "decision": "", "concept": slug}]
        ev += [{"id": "", "title": t, "decision": "refused"} for t in refused.get(slug, [])[:5]]
        out.append({"kind": "pause", "text": f"Stop the daily search for \u201c{fm.get('title', slug)}\u201d", "evidence": ev})
    return out


def validate(raw: list[dict], ids: dict) -> tuple[list[dict], int]:
    """Keep only items whose cited decisions exist, have the right sign and are enough in number. -> (items, dropped)."""
    keep, dropped, seen = [], 0, set()
    per_kind = {k: 0 for k in KINDS}
    for r in raw:
        kind, text = r.get("kind"), " ".join(str(r.get("text", "")).split())[:240]
        sign = "refused" if kind == "dislike" else "accepted"
        ev, used = [], set()
        for e in r.get("evidence") or []:
            d = ids.get(str(e).strip())
            if d and d["decision"] == sign and d["id"] not in used:
                used.add(d["id"]); ev.append({"id": d["id"], "title": d["title"], "decision": d["decision"]})
        if kind not in KINDS or not text or len(ev) < MIN_SUPPORT[kind] or _norm(text) in seen or per_kind[kind] >= MAX_ITEMS[kind]:
            dropped += 1
            continue
        seen.add(_norm(text)); per_kind[kind] += 1
        keep.append({"kind": kind, "text": text, "evidence": ev})
    return keep, dropped


def build(cfg) -> dict:
    """Write a fresh profile from the recent decisions. Items the user judged (or wrote) stay; the rest are replaced.
    Returns {items, dropped, cost}. Raises ProfileError (not enough data, over budget) with a message fit for the user."""
    con = db()
    st = status(con)
    if not st["enough"]:
        raise ProfileError(f"Not enough decisions yet to learn from ({st['accepted']} accepted, {st['refused']} refused; "
                           f"need at least {MIN_EACH} of each and {MIN_DECISIONS} in total).")
    cap = float((cfg.get("budget") or {}).get("profile_daily_usd", 1.0))
    if spent_purpose_today(con, "profile") >= cap:
        raise ProfileError(f"Today's profile budget (${cap:.2f}) is used up; it resets at 00:00 UTC.")
    ds = decisions(con)
    listing, ids = _listing(ds)
    kept = items(con)
    confirmed = [i["text"] for i in kept if i["verdict"] == "ok"]
    wrong = [i["text"] for i in kept if i["verdict"] == "wrong"]
    from . import vault
    titles = {k: fm.get("title", k) for k, fm in vault.concepts(cfg["vault"]).items()}
    prompt = (f"{_stats_text(stats(con), titles)}\n\nEXISTING SEARCH TERMS: {', '.join(_existing_terms(cfg)) or '(none)'}\n\n"
              f"CONFIRMED BY THE USER:\n{chr(10).join('- ' + t for t in confirmed) or '(none yet)'}\n\n"
              f"REJECTED BY THE USER (wrong):\n{chr(10).join('- ' + t for t in wrong) or '(none yet)'}\n{listing}")
    llm = cfg.get("llm") or {}
    model = llm.get("profile_model") or llm.get("ask_model") or llm["summary_model"]
    ref = f"profile:{kv_get(con, 'profile_runs', 0)}"
    try:
        out = claude(cfg, prompt, system=SYSTEM, model=model, schema=SCHEMA, purpose="profile", ref=ref)
    except BudgetExceeded as e:
        raise ProfileError(f"Over the overall budget: {e}")
    new, dropped = validate(out.get("items") or [], ids)
    new += suggest_pauses(cfg, con)
    blocked = {_norm(i["text"]) for i in kept if i["verdict"] != "pending" or i["source"] == "user"}
    con.execute("DELETE FROM profile_items WHERE source='auto' AND verdict='pending'")
    added = 0
    for it in new:
        if _norm(it["text"]) in blocked:
            continue
        con.execute("INSERT INTO profile_items(kind, text, evidence) VALUES(?,?,?)", (it["kind"], it["text"], json.dumps(it["evidence"])))
        added += 1
    kv_set(con, "profile_built_at", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    kv_set(con, "profile_decisions", st["accepted"] + st["refused"])
    kv_set(con, "profile_runs", int(kv_get(con, "profile_runs", 0) or 0) + 1)
    con.commit()
    cost = float(con.execute("SELECT COALESCE(SUM(usd),0) FROM spend WHERE ref=?", (ref,)).fetchone()[0])
    log.info("profile: %d items (%d dropped as unsupported), $%.3f", added, dropped, cost)
    return {"items": added, "dropped": dropped, "cost": cost}


def refresh_if_stale(cfg) -> None:
    """For the daily job: rebuild when enough new decisions have piled up. Never raises."""
    try:
        if stale():
            build(cfg)
    except ProfileError as e:
        log.info("profile not refreshed: %s", e)
    except Exception:
        log.exception("profile refresh failed")


def start_build(cfg) -> bool:
    """Rebuild in the background for the web app; False if one is already running."""
    if JOB["running"]:
        return False
    JOB.update(running=True, error="")

    def run():
        try:
            JOB["result"] = build(cfg)
        except ProfileError as e:
            JOB["error"] = str(e)
        except Exception as e:
            log.exception("profile build failed")
            JOB["error"] = f"Something went wrong: {e}"[:300]
        finally:
            JOB["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True
