# claude-quota-planner

Two pieces that make Claude Code's subscription quota visible and plannable:

- **`statusline-quota.py`** — a status line that draws your **5-hour** and **7-day**
  quota as gradient bars, and writes each reading to disk.
- **`quota_planner.py`** — an MCP server that reads those readings, derives a real
  burn rate, and answers "does this task fit before the window resets?"

No network calls, no credentials, no undocumented endpoints. Claude Code already
sends `rate_limits.five_hour` / `rate_limits.seven_day` to the status line command
on stdin; everything here is downstream of that.

```
main* +42/-7 │ Opus 5 (1M context) │ 469K/1M (47%) │ $12.29/hr
5h ━━━━━━╸───── 36%  3h 29m left      7d ━╸────────── 8%  resets 1d 8h
```

> `rate_limits` is only sent to **Pro/Max subscriber** sessions. API-key auth gets
> nothing, and both pieces will sit idle.

## Install

```bash
mkdir -p ~/.claude/quota
cp statusline-quota.py quota_planner.py ~/.claude/quota/
chmod +x ~/.claude/quota/statusline-quota.py
```

### 1. Status line

Add to `~/.claude/settings.json` (see `examples/settings.snippet.json`):

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 \"$HOME/.claude/quota/statusline-quota.py\"",
    "refreshInterval": 30
  }
}
```

Already have a status line? Keep it — set `CLAUDE_QUOTA_DELEGATE` to it and the
quota bars render underneath (`examples/settings.snippet.delegate.json`).

Pure stdlib, no dependencies. Send one message so the first API response
populates `rate_limits`.

### 2. MCP server

```bash
claude mcp add quota-planner -- uv run --script ~/.claude/quota/quota_planner.py
```

`uv` reads the inline script metadata and installs `mcp>=2.0.0` into a throwaway
env — nothing to set up by hand. The server refuses to start answering until the
status line has written its first snapshot; that dependency is the whole design.

### 3. Teach Claude to use it

The tools only pay off if they're called at the right moments. Paste
`examples/CLAUDE.md.snippet` into your `~/.claude/CLAUDE.md`.

## Tools

| Tool | Does |
|---|---|
| `quota_status` | 5-hour used/remaining, weekly usage, time to reset, measured burn rate, and whether your current pace exhausts the window early |
| `plan_session` | Packs a task list into the quota *and* the clock left in the window. Returns what fits, what to defer past the reset, and where dropping to a cheaper model rescues a deferred task |
| `check_budget` | Go/no-go for one task before you start it, with a cheaper alternative when it doesn't fit |
| `record_task_cost` | Feeds back what a finished task actually cost, so estimates stop being guesses |

Tasks are sized in four buckets:

| Category | Means |
|---|---|
| `micro` | one-file edit, lookup, quick question |
| `small` | contained bug fix or small feature |
| `medium` | multi-file feature, refactor, test suite |
| `large` | migration, architecture change, long research/agent run |

### Calibration is the point

Anthropic publishes no token-to-quota mapping, so the shipped `SEED_PCT` numbers
are deliberately conservative placeholders. Each `record_task_cost` call folds a
real measurement into a rolling average (capped at n=10 so it keeps adapting);
after **two** samples for a given category+model the estimate switches from
"seed" to your own data. Until then, trust the burn rate over the table.

## Configuration

Both scripts read `CLAUDE_QUOTA_DIR` (default `~/.claude/quota`).

Status line only:

| Variable | Default | Does |
|---|---|---|
| `CLAUDE_QUOTA_DELEGATE` | — | Path to an existing status line to render above the bars |
| `QUOTA_BAR_STYLE` | `gradient` | `gradient` \| `solid` \| `ascii` |
| `QUOTA_BAR_PALETTE` | `cyber` | `cyber` \| `neon` \| `severity` |
| `QUOTA_BAR_WIDTH` | `12` | Cells per bar |
| `NO_COLOR` | — | Set to disable colour |

Gradients need a truecolor terminal (`COLORTERM=truecolor`); otherwise use
`QUOTA_BAR_STYLE=ascii`.

## Files it writes

All under `CLAUDE_QUOTA_DIR`, all gitignored — this is your usage data:

| File | Holds |
|---|---|
| `snapshot.json` | Latest reading: both windows, reset times, model, session cost |
| `history.jsonl` | Append-only log of readings — burn rate is the delta between the first and last reading inside the current window |
| `costs.json` | Your calibrated per-category estimates |
| `.gitcache.json` | Status line's git-status cache (keeps the line fast) |

## License

MIT
