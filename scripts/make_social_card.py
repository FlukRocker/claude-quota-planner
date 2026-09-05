#!/usr/bin/env python3
"""Render the GitHub social preview card (1280x640) for the repo.

Reuses the demo GIF machinery, so the two status lines on the card are real
`statusline-quota.py` output rather than a mockup of it.

    uv run --with pillow scripts/make_social_card.py docs/social-preview.png
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "make_demo_gif.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

W, H = 1280, 640
BG = (11, 12, 16)
PANEL = (18, 20, 26)
TITLE = (240, 241, 245)
BODY = (150, 156, 168)
FAINT = (108, 114, 128)
ACCENT = [(0, 255, 240), (0, 178, 255), (168, 85, 247), (255, 0, 229)]  # the cyber palette
PAD = 72
MONO = 18


def sf(size: int, weight: str = "Regular"):
    f = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass  # static build of the font: regular is fine
    return f


def status_image(payload: dict, env: dict, font, bold, cw, ch) -> Image.Image:
    lines = demo.parse(demo.render(payload, env))
    return demo.draw(lines, font, bold, cw, ch)


def accent_bar(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int) -> None:
    """A thin sweep of the bar palette — the one visual the project is known by."""
    for i in range(w):
        t = i / max(w - 1, 1) * (len(ACCENT) - 1)
        lo = min(int(t), len(ACCENT) - 2)
        c = demo_lerp(ACCENT[lo], ACCENT[lo + 1], t - lo)
        d.rectangle([x + i, y, x + i + 1, y + h], fill=c)


def demo_lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def main() -> int:
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs" / "social-preview.png")
    # Draw the status lines straight onto the panel colour: a shot carrying its
    # own background would sit inside the panel as a visible darker rectangle.
    demo.BG, demo.PAD = PANEL, 16
    mono = ImageFont.truetype(demo.FONT, MONO)
    mono_bold = ImageFont.truetype(demo.FONT, MONO, index=1)
    cw, ch = mono.getlength("M"), MONO + 6

    now = int(time.time())
    card = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(card)

    with tempfile.TemporaryDirectory() as tmp:
        qdir = Path(tmp) / "quota"
        qdir.mkdir(parents=True)
        env = dict(os.environ, COLORTERM="truecolor", CLAUDE_QUOTA_DIR=str(qdir),
                   QUOTA_BAR_WIDTH="12", QUOTA_BAR_PALETTE="cyber",
                   QUOTA_BUDGET_USD_DAILY="20", QUOTA_BUDGET_USD_MONTHLY="300",
                   QUOTA_SPEND_SOURCE="local")
        env.pop("CLAUDE_QUOTA_DELEGATE", None)
        env.pop("NO_COLOR", None)
        (qdir / "spend.json").write_text(json.dumps(
            {"sessions": {}, "days": {}, "months": {time.strftime("%Y-%m"): 214.0}}))

        quota = list(demo.frames_quota(now))[13]
        spend = list(demo.frames_spend(now))[19]
        shots = [
            ("Pro / Max subscription", status_image(quota, env, mono, mono_bold, cw, ch)),
            ("API key · Bedrock · Vertex", status_image(spend, env, mono, mono_bold, cw, ch)),
        ]

    accent_bar(d, PAD, PAD, 132, 5)

    d.text((PAD, PAD + 32), "claude-quota-planner", font=sf(58, "Bold"), fill=TITLE)
    d.text((PAD, PAD + 108), "Know what's left before you start the task.",
           font=sf(30), fill=BODY)

    y = 236
    for label, shot in shots:
        d.text((PAD, y), label, font=sf(20, "Medium"), fill=FAINT)
        y += 34
        panel_h = shot.height + 12
        d.rounded_rectangle([PAD - 14, y - 6, W - PAD + 14, y + panel_h - 6],
                            radius=12, fill=PANEL)
        card.paste(shot, (PAD, y))
        y += panel_h + 26

    d.text((PAD, H - PAD - 6), "status line  ·  MCP server  ·  Claude Code plugin",
           font=sf(21), fill=FAINT)

    dest.parent.mkdir(parents=True, exist_ok=True)
    card.save(dest, optimize=True)
    print(f"{dest} — {dest.stat().st_size / 1024:.0f} KB, {card.size[0]}x{card.size[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
