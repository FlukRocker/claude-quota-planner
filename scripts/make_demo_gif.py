#!/usr/bin/env python3
"""Render the README demo GIF from the status line's own output.

Every frame is real `statusline-quota.py` stdout: this script feeds it synthetic
status-line JSON, parses the truecolor ANSI it prints back, and draws the cells
with a monospace font. Nothing about the colours or the layout is reimplemented
here, so the GIF cannot drift from what the status line actually renders.

    uv run --with pillow scripts/make_demo_gif.py docs/statusline.gif
    uv run --with pillow scripts/make_demo_gif.py docs/statusline-spend.gif spend
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "statusline-quota.py"
FONT = "/System/Library/Fonts/Menlo.ttc"
SIZE = 22
BG = (13, 14, 18)
PAD = 18
LINE_GAP = 8
FPS = 6

SGR = re.compile(r"\033\[([0-9;]*)m")


def frames_quota(now: int):
    """A subscriber session filling its 5-hour window."""
    for i in range(22):
        pct5 = 4 + i * 4.3
        yield {
            "session_id": "demo", "cwd": str(ROOT),
            "workspace": {"current_dir": str(ROOT)},
            "model": {"id": "claude-opus-5", "display_name": "Opus 5 (1M context)"},
            "cost": {"total_cost_usd": 1.4 + i * 0.78, "total_duration_ms": 900_000 + i * 600_000,
                     "total_lines_added": 42 + i * 9, "total_lines_removed": 7 + i * 2},
            "context_window": {"used_percentage": 12 + i * 2.4, "context_window_size": 1_000_000},
            "rate_limits": {
                "five_hour": {"used_percentage": round(pct5, 1),
                              "resets_at": now + 17_600 - i * 720},
                "seven_day": {"used_percentage": round(6 + i * 0.5, 1),
                              "resets_at": now + 460_000},
            },
        }


def frames_spend(now: int):
    """Pay-per-token auth: no rate_limits at all, so the USD budget is the ceiling."""
    for i in range(22):
        yield {
            "session_id": "demo", "cwd": str(ROOT),
            "workspace": {"current_dir": str(ROOT)},
            "model": {"id": "claude-opus-5", "display_name": "Opus 5 (1M context)"},
            "cost": {"total_cost_usd": 6.2 + i * 0.63, "total_duration_ms": 5_400_000 + i * 600_000,
                     "total_lines_added": 42 + i * 9, "total_lines_removed": 7 + i * 2},
            "context_window": {"used_percentage": 12 + i * 2.0, "context_window_size": 1_000_000},
        }


SCENES = {"quota": frames_quota, "spend": frames_spend}


def render(payload: dict, env: dict) -> str:
    out = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                         capture_output=True, text=True, env=env)
    return out.stdout.rstrip("\n")


def parse(text: str):
    """ANSI -> [[(char, fg, bg, bold), ...], ...]. Only the codes we emit."""
    lines = []
    fg, bg, bold = (220, 220, 225), None, False
    for raw in text.split("\n"):
        cells, pos = [], 0
        for m in SGR.finditer(raw):
            for ch in raw[pos:m.start()]:
                cells.append((ch, fg, bg, bold))
            pos = m.end()
            params = [p for p in m.group(1).split(";") if p != ""] or ["0"]
            k = 0
            while k < len(params):
                p = params[k]
                if p == "0":
                    fg, bg, bold = (220, 220, 225), None, False
                elif p == "1":
                    bold = True
                elif p == "38" and params[k + 1:k + 2] == ["2"]:
                    fg = tuple(int(x) for x in params[k + 2:k + 5]); k += 4
                elif p == "48" and params[k + 1:k + 2] == ["2"]:
                    bg = tuple(int(x) for x in params[k + 2:k + 5]); k += 4
                k += 1
        for ch in raw[pos:]:
            cells.append((ch, fg, bg, bold))
        lines.append(cells)
    return lines


def draw(lines, font, bold_font, cw, ch):
    width = PAD * 2 + cw * max(len(l) for l in lines)
    height = PAD * 2 + len(lines) * ch + (len(lines) - 1) * LINE_GAP
    img = Image.new("RGB", (int(width), int(height)), BG)
    d = ImageDraw.Draw(img)
    for row, cells in enumerate(lines):
        y = PAD + row * (ch + LINE_GAP)
        for col, (char, fg, bg, bold) in enumerate(cells):
            x = PAD + col * cw
            if bg:
                d.rectangle([x, y, x + cw, y + ch], fill=bg)
            if char != " ":
                d.text((x, y), char, font=bold_font if bold else font, fill=fg)
    return img


def main() -> int:
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs" / "statusline.gif")
    scene = sys.argv[2] if len(sys.argv) > 2 else "quota"
    if scene not in SCENES:
        sys.exit(f"scene must be one of {', '.join(SCENES)}")
    font = ImageFont.truetype(FONT, SIZE)
    bold_font = ImageFont.truetype(FONT, SIZE, index=1)
    cw = font.getlength("M")
    ch = SIZE + 6

    now = int(time.time())
    with tempfile.TemporaryDirectory() as tmp:
        qdir = Path(tmp) / "quota"
        env = dict(os.environ, COLORTERM="truecolor", CLAUDE_QUOTA_DIR=str(qdir),
                   QUOTA_BAR_WIDTH="12", QUOTA_BAR_PALETTE="cyber",
                   QUOTA_BUDGET_USD_DAILY="20", QUOTA_BUDGET_USD_MONTHLY="300",
                   QUOTA_SPEND_SOURCE="local")
        env.pop("CLAUDE_QUOTA_DELEGATE", None)
        env.pop("NO_COLOR", None)
        if scene == "spend":
            # Seed a month already under way, so the monthly bar isn't a flat line.
            qdir.mkdir(parents=True, exist_ok=True)
            (qdir / "spend.json").write_text(json.dumps(
                {"sessions": {}, "days": {}, "months": {time.strftime("%Y-%m"): 214.0}}))
        # Render every frame first: the line grows as the session does, and a GIF
        # takes its canvas from frame one, so the width has to be the global max
        # or later frames get cropped.
        frames = [parse(render(payload, env)) for payload in SCENES[scene](now)]
        widest = max(len(l) for f in frames for l in f)
        imgs = [draw([l + [(" ", (0, 0, 0), None, False)] * (widest - len(l)) for l in f],
                     font, bold_font, cw, ch) for f in frames]
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Assembled with Pillow rather than ffmpeg: one shared palette quantized
        # from every frame at once, so the gradient stays stable frame to frame
        # and no colour crawls. (ffmpeg's paletteuse errors out on some builds.)
        stack = Image.new("RGB", (imgs[0].width, imgs[0].height * len(imgs)))
        for n, im in enumerate(imgs):
            stack.paste(im, (0, n * imgs[0].height))
        palette = stack.quantize(colors=192, method=Image.Quantize.MEDIANCUT)
        flat = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in imgs]
        dest.parent.mkdir(parents=True, exist_ok=True)
        flat[0].save(dest, save_all=True, append_images=flat[1:], loop=0,
                     duration=int(1000 / FPS), optimize=True, disposal=1)

    print(f"{dest} — {dest.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
