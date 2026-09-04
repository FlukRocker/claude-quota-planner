#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.0.0"]
# ///
"""
quota-planner — an MCP server that plans work against your Claude session quota.

Data source: Claude Code passes `rate_limits.five_hour` / `rate_limits.seven_day`
to the statusLine command on stdin. The companion statusline-quota.py writes
those numbers to ~/.claude/quota/{snapshot.json,history.jsonl}; this server
reads them, derives a burn rate, and packs tasks into what is left.

No network calls, no credentials, no undocumented endpoints.
"""

from __future__ import annotations

import json
import os
import time
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

Category = Literal["micro", "small", "medium", "large"]
ModelClass = Literal["opus", "sonnet", "haiku"]

# Seed estimates: percent of ONE 5-hour window consumed by one task, and rough
# wall-clock minutes. Anthropic publishes no token-to-quota mapping, so these
# are deliberately conservative starting points — record_task_cost() replaces
# them with your own measured numbers as you go.
SEED_PCT: dict[str, dict[str, float]] = {
    "opus":   {"micro": 1.5, "small": 4.0, "medium": 10.0, "large": 22.0},
    "sonnet": {"micro": 0.5, "small": 1.5, "medium": 4.0,  "large": 9.0},
    "haiku":  {"micro": 0.2, "small": 0.6, "medium": 1.5,  "large": 3.5},
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

server = _Server("quota-planner", version="0.1.0")


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


def _save_costs(data: dict[str, Any]) -> None:
    QDIR.mkdir(parents=True, exist_ok=True)
    tmp = COSTS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(COSTS)


def _estimate(category: str, model: str) -> tuple[float, float, str]:
    """Return (pct_of_5h_window, minutes, source) for a task."""
    key = f"{model}:{category}"
    learned = _load_costs().get(key)
    if learned and learned.get("n", 0) >= 2:
        return (
            float(learned["pct"]),
            float(learned.get("minutes") or SEED_MIN[category]),
            f"calibrated (n={int(learned['n'])})",
        )
    return SEED_PCT[model][category], SEED_MIN[category], "seed estimate"


# --------------------------------------------------------------------------- #
# derived metrics
# --------------------------------------------------------------------------- #
def _burn_rate(snap: dict[str, Any]) -> tuple[float | None, int]:
    """Percent of the 5-hour window consumed per hour, within the CURRENT window."""
    resets_at = snap["five_hour"].get("resets_at") or 0
    pts = [
        h for h in _load_history()
        if h.get("five_hour", {}).get("resets_at") == resets_at and h.get("ts")
    ]
    if len(pts) < 2:
        return None, len(pts)
    first, last = pts[0], pts[-1]
    hours = (last["ts"] - first["ts"]) / 3600.0
    if hours < 0.02:  # < ~1 minute of signal
        # Deliberately negative: too little TIME, which is a different problem
        # from too few samples and has a different fix (wait, don't send more).
        return None, -len(pts)
    delta = last["five_hour"]["used_percentage"] - first["five_hour"]["used_percentage"]
    return max(delta / hours, 0.0), len(pts)


def _fmt_dur(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _state() -> dict[str, Any]:
    """Everything the tools need, or a structured 'not set up' error."""
    snap = _load_snapshot()
    if not snap:
        raise RuntimeError(
            f"No quota snapshot at {SNAPSHOT}. The statusLine writer isn't running. "
            "Fix: save statusline-quota.py to ~/.claude/quota/, chmod +x it, point "
            '"statusLine".command at it in ~/.claude/settings.json, then send one '
            "message in Claude Code so the first API response populates rate_limits. "
            "Note rate_limits is only sent to Pro/Max subscriber sessions, not API-key auth."
        )

    now = time.time()
    five, seven = snap["five_hour"], snap.get("seven_day", {})
    resets_at = five.get("resets_at") or 0
    burn, points = _burn_rate(snap)
    used = float(five["used_percentage"])
    remaining = max(100.0 - used, 0.0)
    secs_left = max(resets_at - now, 0) if resets_at else 0

    runway_h = (remaining / burn) if burn else None
    return {
        "snapshot": snap,
        "age_seconds": now - snap.get("ts", now),
        "used_pct": used,
        "remaining_pct": remaining,
        "weekly_pct": float(seven.get("used_percentage") or 0),
        "resets_at": resets_at,
        "seconds_to_reset": secs_left,
        "burn_pct_per_hour": burn,
        "burn_points": points,
        "runway_hours": runway_h,
        # True when you will run dry before the window resets.
        "will_exhaust": bool(runway_h is not None and secs_left and runway_h * 3600 < secs_left),
    }


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@server.tool(annotations=READ_ONLY)
def quota_status() -> dict[str, Any]:
    """Report current Claude subscription quota: 5-hour window used/remaining,
    weekly usage, time until reset, measured burn rate, and whether the current
    pace will exhaust the window before it resets. Call this before starting any
    long or expensive piece of work."""
    s = _state()
    snap = s["snapshot"]
    lines = [
        f"**5-hour window:** {s['used_pct']:.0f}% used, {s['remaining_pct']:.0f}% left"
        + (f" — resets in {_fmt_dur(s['seconds_to_reset'])}" if s["resets_at"] else ""),
        f"**Weekly (all models):** {s['weekly_pct']:.0f}% used",
        f"**Model:** {snap.get('model', '?')}",
    ]
    if s["burn_pct_per_hour"] is None:
        pts = s["burn_points"]
        if pts < 0:
            lines.append(
                f"**Burn rate:** {-pts} samples in this window but under a minute apart — "
                "a rate off that span would be noise. It appears once they spread out."
            )
        else:
            lines.append(
                f"**Burn rate:** not enough samples yet ({pts} in this window)"
            )
    else:
        lines.append(
            f"**Burn rate:** {s['burn_pct_per_hour']:.1f}%/hour "
            f"→ ~{_fmt_dur((s['runway_hours'] or 0) * 3600)} of runway left"
        )
        if s["will_exhaust"]:
            lines.append(
                "⚠️ At this pace the 5-hour limit is hit **before** the window resets. "
                "Downgrade the model, shrink scope, or park the heavy task."
            )
    if s["weekly_pct"] >= 80:
        lines.append("⚠️ Weekly cap above 80% — the week, not the session, is the binding constraint.")
    if s["age_seconds"] > 1800:
        lines.append(f"_Snapshot is {_fmt_dur(s['age_seconds'])} old — send a message to refresh it._")

    return {
        "summary": "\n".join(lines),
        "used_pct": round(s["used_pct"], 1),
        "remaining_pct": round(s["remaining_pct"], 1),
        "weekly_pct": round(s["weekly_pct"], 1),
        "seconds_to_reset": int(s["seconds_to_reset"]),
        "burn_pct_per_hour": round(s["burn_pct_per_hour"], 2) if s["burn_pct_per_hour"] else None,
        "will_exhaust_before_reset": s["will_exhaust"],
        "snapshot_age_seconds": int(s["age_seconds"]),
    }


@server.tool(annotations=READ_ONLY)
def plan_session(
    tasks: list[dict],
    buffer_pct: float = 10.0,
    model_class: ModelClass = "opus",
) -> dict[str, Any]:
    """Pack a task list into the quota and time left in the current 5-hour window.

    tasks: list of objects with:
      - name (str, required)
      - category (str, required): micro = one-file edit or quick question;
        small = contained bug fix; medium = multi-file feature or refactor;
        large = migration, architecture change, or long agent run
      - priority (int 1-5, default 3): higher runs first
      - must_do (bool, default false): scheduled even if it overruns the buffer
      - model_class (str, optional): per-task override of the default
    buffer_pct: quota to hold back for unplanned work (default 10).
    model_class: default model tier the tasks will run on.

    Returns an ordered plan: what fits now, what to defer past the reset, and
    where switching to a cheaper model would rescue a deferred task.
    """
    s = _state()
    budget = max(s["remaining_pct"] - buffer_pct, 0.0)
    minutes_left = s["seconds_to_reset"] / 60 if s["resets_at"] else 5 * 60

    enriched = []
    for i, t in enumerate(tasks):
        name = t.get("name") or f"task {i + 1}"
        cat = t.get("category", "small")
        if cat not in SEED_MIN:
            raise ValueError(f"'{name}': category must be micro/small/medium/large. {CATEGORY_HELP}")
        mc = t.get("model_class", model_class)
        pct, mins, src = _estimate(cat, mc)
        enriched.append({
            "name": name, "category": cat, "model_class": mc,
            "priority": int(t.get("priority", 3)), "must_do": bool(t.get("must_do", False)),
            "est_pct": pct, "est_minutes": mins, "estimate_source": src,
        })

    # must-do first, then priority, then cheapest first so more tasks fit.
    enriched.sort(key=lambda t: (not t["must_do"], -t["priority"], t["est_pct"]))

    now_list, defer_list = [], []
    spent = spent_min = 0.0
    for t in enriched:
        fits_quota = spent + t["est_pct"] <= budget
        fits_time = spent_min + t["est_minutes"] <= minutes_left
        if (fits_quota and fits_time) or t["must_do"]:
            spent += t["est_pct"]
            spent_min += t["est_minutes"]
            t["cumulative_pct"] = round(spent, 1)
            if t["must_do"] and not (fits_quota and fits_time):
                t["note"] = "forced in as must_do — expect to overrun the buffer"
            now_list.append(t)
        else:
            blocker = "quota" if not fits_quota else "time to reset"
            t["blocked_by"] = blocker
            cheap_pct, _, _ = _estimate(t["category"], "sonnet")
            if not fits_quota and spent + cheap_pct <= budget and t["model_class"] != "sonnet":
                t["rescue"] = f"would fit on Sonnet (~{cheap_pct:.1f}% vs {t['est_pct']:.1f}%)"
            defer_list.append(t)

    rows = [
        "| # | Task | Cat | Model | Est. quota | Est. time | Cumulative |",
        "|---|------|-----|-------|-----------|-----------|------------|",
    ]
    for i, t in enumerate(now_list, 1):
        rows.append(
            f"| {i} | {t['name']} | {t['category']} | {t['model_class']} | "
            f"{t['est_pct']:.1f}% | {t['est_minutes']:.0f}m | {t['cumulative_pct']:.1f}% |"
        )
    if len(rows) == 2:
        rows.append("| — | _nothing fits in the remaining budget_ | | | | | |")

    out = [
        f"**Budget:** {s['remaining_pct']:.0f}% left − {buffer_pct:.0f}% buffer "
        f"= **{budget:.0f}% usable**, {_fmt_dur(minutes_left * 60)} until reset.",
        "",
        "**Run this window**",
        *rows,
    ]
    if defer_list:
        out += ["", "**Defer past the reset**"]
        for t in defer_list:
            extra = f" — {t['rescue']}" if "rescue" in t else ""
            out.append(
                f"- {t['name']} ({t['category']}, ~{t['est_pct']:.1f}%) "
                f"— blocked by {t['blocked_by']}{extra}"
            )
    if s["burn_pct_per_hour"] and spent_min > 0:
        implied = spent / max(spent_min / 60, 0.01)
        if implied < s["burn_pct_per_hour"] * 0.6:
            out += ["", f"⚠️ This plan assumes {implied:.0f}%/hour but your measured burn is "
                        f"{s['burn_pct_per_hour']:.0f}%/hour. Expect to finish roughly item "
                        f"{max(1, int(len(now_list) * implied / s['burn_pct_per_hour']))} of "
                        f"{len(now_list)} before the limit bites — calibrate with record_task_cost."]
    if s["weekly_pct"] >= 80:
        out += ["", f"⚠️ Weekly cap at {s['weekly_pct']:.0f}% — deferring to the next 5-hour "
                    "window may not help. Check the 7-day reset before planning around it."]
    out += ["", "_Estimates are seeds until calibrated. Call record_task_cost after "
                "finishing a task to replace them with your real numbers._"]

    return {
        "summary": "\n".join(out),
        "usable_budget_pct": round(budget, 1),
        "minutes_to_reset": int(minutes_left),
        "scheduled": now_list,
        "deferred": defer_list,
        "projected_end_pct": round(s["used_pct"] + spent, 1),
    }


@server.tool(annotations=READ_ONLY)
def check_budget(
    category: Category,
    model_class: ModelClass = "opus",
    buffer_pct: float = 10.0,
) -> dict[str, Any]:
    """Go / no-go check for a single task before starting it.
    Returns whether it fits the remaining window, and a cheaper alternative if not."""
    s = _state()
    pct, mins, src = _estimate(category, model_class)
    budget = max(s["remaining_pct"] - buffer_pct, 0.0)
    minutes_left = s["seconds_to_reset"] / 60 if s["resets_at"] else 300
    fits = pct <= budget and mins <= minutes_left

    verdict = "GO" if fits else "HOLD"
    lines = [
        f"**{verdict}** — {category} on {model_class}: ~{pct:.1f}% of the window "
        f"({src}), ~{mins:.0f} min.",
        f"Usable budget: {budget:.0f}% ({s['remaining_pct']:.0f}% left − {buffer_pct:.0f}% buffer), "
        f"{_fmt_dur(minutes_left * 60)} to reset.",
    ]
    if fits and s["burn_pct_per_hour"]:
        implied = pct / max(mins / 60, 0.01)
        if implied < s["burn_pct_per_hour"] * 0.6:
            lines.append(
                f"⚠️ Estimate implies {implied:.0f}%/hour but you are actually burning "
                f"{s['burn_pct_per_hour']:.0f}%/hour — the real cost is likely higher. "
                "Treat this as a soft GO and re-check partway through."
            )
    alternatives = []
    if not fits:
        for alt in ("sonnet", "haiku"):
            if alt == model_class:
                continue
            apct, amins, _ = _estimate(category, alt)
            if apct <= budget and amins <= minutes_left:
                alternatives.append({"model_class": alt, "est_pct": apct})
                lines.append(f"→ Fits on **{alt}** (~{apct:.1f}%).")
                break
        else:
            lines.append(
                f"→ Nothing fits. Wait {_fmt_dur(s['seconds_to_reset'])} for the reset, "
                "or split the task into micro steps."
            )
    return {
        "summary": "\n".join(lines),
        "fits": fits,
        "est_pct": pct,
        "est_minutes": mins,
        "estimate_source": src,
        "usable_budget_pct": round(budget, 1),
        "alternatives": alternatives,
    }


@server.tool(annotations=WRITES)
def record_task_cost(
    category: Category,
    pct_used: float,
    model_class: ModelClass = "opus",
    minutes: float | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Calibrate the estimator with what a finished task actually cost.
    pct_used: 5-hour quota percentage the task consumed — read it off quota_status
    before and after, or off the status line. Estimates switch from seeds to your
    own rolling average after two samples per category+model."""
    if pct_used < 0 or pct_used > 100:
        raise ValueError("pct_used must be between 0 and 100 (percent of the 5-hour window).")
    data = _load_costs()
    key = f"{model_class}:{category}"
    rec = data.get(key) or {
        "pct": SEED_PCT[model_class][category],
        "minutes": SEED_MIN[category],
        "n": 0,
    }
    n = min(int(rec["n"]) + 1, 10)  # cap so the average keeps adapting
    rec["pct"] = rec["pct"] + (pct_used - rec["pct"]) / n
    if minutes is not None:
        rec["minutes"] = rec["minutes"] + (minutes - rec["minutes"]) / n
    rec["n"] = int(rec["n"]) + 1
    rec["last_note"] = note
    rec["updated"] = int(time.time())
    data[key] = rec
    _save_costs(data)
    return {
        "summary": f"{key} now estimates {rec['pct']:.1f}% / {rec['minutes']:.0f} min "
                   f"(from {rec['n']} sample(s)).",
        "key": key,
        "estimate_pct": round(rec["pct"], 2),
        "estimate_minutes": round(rec["minutes"], 1),
        "samples": rec["n"],
    }


if __name__ == "__main__":
    server.run(transport="stdio")
