#!/usr/bin/env python3
"""Render GlucoBar.app icon assets and build ``GlucoBar.icns``.

The app icon is a black rounded tile with the sourced SVG Repo droplet in white.

Requires macOS (uses AppKit drawing + ``iconutil``). Run from the repo root::

    .venv/bin/python scripts/build_glucobar_icns.py

Output: ``scripts/app_bundle/GlucoBar.icns``
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUT_ICNS = _REPO_ROOT / "scripts" / "app_bundle" / "GlucoBar.icns"
_DROP_SVG = _REPO_ROOT / "scripts" / "app_bundle" / "drop-svgrepo-com.svg"
_DROP_VIEWBOX_SIZE = 512.0
_SVG_PATH_TOKEN = re.compile(
    r"[AaCcHhLlMmQqSsTtVvZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?"
)

# macOS iconset sizes (points → pixel width/height).
_ICONSET_ENTRIES: tuple[tuple[str, int], ...] = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)


def _is_svg_command(token: str) -> bool:
    return token.isalpha()


def _load_drop_path_data() -> str:
    root = ET.parse(_DROP_SVG).getroot()
    for elem in root.iter():
        if elem.tag.endswith("path") and elem.attrib.get("d"):
            return elem.attrib["d"]
    raise RuntimeError(f"No SVG path found in {_DROP_SVG}")


def _svg_path_to_bezier(path_data: str):
    """Convert the subset of SVG path commands used by the source droplet."""
    from AppKit import NSBezierPath

    tokens = _SVG_PATH_TOKEN.findall(path_data)
    path = NSBezierPath.bezierPath()
    index = 0
    command: str | None = None
    current = (0.0, 0.0)
    start = (0.0, 0.0)

    def has_number() -> bool:
        return index < len(tokens) and not _is_svg_command(tokens[index])

    def read_float() -> float:
        nonlocal index
        value = float(tokens[index])
        index += 1
        return value

    while index < len(tokens):
        if _is_svg_command(tokens[index]):
            command = tokens[index]
            index += 1

        if command in {"M", "m"}:
            first = True
            while has_number():
                x, y = read_float(), read_float()
                if command == "m":
                    x += current[0]
                    y += current[1]
                current = (x, y)
                if first:
                    path.moveToPoint_(current)
                    start = current
                    first = False
                else:
                    path.lineToPoint_(current)
            command = "l" if command == "m" else "L"
        elif command in {"L", "l"}:
            while has_number():
                x, y = read_float(), read_float()
                if command == "l":
                    x += current[0]
                    y += current[1]
                current = (x, y)
                path.lineToPoint_(current)
        elif command in {"H", "h"}:
            while has_number():
                x = read_float()
                if command == "h":
                    x += current[0]
                current = (x, current[1])
                path.lineToPoint_(current)
        elif command in {"V", "v"}:
            while has_number():
                y = read_float()
                if command == "v":
                    y += current[1]
                current = (current[0], y)
                path.lineToPoint_(current)
        elif command in {"C", "c"}:
            while has_number():
                c1 = (read_float(), read_float())
                c2 = (read_float(), read_float())
                end = (read_float(), read_float())
                if command == "c":
                    c1 = (c1[0] + current[0], c1[1] + current[1])
                    c2 = (c2[0] + current[0], c2[1] + current[1])
                    end = (end[0] + current[0], end[1] + current[1])
                path.curveToPoint_controlPoint1_controlPoint2_(end, c1, c2)
                current = end
        elif command in {"Z", "z"}:
            path.closePath()
            current = start
            command = None
        else:
            raise RuntimeError(f"Unsupported SVG path command: {command}")

    return path


def _draw_app_icon(pixels: int):
    """Return an ``NSImage`` ``pixels``×``pixels`` with a black tile and sourced white droplet."""
    from AppKit import (
        NSBezierPath,
        NSColor,
        NSGraphicsContext,
        NSImage,
    )
    from Foundation import NSAffineTransform, NSMakeRect, NSSize

    size = float(pixels)
    img = NSImage.alloc().initWithSize_(NSSize(size, size))
    img.lockFocus()
    try:
        bg = NSColor.colorWithCalibratedWhite_alpha_(0.05, 1.0)
        bg.setFill()
        corner = size * 0.223
        tile = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0, 0, size, size), corner, corner
        )
        tile.fill()

        pad = size * 0.14
        icon_size = size - 2 * pad
        scale = icon_size / _DROP_VIEWBOX_SIZE

        NSGraphicsContext.saveGraphicsState()
        try:
            transform = NSAffineTransform.transform()
            transform.translateXBy_yBy_(pad, pad + icon_size)
            transform.scaleXBy_yBy_(scale, -scale)
            transform.concat()

            NSColor.whiteColor().setFill()
            _svg_path_to_bezier(_load_drop_path_data()).fill()
        finally:
            NSGraphicsContext.restoreGraphicsState()
    finally:
        img.unlockFocus()
    return img


def _png_data_for_image(img) -> bytes:
    from AppKit import NSBitmapImageRep, NSPNGFileType

    tiff = img.TIFFRepresentation()
    if tiff is None:
        raise RuntimeError("Could not get TIFF representation for icon")
    rep = NSBitmapImageRep.imageRepWithData_(tiff)
    if rep is None:
        raise RuntimeError("Could not create bitmap rep for icon")
    png = rep.representationUsingType_properties_(NSPNGFileType, None)
    if png is None:
        raise RuntimeError("Could not encode icon as PNG")
    return bytes(png)


def main() -> int:
    if sys.platform != "darwin":
        print("This script must run on macOS.", file=sys.stderr)
        return 2
    iconutil = shutil.which("iconutil")
    if not iconutil:
        print("iconutil not found (need Xcode CLT).", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="glucobar_iconset_") as tmp:
        iconset = Path(tmp) / "GlucoBar.iconset"
        iconset.mkdir()
        for filename, px in _ICONSET_ENTRIES:
            img = _draw_app_icon(px)
            png_bytes = _png_data_for_image(img)
            (iconset / filename).write_bytes(png_bytes)

        _OUT_ICNS.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [iconutil, "-c", "icns", str(iconset), "-o", str(_OUT_ICNS)],
            check=True,
        )
    print(f"Wrote {_OUT_ICNS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
