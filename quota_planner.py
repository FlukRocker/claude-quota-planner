#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.0.0"]
# ///
"""
quota-planner — an MCP server that plans work against your Claude session quota.

Two ceilings exist, and which one binds depends on how you authenticate:

  QUOTA MODE (Pro/Max subscribers) — Claude Code passes `rate_limits.five_hour`,
  `.seven_day`, and behind a Claude apps gateway `.spend_limit` to the statusLine
  command on stdin. Work is planned as a percentage of the 5-hour window.

  SPEND MODE (API key, Bedrock, Vertex) — `rate_limits` is never sent, because no
  server-side window exists; you are billed per token. The ceiling is the daily and
  monthly USD budget you set, and work is planned in dollars against it.

Either way the companion statusline-quota.py writes the readings to
~/.claude/quota/{snapshot.json,history.jsonl,spend.json}; this server reads them,
derives a burn rate, and packs tasks into what is left.

No network calls, no credentials, no undocumented endpoints.
"""

from __future__ import annotations

import calendar
import functools
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Literal

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
from mcp.types import ToolAnnotations

QDIR = Path(os.environ.get("CLAUDE_QUOTA_DIR", Path.home() / ".claude" / "quota"))
SNAPSHOT = QDIR / "snapshot.json"
HISTORY = QDIR / "history.jsonl"
COSTS = QDIR / "costs.json"
BUDGET = QDIR / "budget.json"
SPEND = QDIR / "spend.json"

# The one endpoint this project talks to, and only from reconcile_spend — every
# other tool stays offline. Not configurable: an admin credential is powerful
# enough that the destination should not be settable by an environment variable.
ADMIN_BASE = "https://api.anthropic.com"
COST_REPORT = f"{ADMIN_BASE}/v1/organizations/cost_report"
USER_AGENT = ("claude-quota-planner/0.3.0 "
              "(+https://github.com/FlukRocker/claude-quota-planner)")

Category = Literal["micro", "small", "medium", "large"]
ModelClass = Literal["opus", "sonnet", "haiku"]
Mode = Literal["auto", "quota", "spend"]

# Seed estimates: percent of ONE 5-hour window consumed by one task, and rough
# wall-clock minutes. Anthropic publishes no token-to-quota mapping, so these
# are deliberately conservative starting points — record_task_cost() replaces
# them with your own measured numbers as you go.
SEED_PCT: dict[str, dict[str, float]] = {
    "opus":   {"micro": 1.5, "small": 4.0, "medium": 10.0, "large": 22.0},
    "sonnet": {"micro": 0.5, "small": 1.5, "medium": 4.0,  "large": 9.0},
    "haiku":  {"micro": 0.2, "small": 0.6, "medium": 1.5,  "large": 3.5},
}
# Spend-mode seeds in USD. Ratios track list pricing per million tokens
# (Opus 5 $5/$25, Sonnet 5 $2/$10, Haiku 4.5 $1/$5); the absolute numbers are
# guesses about task shape and are meant to be replaced by record_task_cost.
SEED_USD: dict[str, dict[str, float]] = {
    "opus":   {"micro": 0.30, "small": 1.20, "medium": 3.00, "large": 7.00},
    "sonnet": {"micro": 0.12, "small": 0.50, "medium": 1.20, "large": 2.80},
    "haiku":  {"micro": 0.05, "small": 0.20, "medium": 0.50, "large": 1.20},
}
SEED_MIN: dict[str, float] = {"micro": 3, "small": 12, "medium": 35, "large": 80}

