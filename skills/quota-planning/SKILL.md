---
name: quota-planning
description: Plan work against whatever ceiling this session is under — the 5-hour/7-day subscription quota, or a USD budget on pay-per-token auth (API key, Bedrock, Vertex). Use before starting a medium or large task, when sequencing multi-step work, when the user asks "will this fit", "how much quota is left", "what has today cost", "should I use a cheaper model", or after finishing a sized task (record what it cost). Backed by the quota-planner MCP server.
---

# Quota planning

The `quota-planner` MCP server turns the readings Claude Code sends to the status
line into go/no-go answers. Nothing here makes a network call; every number comes
from snapshots the status line wrote to disk.

## Two modes, because there are two different ceilings

| Auth | `mode` | Ceiling | Unit |
|---|---|---|---|
| Claude.ai Pro / Max | `quota` | 5-hour window, 7-day window, and behind a Claude apps gateway a spend limit | percent |
| API key, Bedrock, Vertex | `spend` | the daily and monthly USD budget **you** set | dollars |

`rate_limits` is sent to subscriber sessions only — pay-per-token auth has no
server-side window, so there is nothing to read and no reset to wait for. Every
tool takes `mode` (`auto` follows the latest session; force one when a machine
runs both kinds of session).

Spend mode needs a budget before it can answer anything: `set_budget(daily_usd=…,
monthly_usd=…)`, or the `QUOTA_BUDGET_USD_DAILY` / `QUOTA_BUDGET_USD_MONTHLY` env
vars, which override the file. Without one the tools report spend and refuse to
invent a verdict.

## Prerequisite

The server answers only once the status line has written its first snapshot. If a
tool returns an `error` field saying so, run `/quota-planner:quota-setup` and send
one more message so an API response populates it.

## When to call what

| Moment | Tool |
|---|---|
| Before a task sized `medium` or `large` | `check_budget` |
| Before multi-step work, or when the order matters | `plan_session` first, then follow its ordering |
| User asks how much is left, or what today cost | `quota_status` |
| Right after finishing a sized task | `record_task_cost` |
| User names a spending limit for pay-per-token work | `set_budget` |
| User wants real billed dollars, not the estimate (org accounts) | `reconcile_spend` |

Task sizes: `micro` (one-file edit, lookup), `small` (contained fix or feature),
`medium` (multi-file feature, refactor, test suite), `large` (migration,
architecture change, long research or agent run).

`record_task_cost` takes **exactly one** measurement, and which one depends on the
mode: `pct_used` for subscription quota, `usd_used` for spend. They calibrate
separate tables — a percentage measured on a subscription says nothing about an
API bill.

## Reading the answers

- **Burn rate beats the table.** The shipped seeds are deliberately conservative
  placeholders; when the measured burn-rate warning disagrees with the
  per-category estimate, trust the burn rate.
- **`estimate_source`** says `seed` until two `record_task_cost` samples exist for
  that category+model+unit, then switches to `calibrated (n=…)`. Recording is what
  makes the estimates real — do not skip it.
- **The gateway spend limit is a separate denominator.** It is reported and warned
  on, never mixed into the 5-hour arithmetic. At 100% nothing runs until it
  resets, whatever the window says.
- **Spend figures are estimates by default.** `cost.total_cost_usd` is computed
  client-side at list price and resets on `/clear`; it is not the invoice. Say so
  when quoting it. `reconcile_spend` replaces it with billed dollars from the Admin
  API cost report — that needs an org account and an admin credential, reports
  org-wide totals over **UTC** days, and still leaves the burn rate estimated. When
  `quota_status` says figures are billed, drop the estimate caveat and use the
  UTC-day framing instead.
- **A `check_budget` refusal is not a stop sign.** It returns cheaper alternatives
  (smaller model class, narrower scope); surface those rather than declining work.
- **Deferral is a real option.** `plan_session` returns what fits now and what to
  push past the reset; say which is which instead of quietly dropping tasks.

## Reporting

Give the user the number and the decision, not the raw JSON: what fits, what
doesn't, how long until the reset, and the cheaper path when there is one.
