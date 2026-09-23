"""Rasterize actual Ratatui TestBackend cells; requires Pillow, never an image generator."""

# Standalone helper for the Cargo project, not a Python package.
# ruff: noqa: INP001

import argparse
import json
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cells", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font", default="/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
    parser.add_argument("--cjk-font", default="/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    args = parser.parse_args()
    data = json.loads(args.cells.read_text())
    width, height = data["width"], data["height"]
    font = ImageFont.truetype(args.font, 16)
    cjk = ImageFont.truetype(args.cjk_font, 16) if Path(args.cjk_font).is_file() else font
    image = Image.new("RGB", (width * 10 + 32, height * 22 + 32), "#14171d")
    draw = ImageDraw.Draw(image)
    covered = set()
    for index, cell in enumerate(data["cells"]):
        span = sum(2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in cell["text"])
        covered.update(range(index + 1, index + span))
    for index, cell in enumerate(data["cells"]):
        if index in covered:
            continue
        x, y = 16 + index % width * 10, 16 + index // width * 22
        span = sum(2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in cell["text"])
        draw.rectangle((x, y, x + max(1, span) * 10, y + 22), fill=cell["bg"])
    for index, cell in enumerate(data["cells"]):
        if index in covered:
            continue
        x, y = 16 + index % width * 10, 16 + index // width * 22
        draw.text(
            (x, y),
            cell["text"],
            font=cjk if any(ord(c) > 0x2E80 for c in cell["text"]) else font,
            fill=cell["fg"],
        )
    image.save(args.output)


if __name__ == "__main__":
    main()
