#!/usr/bin/env python3
"""Wire the quota status line into ~/.claude/settings.json.

Plugin manifests cannot declare a statusLine, so this is the one piece of the
plugin that has to touch user settings. It links the shipped script into
CLAUDE_QUOTA_DIR (default ~/.claude/quota) so plugin updates flow through, then
sets settings.statusLine to run it.

An existing statusLine is preserved: it is moved into CLAUDE_QUOTA_DELEGATE and
rendered above the quota bars. settings.json is backed up before any write.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

SELF = Path(__file__).resolve()
PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or SELF.parent.parent).resolve()
SOURCE = PLUGIN_ROOT / "statusline-quota.py"
QUOTA_DIR = Path(os.environ.get("CLAUDE_QUOTA_DIR") or Path.home() / ".claude" / "quota")
SETTINGS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def link_script(dest: Path, dry_run: bool) -> str:
    """Symlink the plugin's status line into the quota dir; copy if links fail."""
    if not SOURCE.is_file():
        sys.exit(f"error: {SOURCE} not found — is CLAUDE_PLUGIN_ROOT set correctly?")
    if dest.is_symlink() and dest.resolve() == SOURCE:
        return f"already linked: {dest} -> {SOURCE}"
    if dry_run:
        return f"would link: {dest} -> {SOURCE}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        dest.symlink_to(SOURCE)
        return f"linked: {dest} -> {SOURCE}"
    except OSError:  # Windows without developer mode, exotic filesystems
        shutil.copy2(SOURCE, dest)
        dest.chmod(0o755)
        return f"copied (symlink unavailable): {SOURCE} -> {dest}"


def statusline_command(script: Path, delegate: str | None) -> str:
    quoted = f'"{script}"'
    if delegate:
        return f'CLAUDE_QUOTA_DELEGATE={delegate} python3 {quoted}'
    return f"python3 {quoted}"


def existing_delegate(current: dict | None) -> str | None:
    """Reuse whatever status line is already configured, unless it is ours."""
    if not isinstance(current, dict) or current.get("type") != "command":
        return None
    command = current.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    if "statusline-quota.py" in command:
        return None  # already us; don't nest
    return f'"{command}"' if '"' not in command else f"'{command}'"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--refresh", type=int, default=30, help="statusLine refreshInterval, seconds")
    ap.add_argument("--no-delegate", action="store_true", help="replace any existing status line outright")
    args = ap.parse_args()

    dest = QUOTA_DIR / "statusline-quota.py"
    steps = [link_script(dest, args.dry_run)]

    settings: dict = {}
    if SETTINGS.is_file():
        try:
            settings = json.loads(SETTINGS.read_text())
        except json.JSONDecodeError as exc:
            sys.exit(f"error: {SETTINGS} is not valid JSON ({exc}) — fix it and rerun")
        if not isinstance(settings, dict):
            sys.exit(f"error: {SETTINGS} does not contain a JSON object")

    delegate = None if args.no_delegate else existing_delegate(settings.get("statusLine"))
    if delegate:
        steps.append(f"keeping existing status line as CLAUDE_QUOTA_DELEGATE={delegate}")

    wanted = {
        "type": "command",
        "command": statusline_command(dest, delegate),
        "refreshInterval": args.refresh,
    }

    if settings.get("statusLine") == wanted:
        steps.append(f"settings.json already up to date: {SETTINGS}")
    elif args.dry_run:
        steps.append(f"would set statusLine in {SETTINGS} to:\n  {json.dumps(wanted, indent=2)}")
    else:
        if SETTINGS.is_file():
            backup = SETTINGS.with_suffix(f".json.bak.{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(SETTINGS, backup)
            steps.append(f"backed up: {backup}")
        settings["statusLine"] = wanted
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
        steps.append(f"set statusLine in {SETTINGS}")

    print("\n".join(steps))
    print(
        "\nNext: send one message so Claude Code's first API response populates "
        "rate_limits, then the MCP tools have a snapshot to read.\n"
        "Note: rate_limits is only sent to Pro/Max subscriber sessions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
