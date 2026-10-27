#!/usr/bin/env python3
"""Generate the Lock In app icon: ceramic texture, bronze frame, extruded LOCK IN.

Usage: python3 make_icon.py [path/to/ArchivoBlack.ttf]
Falls back to the TTF embedded in server.py when the path is missing.
Outputs lock-in.png (256) and lock-in-128.png next to this script.
"""
import base64
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server.py"


def load_font(size: int) -> ImageFont.FreeTypeFont:
    from PIL import ImageFont
    candidates = []
    if len(sys.argv) > 1:
        candidates.append(sys.argv[1])
    candidates += [
        str(Path(tempfile.gettempdir()) / "grind-archivo-black.ttf"),
        "/tmp/opencode/ArchivoBlack.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    # unpack the TTF embedded in server.py
    import re
    m = re.search(r'ARCHIVO_TTF_B64 = "([^"]*)"', SERVER.read_text())
    tmp = Path(tempfile.gettempdir()) / "grind-archivo-black.ttf"
    tmp.write_bytes(base64.b64decode(m.group(1)))
    return ImageFont.truetype(str(tmp), size)


def build(size: int) -> Image.Image:
    S = size * 2  # draw large, downscale for crispness
    img = Image.new("RGB", (S, S))
    gif = Image.open(_ceramic_path()).convert("RGB")
    # tile the 640x480 texture over the canvas
    for y in range(0, S, gif.height):
        for x in range(0, S, gif.width):
            img.paste(gif, (x, y))
    img = img.crop((0, 0, S, S))

    d = ImageDraw.Draw(img)

    # bronze frame + inner highlight
    f = max(3, S // 32)
    d.rectangle([f, f, S - f, S - f], outline="#a85e00", width=f)
    d.rectangle([f + f, f + f, S - f - f, S - f - f], outline="#f2ae5c", width=max(1, S // 128))

    # extruded stacked wordmark
    f_big = load_font(int(S * 0.26))
    for text, cy in (("LOCK", S * 0.40), ("IN", S * 0.64)):
        w = d.textlength(text, font=f_big)
        x, y = (S - w) / 2, cy
        for off, col in ((S // 60, "#2a1600"), (S // 120, "#553000"), (0, "#e0912a")):
            d.text((x + off, y + off), text, font=f_big, fill=col)

    return img.resize((size, size), Image.LANCZOS)


_CERAMIC = None


def _ceramic_path() -> str:
    global _CERAMIC
    if _CERAMIC:
        return _CERAMIC
    for cand in ("/tmp/opencode/ceramic.gif",):
        if Path(cand).exists():
            _CERAMIC = cand
            return cand
    # unpack from server.py
    import re
    m = re.search(r'CERAMIC_GIF_B64 = "([^"]*)"', SERVER.read_text())
    tmp = Path(tempfile.gettempdir()) / "lockin-ceramic.gif"
    tmp.write_bytes(base64.b64decode(m.group(1)))
    _CERAMIC = str(tmp)
    return _CERAMIC


if __name__ == "__main__":
    build(256).save(HERE / "lock-in.png")
    build(128).save(HERE / "lock-in-128.png")
    print("wrote", HERE / "lock-in.png", "and lock-in-128.png")
