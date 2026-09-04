---
description: Install the 5h/7d quota status line into settings.json (the one part a plugin cannot declare)
allowed-tools: Bash(python3:*)
argument-hint: "[--dry-run] [--no-delegate] [--refresh <seconds>]"
---

Plugin manifests cannot declare a `statusLine`, so the status line half of
quota-planner has to be wired into user settings once. Without it the MCP server
has no snapshots to read.

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_statusline.py" $ARGUMENTS
```

What it does:

1. Links `statusline-quota.py` from the plugin into `$CLAUDE_QUOTA_DIR`
   (default `~/.claude/quota`), so plugin updates flow through the link.
2. Backs up `~/.claude/settings.json`, then points `statusLine` at that script.
3. Preserves any status line already configured by moving it into
   `CLAUDE_QUOTA_DELEGATE`, so it renders above the quota bars. Pass
   `--no-delegate` to replace it outright.

If the user has not asked for settings to be modified outright, run it with
`--dry-run` first, show them the plan, and only then run it for real. Afterwards,
tell them to send one more message so the first API response populates
`rate_limits`, and that `rate_limits` reaches Pro/Max subscriber sessions only.
