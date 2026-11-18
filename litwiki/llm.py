"""Headless Claude Code calls with a spend ledger and budget guards."""
import json
import subprocess
from datetime import date

from .common import STATE_DIR, db, log
from .notify import notify


class BudgetExceeded(Exception):
    pass


# Which setting picks the model for each kind of call: llm.<family>_model, falling back along this chain, then summary_model.
FALLBACK = {"backfill": "summary", "manual": "summary", "bootstrap": "summary", "profile": "ask", "chat": "triage", "screen": "triage"}


def pick_model(cfg: dict, family: str) -> str:
    """The model for a kind of call (summary, backfill, manual, ask, triage, chat, profile, bootstrap, screen)."""
    llm, seen = cfg.get("llm") or {}, set()
    while family and family not in seen:
        seen.add(family)
        if llm.get(f"{family}_model"):
            return llm[f"{family}_model"]
        family = FALLBACK.get(family)
    return llm["summary_model"]


def spent(con, period: str) -> float:
    """period: 'month' or 'day' (UTC, matching sqlite datetime('now'))."""
    fmt = "%Y-%m" if period == "month" else "%Y-%m-%d"
    r = con.execute(
        f"SELECT COALESCE(SUM(usd),0) s FROM spend WHERE strftime('{fmt}', ts) = strftime('{fmt}', 'now')"
    ).fetchone()
    return float(r["s"])


def spent_purpose_today(con, prefix: str) -> float:
    r = con.execute(
        "SELECT COALESCE(SUM(usd),0) s FROM spend WHERE date(ts)=date('now') AND purpose LIKE ?",
        (prefix + "%",),
    ).fetchone()
    return float(r["s"])


def check_budget(cfg: dict) -> None:
    con = db()
    b = cfg["budget"]
    month, day = spent(con, "month"), spent(con, "day")
    ym, today = date.today().strftime("%Y-%m"), date.today().isoformat()
    if month >= b["monthly_usd"]:
        notify(f"Monthly LLM budget exhausted (${month:.2f} / ${b['monthly_usd']:.0f}). "
               "All LLM calls are paused until next month.", once_key=f"month-stop-{ym}", level="error")
        raise BudgetExceeded(f"monthly budget reached: ${month:.2f}")
    if month >= b["monthly_usd"] * b["warn_fraction"]:
        notify(f"Monthly LLM spend at ${month:.2f} of ${b['monthly_usd']:.0f}.",
               once_key=f"month-warn-{ym}", level="warn")
    if day >= b["daily_usd"]:
        notify(f"Daily LLM spend hit ${day:.2f} (cap ${b['daily_usd']:.2f}); this is unusual. "
               "LLM calls paused until tomorrow (UTC). Check `litwiki status` / logs.",
               once_key=f"day-stop-{today}", level="error")
        raise BudgetExceeded(f"daily budget reached: ${day:.2f}")


def claude(cfg: dict, prompt: str, *, system: str, model: str, schema: dict,
           purpose: str, ref: str = "", timeout: int = 600) -> dict:
    """Run `claude -p` with no tools and a JSON schema; return structured output."""
    check_budget(cfg)
    cap = cfg["llm"]["per_call_budget_usd"]
    cmd = [
        "claude", "-p", "--model", model, "--tools", "", "--no-session-persistence",
        "--output-format", "json", "--system-prompt", system,
        "--json-schema", json.dumps(schema), "--max-budget-usd", str(cap),
    ]
    workdir = STATE_DIR / "claude-cwd"  # empty dir: no CLAUDE.md / project settings picked up
    workdir.mkdir(exist_ok=True)
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          timeout=timeout, cwd=workdir)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude returned non-JSON (rc={proc.returncode}): "
                           f"{proc.stdout[-500:]} {proc.stderr[-500:]}")
    usd = float(out.get("total_cost_usd") or 0)
    con = db()
    con.execute("INSERT INTO spend(purpose, model, usd, ref) VALUES(?,?,?,?)", (purpose, model, usd, ref))
    con.commit()
    log.info("claude %s %s %s: $%.4f", purpose, model, ref, usd)
    if usd > cap * 0.9:
        notify(f"Single LLM call cost ${usd:.2f} ({purpose} {ref}); near the per-call cap.", level="warn")
    if out.get("is_error") or out.get("structured_output") is None:
        raise RuntimeError(f"claude error ({out.get('subtype')}): {str(out.get('result'))[:500]}")
    return out["structured_output"]
