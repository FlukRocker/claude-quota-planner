#!/usr/bin/env python3
"""
statusline-quota.py — Claude Code status line + quota snapshot writer.

Line 1:  branch* +added/-removed │ Model[1m] │ 469K/1M (47%) │ $12.29/hr
Line 2:  5h ━━━━━━╸───── 36%  3h 29m left      7d ━╸────────── 8%  resets 1d 8h

Both quota bars share one line. No emoji. Bars are true-colour gradients:
each cell is coloured by its own position on the 0-100 scale, so the palette
runs green -> amber -> orange -> red as the bar fills.

Replaces statusline-quota.sh and drops the jq dependency — same snapshot and
history files, so the MCP server and hooks keep working unchanged.

Environment:
  CLAUDE_QUOTA_DIR    state directory        (default ~/.claude/quota)
  CLAUDE_QUOTA_DELEGATE  previous status line to render above (optional)
  QUOTA_BAR_STYLE     gradient|solid|ascii   (default gradient)
  QUOTA_BAR_PALETTE   cyber|neon|severity    (default cyber)
  QUOTA_BAR_WIDTH     cells per bar          (default 12)
  NO_COLOR            set to disable colour entirely
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

QDIR = Path(os.environ.get("CLAUDE_QUOTA_DIR", Path.home() / ".claude" / "quota"))
SNAPSHOT = QDIR / "snapshot.json"
HISTORY = QDIR / "history.jsonl"
GITCACHE = QDIR / ".gitcache.json"
DELEGATE = os.environ.get("CLAUDE_QUOTA_DELEGATE", "")

WIDTH = max(4, int(os.environ.get("QUOTA_BAR_WIDTH", 12)))
STYLE = os.environ.get("QUOTA_BAR_STYLE", "gradient")
NO_COLOR = bool(os.environ.get("NO_COLOR"))
TRUECOLOR = os.environ.get("COLORTERM", "") in ("truecolor", "24bit")

# Gradient stops: position on the 0-100 scale -> RGB.
# Terminals can't actually glow, so "neon" means near-maximum saturation with
# one channel pinned at 255 — that is what reads as neon on a dark background.
PALETTES = {
    "neon": [(0, (57, 255, 20)), (35, (170, 255, 0)), (55, (255, 234, 0)),
             (75, (255, 158, 0)), (90, (255, 42, 109)), (100, (255, 7, 58))],
    # Aesthetic only — no severity meaning, just a cool-to-hot sweep.
    "cyber": [(0, (0, 255, 240)), (40, (0, 178, 255)), (70, (168, 85, 247)),
              (100, (255, 0, 229))],
    # The muted original, for anyone who finds neon loud.
    "severity": [(0, (34, 197, 94)), (50, (234, 179, 8)),
                 (80, (249, 115, 22)), (100, (239, 68, 68))],
}
PALETTE = os.environ.get("QUOTA_BAR_PALETTE", "cyber")
STOPS = PALETTES.get(PALETTE, PALETTES["cyber"])
# Palettes whose colour says nothing about how bad the number is. On those the BAR wears the
# palette and the NUMBER keeps a severity colour, or a 95%-full cyber bar reads as decoration.
AESTHETIC = {"cyber"}
HALF = "\u258c"  # left half block: two colours per column, so 12 columns give 24 gradient steps
BOLD = "" if NO_COLOR else "\033[1m"
if NO_COLOR:
    DIM = RESET = ""
else:
    DIM = "\033[38;2;113;113;122m" if TRUECOLOR else "\033[90m"
    RESET = "\033[0m"


# --------------------------------------------------------------------------- #
# colour
# --------------------------------------------------------------------------- #
def _srgb_to_linear(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c):
    v = c * 12.92 if c <= 0.0031308 else 1.055 * (max(c, 0.0) ** (1 / 2.4)) - 0.055
    return min(255, max(0, round(v * 255)))


def _to_oklab(rgb):
    r, g, b = (_srgb_to_linear(x) for x in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = l ** (1 / 3), m ** (1 / 3), s ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _from_oklab(lab):
    L, a, b = lab
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return tuple(_linear_to_srgb(x) for x in (
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s))


def lerp(a, b, t):
    """Blend in Oklab, not in sRGB.

    Straight-line sRGB interpolation between two saturated colours dips through a darker,
    greyer middle - cyan to magenta goes visibly muddy around the halfway mark, and that dip
    is what reads as a band rather than a sweep. Oklab is perceptually uniform, so equal steps
    look equal and the ramp has no waist in it.
    """
    la, lb = _to_oklab(a), _to_oklab(b)
    return _from_oklab(tuple(x + (y - x) * t for x, y in zip(la, lb)))


def scale_color(pct):
    """Colour for a given position on the 0-100 scale."""
    pct = min(max(pct, 0), 100)
    for i in range(len(STOPS) - 1):
        p0, c0 = STOPS[i]
        p1, c1 = STOPS[i + 1]
        if p0 <= pct <= p1:
            return lerp(c0, c1, (pct - p0) / (p1 - p0) if p1 > p0 else 0)
    return STOPS[-1][1]


def pct_color(pct):
    """Colour for a NUMBER, which has to mean something even when the bar is decorative."""
    if PALETTE in AESTHETIC:
        return (255, 69, 58) if pct >= 90 else (255, 159, 10) if pct >= 75 else scale_color(pct)
    return scale_color(pct)


def dim(rgb, factor=0.26):
    """Darkened tint of a colour - the unfilled track, so the bar reads as a glow trail
    rather than sitting on flat gray."""
    return tuple(round(c * factor) for c in rgb)


def _c256(rgb):
    r, g, b = rgb
    return 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)


def paint(rgb, text, bold=False):
    if NO_COLOR:
        return text
    pre = BOLD if bold else ""
    if TRUECOLOR:
        r, g, b = rgb
        return f"{pre}\033[38;2;{r};{g};{b}m{text}{RESET}"
    return f"{pre}\033[38;5;{_c256(rgb)}m{text}{RESET}"


def paint_pair(fg, bg, ch):
    """One column carrying two colours: foreground left half, background right half."""
    if TRUECOLOR:
        return (f"\033[38;2;{fg[0]};{fg[1]};{fg[2]}m"
                f"\033[48;2;{bg[0]};{bg[1]};{bg[2]}m{ch}{RESET}")
    return f"\033[38;5;{_c256(fg)}m\033[48;5;{_c256(bg)}m{ch}{RESET}"


# --------------------------------------------------------------------------- #
# bar
# --------------------------------------------------------------------------- #
def bar(pct):
    """Gradient progress bar carrying twice its column count in colour steps.

    Each column is a left half block, so its foreground paints the left half and its background
    paints the right half: WIDTH columns carry 2*WIDTH colours. That is where the smoothness
    comes from - not from a wider bar, which would cost the line width the second window needs.
    """
    pct = min(max(float(pct or 0), 0.0), 100.0)
    if STYLE == "ascii" or NO_COLOR:
        # Half blocks are meaningless without colour - both halves are the same character.
        f = round(pct / 100 * WIDTH)
        return "[" + "=" * f + "-" * (WIDTH - f) + "]"

    subs = WIDTH * 2
    filled = int(round(pct / 100 * subs))
    out = []
    for cell in range(WIDTH):
        halves = []
        for k in (0, 1):
            i = cell * 2 + k
            # Colour each half by where IT sits on the scale, not by the total - that is what
            # makes it a gradient rather than a solid block.
            c = scale_color((i + 0.5) / subs * 100) if STYLE == "gradient" else scale_color(pct)
            halves.append(c if i < filled else dim(c))
        out.append(paint_pair(halves[0], halves[1], HALF))
    return "".join(out)


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def human_tokens(n):
    n = int(n or 0)
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.0f}M" if v >= 10 or v == int(v) else f"{v:.1f}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}K"
    return str(n)


def countdown(epoch, now):
    s = max(int((epoch or 0) - now), 0)
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def git_info(cwd):
    """Branch and dirty flag, cached for 3s so it isn't two spawns per keystroke."""
    if not cwd or not os.path.isdir(cwd):
        return None, False
    try:
        cache = json.loads(GITCACHE.read_text())
        if cache.get("cwd") == cwd and time.time() - cache.get("ts", 0) < 3:
            return cache.get("branch"), cache.get("dirty", False)
    except Exception:
        pass
    try:
        run = lambda *a: subprocess.run(
            ["git", "-C", cwd, "--no-optional-locks", *a],
            capture_output=True, text=True, timeout=1.5)
        b = run("symbolic-ref", "--short", "HEAD")
        branch = b.stdout.strip() if b.returncode == 0 else None
        if not branch:
            return None, False
        st = run("status", "--porcelain")
        dirty = bool(st.stdout.strip())
    except Exception:
        return None, False
    try:
        QDIR.mkdir(parents=True, exist_ok=True)
        GITCACHE.write_text(json.dumps(
            {"cwd": cwd, "branch": branch, "dirty": dirty, "ts": time.time()}))
    except Exception:
        pass
    return branch, dirty