CATEGORY_HELP = (
    "micro = one-file edit, lookup, quick question | "
    "small = contained bug fix or small feature | "
    "medium = multi-file feature, refactor, test suite | "
    "large = migration, architecture change, long research/agent run"
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

server = _Server("quota-planner", version="0.3.0")


# --------------------------------------------------------------------------- #
# storage helpers
# --------------------------------------------------------------------------- #
def _load_snapshot() -> dict[str, Any] | None:
    try:
        return json.loads(SNAPSHOT.read_text())
    except Exception:
        return None


def _load_history(limit: int = 400) -> list[dict[str, Any]]:
    try:
        lines = HISTORY.read_text().splitlines()[-limit:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _load_costs() -> dict[str, dict[str, float]]:
    try:
        return json.loads(COSTS.read_text())
    except Exception:
        return {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    QDIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _budget_env() -> dict[str, float | None]:
    """Env overrides, which win over budget.json — same rule the status line uses."""
    out: dict[str, float | None] = {}
    for key, env in (("daily_usd", "QUOTA_BUDGET_USD_DAILY"),
                     ("monthly_usd", "QUOTA_BUDGET_USD_MONTHLY")):
        try:
            v = float(os.environ[env])
        except (KeyError, ValueError):
            v = None
        out[key] = v if v and v > 0 else None
    return out


def _load_budget() -> dict[str, Any]:
    try:
        cfg = json.loads(BUDGET.read_text())
    except Exception:
        cfg = {}
    env = _budget_env()
    out: dict[str, Any] = {"source": {}}
    for key in ("daily_usd", "monthly_usd"):
        if env[key] is not None:
            out[key], out["source"][key] = env[key], "env"
            continue
        try:
            v = float(cfg.get(key))
        except (TypeError, ValueError):
            v = None
        out[key] = v if v and v > 0 else None
        out["source"][key] = "budget.json" if out[key] else None
    return out


def _load_spend() -> dict[str, Any]:
    try:
        return json.loads(SPEND.read_text())
    except Exception:
        return {}


def _spend_source() -> str:
    """'local' (client-side estimate) or 'cost_report' (billed, via Admin API)."""
    env = os.environ.get("QUOTA_SPEND_SOURCE")
    if env in ("local", "cost_report"):
        return env
    try:
        cfg = json.loads(BUDGET.read_text())
    except Exception:
        cfg = {}
    return "cost_report" if cfg.get("spend_source") == "cost_report" else "local"


def _admin_credential() -> tuple[str, str]:
    """Only variables set deliberately for this count.

    ANTHROPIC_API_KEY is skipped on purpose: it is usually the regular or
    workspace-scoped key, which the Admin API rejects anyway, and quietly
    reaching for whatever key happens to be exported is not a thing a planning
    tool should do.
    """
    key = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if key:
        return "x-api-key", key
    token = os.environ.get("ANTHROPIC_ADMIN_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if token:
        return "authorization", f"Bearer {token}"
    raise RuntimeError(
        "No admin credential. Billed spend comes from the Admin API, which needs an "
        "Admin API key (sk-ant-admin01-…) in ANTHROPIC_ADMIN_KEY, or an org:admin OAuth "
        "token in ANTHROPIC_ADMIN_OAUTH_TOKEN / ANTHROPIC_AUTH_TOKEN. Workspace-scoped "
        "keys are rejected by the endpoint, and the Admin API is unavailable for "
        "individual (non-organization) accounts."
    )


def _utc_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _cost_report(starting_at: str, limit: int = 31) -> list[dict[str, Any]]:
    """GET /v1/organizations/cost_report, following pagination. Buckets are UTC days."""
    header, value = _admin_credential()
    buckets: list[dict[str, Any]] = []
    page = None
    for _ in range(5):  # a month of daily buckets fits in one page; this is a guard
        params = {"starting_at": starting_at, "bucket_width": "1d", "limit": str(limit)}
        if page:
            params["page"] = page
        req = urllib.request.Request(
            f"{COST_REPORT}?{urllib.parse.urlencode(params)}",
            headers={header: value, "anthropic-version": "2023-06-01",
                     "User-Agent": USER_AGENT, "accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            hint = {
                401: " — credential rejected. Admin API keys and org:admin OAuth tokens "
                     "only; regular and workspace-scoped keys do not work here.",
                403: " — credential lacks admin scope, or this is an individual account: "
                     "the Admin API is unavailable outside an organization.",
                404: " — endpoint not found for this account. Claude Enterprise "
                     "organizations use the Analytics API instead, and the endpoint is "
                     "unavailable on Claude Platform on AWS.",
            }.get(exc.code, "")
            raise RuntimeError(f"Admin API returned {exc.code}{hint} {detail}".strip())
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach {ADMIN_BASE}: {exc.reason}")
        buckets.extend(body.get("data") or [])
        if not body.get("has_more"):
            break
        page = body.get("next_page")
        if not page:
            break
    return buckets


def _month_start_utc(now: float) -> str:
    g = time.gmtime(now)
    return f"{g.tm_year:04d}-{g.tm_mon:02d}-01T00:00:00Z"


def _billed_totals(buckets: list[dict[str, Any]]) -> dict[str, float]:
    """UTC day -> dollars. Amounts arrive as decimal strings in cents."""
    days: dict[str, float] = {}
    for b in buckets:
        start = b.get("starting_at") or ""
        day = start[:10]
        if not day:
            continue
        total = 0.0
        for item in b.get("results") or []:
            if (item.get("currency") or "USD") != "USD":
                # Documented as always USD today; if that ever changes, refusing
                # beats silently adding two currencies together.
                raise RuntimeError(f"Unexpected currency {item.get('currency')!r} in cost report.")
            try:
                total += float(item.get("amount") or 0) / 100.0
            except (TypeError, ValueError):
                continue
        days[day] = round(total, 6)
    return days


# --------------------------------------------------------------------------- #
# units
# --------------------------------------------------------------------------- #
def _estimate(category: str, model: str, unit: str) -> tuple[float, float, str]:
    """Return (cost_in_unit, minutes, source) for a task.

    Percent-of-window and dollars are stored under separate keys: a calibration
    measured on a subscription says nothing about an API-key bill, and averaging
    the two would produce a number in no unit at all.
    """
    key = f"{model}:{category}" if unit == "pct" else f"usd:{model}:{category}"
    seed = (SEED_PCT if unit == "pct" else SEED_USD)[model][category]
    field = "pct" if unit == "pct" else "usd"
    learned = _load_costs().get(key)
    if learned and learned.get("n", 0) >= 2 and learned.get(field) is not None:
        return (
            float(learned[field]),
            float(learned.get("minutes") or SEED_MIN[category]),
            f"calibrated (n={int(learned['n'])})",
        )
    return seed, SEED_MIN[category], "seed estimate"


def _amt(v: float | None, unit: str) -> str:
    if v is None:
        return "—"
    if unit == "pct":
        return f"{v:.0f}%"
    return f"${v:,.0f}" if v >= 1000 else f"${v:,.2f}"


def _fmt_dur(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h >= 48:
        return f"{h // 24}d {h % 24}h"
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _day_end(now: float) -> float:
    lt = time.localtime(now)
    return now + (86400 - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec))


def _month_end(now: float) -> float:
    lt = time.localtime(now)
    y, m = (lt.tm_year + 1, 1) if lt.tm_mon == 12 else (lt.tm_year, lt.tm_mon + 1)
    return time.mktime((y, m, 1, 0, 0, 0, 0, 1, -1))


def _day_end_utc(now: float) -> float:
    g = time.gmtime(now)
    return now + (86400 - (g.tm_hour * 3600 + g.tm_min * 60 + g.tm_sec))


def _month_end_utc(now: float) -> float:
    """Billed buckets are UTC days, so their month rolls over on UTC time too."""
    g = time.gmtime(now)
    y, m = (g.tm_year + 1, 1) if g.tm_mon == 12 else (g.tm_year, g.tm_mon + 1)
    return calendar.timegm((y, m, 1, 0, 0, 0, 0, 1, 0))


# --------------------------------------------------------------------------- #
# derived metrics
# --------------------------------------------------------------------------- #
def _burn_quota(snap: dict[str, Any]) -> tuple[float | None, int]:
    """Percent of the 5-hour window consumed per hour, within the CURRENT window."""
    resets_at = (snap.get("five_hour") or {}).get("resets_at") or 0
    pts = [
        h for h in _load_history()
        if (h.get("five_hour") or {}).get("resets_at") == resets_at and h.get("ts")
    ]
    return _rate(pts, lambda h: h["five_hour"]["used_percentage"])


def _burn_spend(snap: dict[str, Any]) -> tuple[float | None, int]:
    """Dollars per hour spent TODAY, across every session on this machine."""
    day = (snap.get("spend") or {}).get("day")
    pts = [
        h for h in _load_history()
        if (h.get("spend") or {}).get("day") == day and h.get("ts")
    ]
    return _rate(pts, lambda h: float(h["spend"]["daily_usd"]))


def _rate(pts: list[dict[str, Any]], value) -> tuple[float | None, int]:
    if len(pts) < 2:
        return None, len(pts)
    first, last = pts[0], pts[-1]
    hours = (last["ts"] - first["ts"]) / 3600.0
    if hours < 0.02:  # < ~1 minute of signal
        # Deliberately negative: too little TIME, which is a different problem
        # from too few samples and has a different fix (wait, don't send more).
        return None, -len(pts)
    try:
        delta = value(last) - value(first)
    except Exception:
        return None, len(pts)
    return max(delta / hours, 0.0), len(pts)


def _setup_error(detail: str) -> RuntimeError:
    return RuntimeError(
        f"{detail} The statusLine writer feeds this server. Fix: install the status "
        "line (plugin: run /quota-planner:quota-setup; manual: save statusline-quota.py "
        'to ~/.claude/quota/, chmod +x it, point "statusLine".command at it in '
        "~/.claude/settings.json), then send one message in Claude Code so the first "
        "API response populates it."
    )


def _state(mode: str = "auto") -> dict[str, Any]:
    """Everything the tools need, in whichever unit binds, or a 'not set up' error."""
    snap = _load_snapshot()
    if not snap:
        raise _setup_error(f"No quota snapshot at {SNAPSHOT}.")

    now = time.time()
    resolved = mode if mode in ("quota", "spend") else (snap.get("mode") or "quota")
    if resolved == "quota" and (snap.get("five_hour") or {}).get("used_percentage") is None:
        raise _setup_error(
            "The snapshot carries no rate_limits, so quota mode has nothing to read. "
            "rate_limits reaches Pro/Max subscriber sessions only — on API-key, Bedrock "
            "or Vertex auth use mode='spend' and set a USD budget with set_budget."
        )

    common = {
        "mode": resolved,
        "snapshot": snap,
        "age_seconds": now - snap.get("ts", now),
        "model": snap.get("model", "?"),
        "spend_block": snap.get("spend") or {},
    }

    if resolved == "spend":
        spend = snap.get("spend") or {}
        budget = _load_budget()
        daily = budget["daily_usd"]
        monthly = budget["monthly_usd"]
        source = _spend_source()
        billed = _load_spend().get("billed") or {}
        bdays = billed.get("days") or {}
        today_utc = _utc_day(now)
        use_billed = source == "cost_report" and today_utc in bdays

        if use_billed:
            # Billed buckets are UTC days, so the whole denominator moves to UTC:
            # mixing a UTC day's dollars with a local day's reset would compare
            # two different windows.
            used = float(bdays[today_utc])
            month = today_utc[:7]
            secondary_used = sum(float(v) for d, v in bdays.items() if d[:7] == month)
            secs_left = max(_day_end_utc(now) - now, 0)
            secondary_reset = max(_month_end_utc(now) - now, 0)
        else:
            used = float(spend.get("daily_usd") or 0)
            secondary_used = float(spend.get("monthly_usd") or 0)
            secs_left = max(_day_end(now) - now, 0)
            secondary_reset = max(_month_end(now) - now, 0)

        # The rate always comes from the local series: billed data is daily, and a
        # daily bucket cannot produce an hourly rate.
        burn, points = _burn_spend(snap)
        remaining = max(daily - used, 0.0) if daily else None
        runway_h = (remaining / burn) if (burn and remaining is not None) else None
        return {
            **common,
            "unit": "usd",
            "window_label": "Today (UTC, billed)" if use_billed else "Today",
            "secondary_label": "This month (UTC, billed)" if use_billed else "This month",
            "spend_source": source,
            "billed_in_use": use_billed,
            "billed_missing_today": source == "cost_report" and not use_billed,
            "billed_age_seconds": (now - float(billed["fetched_at"]))
                                  if billed.get("fetched_at") else None,
            "limit": daily,
            "used": used,
            "remaining": remaining,
            "secondary_used": secondary_used,
            "secondary_limit": monthly,
            "secondary_seconds_to_reset": secondary_reset,
            "budget_source": budget["source"],
            "seconds_to_reset": secs_left,
            "resets_at": (_day_end_utc(now) if use_billed else _day_end(now)),
            "burn_per_hour": burn,
            "burn_points": points,
            "runway_hours": runway_h,
            "will_exhaust": bool(runway_h is not None and runway_h * 3600 < secs_left),
            "spend_limit": None,
        }

    five, seven = snap["five_hour"], snap.get("seven_day", {})
    resets_at = five.get("resets_at") or 0
    burn, points = _burn_quota(snap)
    used = float(five["used_percentage"])
    remaining = max(100.0 - used, 0.0)
    secs_left = max(resets_at - now, 0) if resets_at else 0
    runway_h = (remaining / burn) if burn else None
    slim = snap.get("spend_limit") or None
    if slim:
        slim = {
            "used_percentage": float(slim.get("used_percentage") or 0),
            "resets_at": slim.get("resets_at") or 0,
            "seconds_to_reset": max((slim.get("resets_at") or 0) - now, 0),
        }
    return {
        **common,
        "unit": "pct",
        "window_label": "5-hour window",
        "secondary_label": "Weekly (all models)",
        "limit": 100.0,
        "used": used,
        "remaining": remaining,
        "secondary_used": float(seven.get("used_percentage") or 0),
        "secondary_limit": 100.0,
        "secondary_seconds_to_reset": max((seven.get("resets_at") or 0) - now, 0),
        "budget_source": {},
        "seconds_to_reset": secs_left,
        "resets_at": resets_at,
        "burn_per_hour": burn,
        "burn_points": points,
        "runway_hours": runway_h,
        # True when you will run dry before the window resets.
        "will_exhaust": bool(runway_h is not None and secs_left and runway_h * 3600 < secs_left),
        "spend_limit": slim,
        "windows_age_seconds": now - (snap.get("windows_ts") or snap.get("ts", now)),
    }


def _unit_out(s: dict[str, Any], value: float | None) -> dict[str, Any]:
    """Emit a number under the key matching its unit, so callers can't mix them up."""
    key = "pct" if s["unit"] == "pct" else "usd"
    return {key: None if value is None else round(value, 2)}


def _burn_line(s: dict[str, Any]) -> str:
    unit = s["unit"]
    per = "%/hour" if unit == "pct" else "/hour"
    if s["burn_per_hour"] is None:
        pts = s["burn_points"]
        if pts < 0:
            return (f"**Burn rate:** {-pts} samples in this window but under a minute "
                    "apart — a rate off that span would be noise. It appears once they "
                    "spread out.")
        return f"**Burn rate:** not enough samples yet ({pts} in this window)"
    rate = (f"{s['burn_per_hour']:.1f}{per}" if unit == "pct"
            else f"${s['burn_per_hour']:,.2f}{per}")
    if s["runway_hours"] is None:
        return f"**Burn rate:** {rate} (no budget set, so no runway to compute)"
    return (f"**Burn rate:** {rate} → ~{_fmt_dur(s['runway_hours'] * 3600)} "
            "of runway left")


def _spend_limit_lines(s: dict[str, Any]) -> list[str]:
    """The gateway spend cap is a separate denominator — report it, never mix it in."""
    slim = s.get("spend_limit")
    if not slim:
        return []
    pct = slim["used_percentage"]
    line = f"**Gateway spend limit:** {pct:.0f}% used"
    if slim["seconds_to_reset"]:
        line += f" — resets in {_fmt_dur(slim['seconds_to_reset'])}"
    out = [line]
    if pct >= 100:
        out.append("⛔ Spend limit reached. Nothing runs until it resets, whatever the "
                   "5-hour window says.")
    elif pct >= 80:
        out.append("⚠️ Spend limit above 80% — it, not the 5-hour window, is the binding "
                   "constraint.")
    return out


def _guard(fn):
    """Return validation and setup failures as readable text.

    An exception raised inside a tool reaches the caller as a bare
    "Error executing tool X" — the message, which is the entire point of these
    errors, is dropped. Returning it keeps the fix visible to whoever called.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, RuntimeError) as exc:
            return {"error": str(exc), "summary": f"⛔ {exc}"}
    return wrapper


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@server.tool(annotations=READ_ONLY)
@_guard
def quota_status(mode: Mode = "auto") -> dict[str, Any]:
    """Report the ceiling this session is under: on a subscription, the 5-hour window
    used/remaining, weekly usage, gateway spend limit, time until reset and measured
    burn rate; on API-key/Bedrock/Vertex auth, today's and this month's USD spend
    against your budget. Call before starting any long or expensive piece of work.
    mode: 'auto' follows the latest session, 'quota' or 'spend' forces one."""
    s = _state(mode)
    unit = s["unit"]
    lines = [
        f"**{s['window_label']}:** {_amt(s['used'], unit)} used"
        + (f", {_amt(s['remaining'], unit)} left" if s["remaining"] is not None
           else " (no budget set)")
        + (f" of {_amt(s['limit'], unit)}" if unit == "usd" and s["limit"] else "")
        + (f" — resets in {_fmt_dur(s['seconds_to_reset'])}" if s["seconds_to_reset"] else ""),
        f"**{s['secondary_label']}:** {_amt(s['secondary_used'], unit)} used"
        + (f" of {_amt(s['secondary_limit'], unit)}"
           if unit == "usd" and s["secondary_limit"] else ""),
        f"**Model:** {s['model']}",
        _burn_line(s),
    ]
    if s["will_exhaust"]:
        lines.append(
            "⚠️ At this pace you run dry **before** the window resets. "
            "Downgrade the model, shrink scope, or park the heavy task."
        )
    lines += _spend_limit_lines(s)

    if unit == "pct":
        if s["secondary_used"] >= 80:
            lines.append("⚠️ Weekly cap above 80% — the week, not the session, is the "
                         "binding constraint.")
        today = (s["spend_block"] or {}).get("daily_usd")
        if today:
            lines.append(f"_Spend today (list-price estimate): ${float(today):,.2f}._")
    else:
        if s["limit"] is None:
            lines.append("_No daily budget set — call set_budget(daily_usd=…) to turn "
                         "these numbers into go/no-go answers._")
        elif s["secondary_limit"] and s["secondary_used"] >= s["secondary_limit"] * 0.8:
            lines.append("⚠️ Monthly budget above 80% — deferring to tomorrow may not "
                         "help.")
        if s.get("billed_in_use"):
            age = s.get("billed_age_seconds")
            lines.append("_Figures are billed amounts from the Admin API cost report, "
                         "over **UTC** days"
                         + (f", pulled {_fmt_dur(age)} ago" if age else "") + "._")
            if age and age > 6 * 3600:
                lines.append("⚠️ Billed figures are stale — call reconcile_spend to "
                             "refresh them.")
        elif s.get("billed_missing_today"):
            lines.append("⚠️ spend_source is cost_report but no billed figure exists for "
                         "today (UTC) — showing the local estimate. Call reconcile_spend.")
        else:
            lines.append("_Spend is Claude Code's client-side estimate at list price, not "
                         "your invoice._")

    if s["age_seconds"] > 1800:
        lines.append(f"_Snapshot is {_fmt_dur(s['age_seconds'])} old — send a message "
                     "to refresh it._")

    out = {
        "summary": "\n".join(lines),
        "mode": s["mode"],
        "unit": unit,
        "seconds_to_reset": int(s["seconds_to_reset"]),
        "burn_per_hour": round(s["burn_per_hour"], 2) if s["burn_per_hour"] else None,
        "will_exhaust_before_reset": s["will_exhaust"],
        "snapshot_age_seconds": int(s["age_seconds"]),
    }
    if unit == "pct":
        out.update({
            "used_pct": round(s["used"], 1),
            "remaining_pct": round(s["remaining"], 1),
            "weekly_pct": round(s["secondary_used"], 1),
            "burn_pct_per_hour": out["burn_per_hour"],
            "spend_limit_pct": (round(s["spend_limit"]["used_percentage"], 1)
                                if s["spend_limit"] else None),
        })
    else:
        out.update({
            "daily_usd": round(s["used"], 2),
            "daily_budget_usd": s["limit"],
            "remaining_usd": None if s["remaining"] is None else round(s["remaining"], 2),
            "monthly_usd": round(s["secondary_used"], 2),
            "monthly_budget_usd": s["secondary_limit"],
            "burn_usd_per_hour": out["burn_per_hour"],
            "spend_source": s.get("spend_source", "local"),
            # Billed totals are invoiced dollars; the burn rate never is.
            "estimated": not s.get("billed_in_use"),
            "burn_estimated": True,
        })
    return out


@server.tool(annotations=READ_ONLY)
@_guard
def plan_session(
    tasks: list[dict],
    buffer_pct: float = 10.0,
    model_class: ModelClass = "opus",
    mode: Mode = "auto",
) -> dict[str, Any]:
    """Pack a task list into the budget and time left before the next reset — the
    5-hour window on a subscription, today's USD budget on API-key/Bedrock/Vertex auth.

    tasks: list of objects with:
      - name (str, required)
      - category (str, required): micro = one-file edit or quick question;
        small = contained bug fix; medium = multi-file feature or refactor;
        large = migration, architecture change, or long agent run
      - priority (int 1-5, default 3): higher runs first
      - must_do (bool, default false): scheduled even if it overruns the buffer
      - model_class (str, optional): per-task override of the default
    buffer_pct: share of what's left to hold back for unplanned work (default 10).
    model_class: default model tier the tasks will run on.
    mode: 'auto' follows the latest session, 'quota' or 'spend' forces one.

    Returns an ordered plan: what fits now, what to defer past the reset, and
    where switching to a cheaper model would rescue a deferred task.
    """
    s = _state(mode)
    unit = s["unit"]
    if s["remaining"] is None:
        raise ValueError(
            "Spend mode with no daily budget: there is nothing to pack tasks into. "
            "Call set_budget(daily_usd=…) first, or pass explicit budgets via "
            "QUOTA_BUDGET_USD_DAILY."
        )
    budget = max(s["remaining"] * (1 - buffer_pct / 100.0), 0.0) if unit == "usd" \
        else max(s["remaining"] - buffer_pct, 0.0)
    minutes_left = s["seconds_to_reset"] / 60 if s["seconds_to_reset"] else 5 * 60

    enriched = []
    for i, t in enumerate(tasks):
        name = t.get("name") or f"task {i + 1}"
        cat = t.get("category", "small")
        if cat not in SEED_MIN:
            raise ValueError(f"'{name}': category must be micro/small/medium/large. {CATEGORY_HELP}")
        mc = t.get("model_class", model_class)
        cost, mins, src = _estimate(cat, mc, unit)
        enriched.append({
            "name": name, "category": cat, "model_class": mc,
            "priority": int(t.get("priority", 3)), "must_do": bool(t.get("must_do", False)),
            "est_cost": cost, "unit": unit, "est_minutes": mins, "estimate_source": src,
            **{f"est_{k}": v for k, v in _unit_out(s, cost).items()},
        })

    # must-do first, then priority, then cheapest first so more tasks fit.
    enriched.sort(key=lambda t: (not t["must_do"], -t["priority"], t["est_cost"]))

    now_list, defer_list = [], []
    spent = spent_min = 0.0
    for t in enriched:
        fits_budget = spent + t["est_cost"] <= budget
        fits_time = spent_min + t["est_minutes"] <= minutes_left
        if (fits_budget and fits_time) or t["must_do"]:
            spent += t["est_cost"]
            spent_min += t["est_minutes"]
            t["cumulative"] = _amt(spent, unit)
            if t["must_do"] and not (fits_budget and fits_time):
                t["note"] = "forced in as must_do — expect to overrun the buffer"
            now_list.append(t)
        else:
            t["blocked_by"] = "budget" if not fits_budget else "time to reset"
            cheap, _, _ = _estimate(t["category"], "sonnet", unit)
            if not fits_budget and spent + cheap <= budget and t["model_class"] != "sonnet":
                t["rescue"] = (f"would fit on Sonnet (~{_amt(cheap, unit)} vs "
                               f"{_amt(t['est_cost'], unit)})")
            defer_list.append(t)

    head = "Est. quota" if unit == "pct" else "Est. cost"
    rows = [
        f"| # | Task | Cat | Model | {head} | Est. time | Cumulative |",
        "|---|------|-----|-------|-----------|-----------|------------|",
    ]
    for i, t in enumerate(now_list, 1):
        rows.append(
            f"| {i} | {t['name']} | {t['category']} | {t['model_class']} | "
            f"{_amt(t['est_cost'], unit)} | {t['est_minutes']:.0f}m | {t['cumulative']} |"
        )
    if len(rows) == 2:
        rows.append("| — | _nothing fits in the remaining budget_ | | | | | |")

    out = [
        f"**Budget:** {_amt(s['remaining'], unit)} left − {buffer_pct:.0f}% buffer "
        f"= **{_amt(budget, unit)} usable**, {_fmt_dur(minutes_left * 60)} until reset.",
        "",
        "**Run this window**",
        *rows,
    ]
    if defer_list:
        out += ["", "**Defer past the reset**"]
        for t in defer_list:
            extra = f" — {t['rescue']}" if "rescue" in t else ""
            out.append(
                f"- {t['name']} ({t['category']}, ~{_amt(t['est_cost'], unit)}) "
                f"— blocked by {t['blocked_by']}{extra}"
            )
    if s["burn_per_hour"] and spent_min > 0:
        implied = spent / max(spent_min / 60, 0.01)
        if implied < s["burn_per_hour"] * 0.6:
            out += ["", f"⚠️ This plan assumes {_amt(implied, unit)}/hour but your measured "
                        f"burn is {_amt(s['burn_per_hour'], unit)}/hour. Expect to finish "
                        f"roughly item {max(1, int(len(now_list) * implied / s['burn_per_hour']))} "
                        f"of {len(now_list)} before the limit bites — calibrate with "
                        "record_task_cost."]
    if unit == "pct" and s["secondary_used"] >= 80:
        out += ["", f"⚠️ Weekly cap at {s['secondary_used']:.0f}% — deferring to the next "
                    "5-hour window may not help. Check the 7-day reset before planning "
                    "around it."]
    if unit == "usd" and s["secondary_limit"] and \
            s["secondary_used"] >= s["secondary_limit"] * 0.8:
        out += ["", f"⚠️ Monthly budget at {_amt(s['secondary_used'], unit)} of "
                    f"{_amt(s['secondary_limit'], unit)} — deferring to tomorrow may not help."]
    out += _spend_limit_lines(s)
    out += ["", "_Estimates are seeds until calibrated. Call record_task_cost after "
                "finishing a task to replace them with your real numbers._"]

    return {
        "summary": "\n".join(out),
        "mode": s["mode"],
        "unit": unit,
        "usable_budget": round(budget, 2),
        "minutes_to_reset": int(minutes_left),
        "scheduled": now_list,
        "deferred": defer_list,
        "projected_end": round(s["used"] + spent, 2),
    }


@server.tool(annotations=READ_ONLY)
@_guard
def check_budget(
    category: Category,
    model_class: ModelClass = "opus",
    buffer_pct: float = 10.0,
    mode: Mode = "auto",
) -> dict[str, Any]:
    """Go / no-go check for a single task before starting it. Answers in percent of the
    5-hour window on a subscription, or in dollars against today's budget on
    API-key/Bedrock/Vertex auth. Returns a cheaper alternative when it doesn't fit."""
    s = _state(mode)
    unit = s["unit"]
    cost, mins, src = _estimate(category, model_class, unit)
    minutes_left = s["seconds_to_reset"] / 60 if s["seconds_to_reset"] else 300

    if s["remaining"] is None:
        # Spend mode with no budget: there is no ceiling to check against, so
        # report the cost instead of inventing a verdict.
        lines = [
            f"**NO BUDGET SET** — {category} on {model_class} costs about "
            f"{_amt(cost, unit)} ({src}), ~{mins:.0f} min.",
            f"Spent today: {_amt(s['used'], unit)}; this month: "
            f"{_amt(s['secondary_used'], unit)}.",
            "→ Call set_budget(daily_usd=…) to get a real go/no-go.",
        ]
        return {
            "summary": "\n".join(lines), "mode": s["mode"], "unit": unit,
            "fits": None, "est_usd": round(cost, 2), "est_minutes": mins,
            "estimate_source": src, "alternatives": [],
        }

    budget = max(s["remaining"] * (1 - buffer_pct / 100.0), 0.0) if unit == "usd" \
        else max(s["remaining"] - buffer_pct, 0.0)
    fits = cost <= budget and mins <= minutes_left
    blocked_by_spend_limit = bool(s["spend_limit"]
                                  and s["spend_limit"]["used_percentage"] >= 100)
    if blocked_by_spend_limit:
        fits = False

    verdict = "GO" if fits else "HOLD"
    lines = [
        f"**{verdict}** — {category} on {model_class}: ~{_amt(cost, unit)} "
        f"({src}), ~{mins:.0f} min.",
        f"Usable budget: {_amt(budget, unit)} ({_amt(s['remaining'], unit)} left − "
        f"{buffer_pct:.0f}% buffer), {_fmt_dur(minutes_left * 60)} to reset.",
    ]
    lines += _spend_limit_lines(s)
    if fits and s["burn_per_hour"]:
        implied = cost / max(mins / 60, 0.01)
        if implied < s["burn_per_hour"] * 0.6:
            lines.append(
                f"⚠️ Estimate implies {_amt(implied, unit)}/hour but you are actually "
                f"burning {_amt(s['burn_per_hour'], unit)}/hour — the real cost is likely "
                "higher. Treat this as a soft GO and re-check partway through."
            )
    alternatives = []
    if not fits and not blocked_by_spend_limit:
        for alt in ("sonnet", "haiku"):
            if alt == model_class:
                continue
            acost, amins, _ = _estimate(category, alt, unit)
            if acost <= budget and amins <= minutes_left:
                alternatives.append({"model_class": alt, **_unit_out(s, acost)})
                lines.append(f"→ Fits on **{alt}** (~{_amt(acost, unit)}).")
                break
        else:
            lines.append(
                f"→ Nothing fits. Wait {_fmt_dur(s['seconds_to_reset'])} for the reset, "
                "or split the task into micro steps."
            )
    out = {
        "summary": "\n".join(lines),
        "mode": s["mode"],
        "unit": unit,
        "fits": fits,
        "est_minutes": mins,
        "estimate_source": src,
        "usable_budget": round(budget, 2),
        "alternatives": alternatives,
    }
    out.update({f"est_{k}": v for k, v in _unit_out(s, cost).items()})
    if unit == "pct":  # keys older callers already read
        out["usable_budget_pct"] = round(budget, 1)
    return out


@server.tool(annotations=WRITES)
@_guard
def record_task_cost(
    category: Category,
    pct_used: float | None = None,
    usd_used: float | None = None,
    model_class: ModelClass = "opus",
    minutes: float | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Calibrate the estimator with what a finished task actually cost. Pass exactly
    one measurement: pct_used for subscription quota (percent of the 5-hour window,
    read off quota_status before and after), or usd_used for pay-per-token auth
    (dollars, from quota_status daily_usd before and after). Estimates switch from
    seeds to your own rolling average after two samples per category+model+unit."""
    if (pct_used is None) == (usd_used is None):
        raise ValueError("Pass exactly one of pct_used (subscription quota) or "
                         "usd_used (pay-per-token spend).")
    if pct_used is not None and not 0 <= pct_used <= 100:
        raise ValueError("pct_used must be between 0 and 100 (percent of the 5-hour window).")
    if usd_used is not None and usd_used < 0:
        raise ValueError("usd_used must not be negative.")

    unit = "pct" if pct_used is not None else "usd"
    field = unit
    value = pct_used if pct_used is not None else usd_used
    key = f"{model_class}:{category}" if unit == "pct" else f"usd:{model_class}:{category}"
    seed = (SEED_PCT if unit == "pct" else SEED_USD)[model_class][category]

    data = _load_costs()
    rec = data.get(key) or {field: seed, "minutes": SEED_MIN[category], "n": 0}
    rec.setdefault(field, seed)
    n = min(int(rec["n"]) + 1, 10)  # cap so the average keeps adapting
    rec[field] = rec[field] + (value - rec[field]) / n
    if minutes is not None:
        rec["minutes"] = rec["minutes"] + (minutes - rec["minutes"]) / n
    rec["n"] = int(rec["n"]) + 1
    rec["unit"] = unit
    rec["last_note"] = note
    rec["updated"] = int(time.time())
    data[key] = rec
    _save_json(COSTS, data)
    shown = f"{rec[field]:.1f}%" if unit == "pct" else f"${rec[field]:,.2f}"
    return {
        "summary": f"{key} now estimates {shown} / {rec['minutes']:.0f} min "
                   f"(from {rec['n']} sample(s)).",
        "key": key,
        "unit": unit,
        "estimate": round(rec[field], 2),
        "estimate_pct": round(rec[field], 2) if unit == "pct" else None,
        "estimate_usd": round(rec[field], 2) if unit == "usd" else None,
        "estimate_minutes": round(rec["minutes"], 1),
        "samples": rec["n"],
    }


@server.tool(annotations=WRITES)
@_guard
def set_budget(
    daily_usd: float | None = None,
    monthly_usd: float | None = None,
) -> dict[str, Any]:
    """Set the USD ceilings spend mode plans against (pay-per-token auth: API key,
    Bedrock, Vertex). Written to budget.json; QUOTA_BUDGET_USD_DAILY and
    QUOTA_BUDGET_USD_MONTHLY override it when set. Pass 0 to clear one."""
    if daily_usd is None and monthly_usd is None:
        raise ValueError("Pass daily_usd, monthly_usd, or both (0 clears one).")
    try:
        cfg = json.loads(BUDGET.read_text())
    except Exception:
        cfg = {}
    for key, val in (("daily_usd", daily_usd), ("monthly_usd", monthly_usd)):
        if val is None:
            continue
        if val < 0:
            raise ValueError(f"{key} must not be negative.")
        cfg[key] = float(val) if val > 0 else None
    _save_json(BUDGET, cfg)

    effective = _load_budget()
    lines = [
        f"**Daily budget:** {_amt(effective['daily_usd'], 'usd')}"
        + (f" _(from {effective['source']['daily_usd']})_"
           if effective["source"]["daily_usd"] else ""),
        f"**Monthly budget:** {_amt(effective['monthly_usd'], 'usd')}"
        + (f" _(from {effective['source']['monthly_usd']})_"
           if effective["source"]["monthly_usd"] else ""),
    ]
    for key in ("daily_usd", "monthly_usd"):
        if effective["source"][key] == "env" and cfg.get(key):
            lines.append(f"⚠️ {key} is pinned by an environment variable, so the value "
                         "written to budget.json is not what takes effect.")
    lines.append(f"_Written to {BUDGET}._")
    return {
        "summary": "\n".join(lines),
        "daily_usd": effective["daily_usd"],
        "monthly_usd": effective["monthly_usd"],
        "source": effective["source"],
        "path": str(BUDGET),
    }


@server.tool(annotations=WRITES)
@_guard
def reconcile_spend(days: int = 7, set_default: bool = True) -> dict[str, Any]:
    """Replace the local list-price estimate with billed dollars from the Admin API
    cost report (GET /v1/organizations/cost_report), and switch spend mode to use them.

    This is the only tool that touches the network. It needs an Admin API key in
    ANTHROPIC_ADMIN_KEY or an org:admin OAuth token in ANTHROPIC_ADMIN_OAUTH_TOKEN /
    ANTHROPIC_AUTH_TOKEN; the Admin API is unavailable for individual accounts.
    Costs come back per UTC day for the WHOLE ORGANIZATION — the endpoint takes no
    API-key or workspace filter, so on a shared org this includes your teammates.

    days: how many days of history to pull (1-31); the current month is always
    included so month-to-date is complete.
    set_default: also record spend_source=cost_report in budget.json, so later calls
    read billed figures instead of the local estimate.
    """
    now = time.time()
    days = max(1, min(int(days), 31))
    window_start = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(now - (days - 1) * 86400))
    month_start = _month_start_utc(now)
    starting_at = min(window_start, month_start)  # ISO-8601 sorts chronologically

    totals = _billed_totals(_cost_report(starting_at))
    if not totals:
        raise RuntimeError(
            "The cost report returned no buckets. Either nothing was billed in this "
            "window, or this credential belongs to an organization with no API usage."
        )

    led = _load_spend()
    prior = led.get("billed") or {}
    bdays = dict(prior.get("days") or {})
    bdays.update(totals)
    led["billed"] = {
        "days": dict(sorted(bdays.items())[-62:]),
        "fetched_at": int(now),
        "currency": "USD",
        "scope": "organization",
    }
    _save_json(SPEND, led)

    if set_default:
        try:
            cfg = json.loads(BUDGET.read_text())
        except Exception:
            cfg = {}
        cfg["spend_source"] = "cost_report"
        _save_json(BUDGET, cfg)

    today = _utc_day(now)
    month = today[:7]
    billed_today = float(led["billed"]["days"].get(today) or 0)
    billed_month = sum(float(v) for d, v in led["billed"]["days"].items() if d[:7] == month)
    local_today = float((led.get("days") or {}).get(time.strftime("%Y-%m-%d")) or 0)

    lines = [
        f"**Billed today (UTC {today}):** ${billed_today:,.2f}",
        f"**Billed month to date ({month}):** ${billed_month:,.2f}",
        f"_Pulled {len(totals)} daily bucket(s) from {starting_at}._",
    ]
    if local_today:
        drift = billed_today - local_today
        lines.append(
            f"Local list-price estimate for today was ${local_today:,.2f} "
            f"({'+' if drift >= 0 else '−'}${abs(drift):,.2f} difference). The two cover "
            "different windows — billed is a UTC day, the estimate a local one — and the "
            "billed figure covers the whole organization, not this machine."
        )
    source = _spend_source()
    if set_default and source != "cost_report":
        lines.append("⚠️ QUOTA_SPEND_SOURCE is set in the environment and overrides the "
                     "file, so the local estimate is still what gets reported.")
    elif set_default:
        lines.append("_spend_source is now cost_report: quota_status, check_budget and "
                     "plan_session read billed figures over UTC days. Burn rate stays "
                     "local — a daily bucket cannot produce an hourly rate._")

    return {
        "summary": "\n".join(lines),
        "billed_today_usd": round(billed_today, 2),
        "billed_month_usd": round(billed_month, 2),
        "local_estimate_today_usd": round(local_today, 2) or None,
        "days_fetched": len(totals),
        "starting_at": starting_at,
        "spend_source": _spend_source(),
        "scope": "organization",
    }


if __name__ == "__main__":
    server.run(transport="stdio")