# --------------------------------------------------------------------------- #
# persistence (unchanged contract — MCP server and hooks read these)
# --------------------------------------------------------------------------- #
def stored_window():
    """The 5-hour window the published snapshot currently describes, or None."""
    try:
        return int((json.loads(SNAPSHOT.read_text())
                    .get("five_hour") or {}).get("resets_at") or 0)
    except Exception:
        return None


def persist(d, now):
    five = (d.get("rate_limits") or {}).get("five_hour") or {}
    if five.get("used_percentage") is None:
        return
    seven = (d.get("rate_limits") or {}).get("seven_day") or {}
    snap = {
        "ts": now,
        "five_hour": {"used_percentage": five.get("used_percentage"),
                      "resets_at": five.get("resets_at") or 0},
        "seven_day": {"used_percentage": seven.get("used_percentage") or 0,
                      "resets_at": seven.get("resets_at") or 0},
        "model": (d.get("model") or {}).get("display_name") or "?",
        "model_id": (d.get("model") or {}).get("id") or "",
        "session_id": d.get("session_id") or "",
        "cwd": (d.get("workspace") or {}).get("current_dir") or d.get("cwd") or "",
        "session_cost_usd": (d.get("cost") or {}).get("total_cost_usd") or 0,
    }
    # Several Claude Code sessions share these files, and a long-running one can
    # still be reporting a window that has since reset — observed in practice,
    # with resets_at values already in the past sitting alongside current ones.
    # Publishing that as the snapshot would walk the headline numbers BACKWARDS,
    # so the snapshot only ever moves forward. History keeps every sample either
    # way: the burn-rate calculation filters on resets_at and wants the older
    # window too.
    prior = stored_window()
    if prior is None or snap["five_hour"]["resets_at"] >= prior:
        try:
            QDIR.mkdir(parents=True, exist_ok=True)
            tmp = SNAPSHOT.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snap))
            tmp.replace(SNAPSHOT)
        except Exception:
            return
    # Append only when the number moved: keeps the burn-rate series meaningful.
    try:
        last = None
        if HISTORY.exists():
            with open(HISTORY, "rb") as f:
                f.seek(max(0, f.seek(0, 2) - 4096))
                tail = f.read().decode("utf-8", "replace").splitlines()
            if tail:
                last = json.loads(tail[-1])["five_hour"]["used_percentage"]
        if last != snap["five_hour"]["used_percentage"]:
            with open(HISTORY, "a") as f:
                f.write(json.dumps(snap) + "\n")
            # Trim by LINE COUNT, not by size, and only once the file is twice
            # the keep target. A size threshold below what 3000 lines actually
            # weigh (~1MB here) re-trims the whole file on every single refresh.
            lines = HISTORY.read_text().splitlines()
            if len(lines) > 6000:
                HISTORY.write_text("\n".join(lines[-3000:]) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return
    now = int(time.time())
    persist(d, now)

    cost = d.get("cost") or {}
    ctx = d.get("context_window") or {}
    model = d.get("model") or {}
    cwd = (d.get("workspace") or {}).get("current_dir") or d.get("cwd") or ""
    sep = f" {DIM}│{RESET} " if not NO_COLOR else " | "

    # ---- line 0: whatever the status line was before this one, if any -------
    # Opt-in via CLAUDE_QUOTA_DELEGATE. Invoked with </dev/null because the
    # status line it replaces does not read stdin; this script has already
    # consumed it in any case.
    if DELEGATE and os.path.isfile(DELEGATE) and os.access(DELEGATE, os.X_OK):
        try:
            prev = subprocess.run([DELEGATE], capture_output=True, text=True,
                                  timeout=1.5, stdin=subprocess.DEVNULL)
            if prev.stdout.strip():
                print(prev.stdout.rstrip("\n"))
        except Exception:
            pass

    # ---- line 1 -----------------------------------------------------------
    seg = []
    branch, dirty = git_info(cwd)
    if branch:
        mark = paint((239, 68, 68), "*") if dirty else ""
        piece = paint((129, 140, 248), branch) + mark
        add, rem = cost.get("total_lines_added") or 0, cost.get("total_lines_removed") or 0
        if add or rem:
            piece += " " + paint((34, 197, 94), f"+{add}") + "/" + paint((239, 68, 68), f"-{rem}")
        seg.append(piece)

    name = model.get("display_name") or model.get("id") or "Claude"
    size = ctx.get("context_window_size") or 0
    if size >= 1_000_000:
        name += "[1m]"
    seg.append(paint((232, 121, 249), name))

    used_pct = ctx.get("used_percentage")
    if used_pct is not None and size:
        # used_percentage counts input tokens only. current_usage is null before
        # the first API call and just after /compact, so fall back to the
        # percentage when it's missing.
        cu = ctx.get("current_usage") or {}
        if cu:
            used_tok = sum(int(cu.get(k) or 0) for k in
                           ("input_tokens", "cache_creation_input_tokens",
                            "cache_read_input_tokens"))
        else:
            used_tok = round(float(used_pct) / 100 * size)
        seg.append(f"{human_tokens(used_tok)}/{human_tokens(size)} "
                   f"{paint(pct_color(float(used_pct)), f'({float(used_pct):.0f}%)')}")

    usd = float(cost.get("total_cost_usd") or 0)
    dur_h = float(cost.get("total_duration_ms") or 0) / 3_600_000
    if usd > 0:
        seg.append(paint((251, 146, 60),
                         f"${usd / dur_h:,.2f}/hr" if dur_h > 0.02 else f"${usd:,.2f}"))
    if seg:
        print(sep.join(seg))

    # ---- line 2: both windows, one line -----------------------------------
    rl = d.get("rate_limits") or {}
    five, seven = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    if five.get("used_percentage") is None:
        print(f"{DIM}5h/7d quota  waiting for first API response{RESET}")
        return

    parts = []
    p5 = float(five["used_percentage"])
    tail5 = f"{countdown(five.get('resets_at'), now)} left" if five.get("resets_at") else ""
    parts.append(f"{DIM}5h{RESET} {bar(p5)} {paint(pct_color(p5), f'{p5:.0f}%')}"
                 + (f" {DIM}{tail5}{RESET}" if tail5 else ""))

    if seven.get("used_percentage") is not None:
        p7 = float(seven["used_percentage"])
        tail7 = f"resets {countdown(seven.get('resets_at'), now)}" if seven.get("resets_at") else ""
        parts.append(f"{DIM}7d{RESET} {bar(p7)} {paint(pct_color(p7), f'{p7:.0f}%')}"
                     + (f" {DIM}{tail7}{RESET}" if tail7 else ""))

    print(("   " if NO_COLOR else f"  {DIM}·{RESET}  ").join(parts))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
