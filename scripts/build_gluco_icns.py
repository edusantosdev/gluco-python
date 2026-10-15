#!/usr/bin/env python3
"""Render Gluco.app icon sizes from the artwork and build ``Gluco.icns``.

The artwork (``scripts/app_bundle/Gluco.icon/Assets/artwork.png``) is the
same image the Icon Composer icon uses, so the ``.icns`` fallback for macOS
before 26 matches the Liquid Glass icon. Each size clips it to the standard
macOS icon body: an 824 pt rounded square centred on a 1024 pt canvas.

Requires macOS (uses AppKit drawing + ``iconutil``). Run from the repo root::

    .venv/bin/python scripts/build_gluco_icns.py

Output: ``scripts/app_bundle/Gluco.icns``
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUT_ICNS = _REPO_ROOT / "scripts" / "app_bundle" / "Gluco.icns"
_ARTWORK = _REPO_ROOT / "scripts" / "app_bundle" / "Gluco.icon" / "Assets" / "artwork.png"

# Apple's macOS icon grid, as fractions of the canvas: the body is 824 of
# 1024 with a ~185 corner radius, leaving room for the system shadow.
_BODY_FRACTION = 824.0 / 1024.0
_CORNER_FRACTION = 185.4 / 824.0

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


def _draw_app_icon(size: int):
    from AppKit import (
        NSBezierPath,
        NSCompositingOperationSourceOver,
        NSGraphicsContext,
        NSImage,
        NSImageInterpolationHigh,
        NSMakeRect,
        NSSize,
        NSZeroRect,
    )

    artwork = NSImage.alloc().initWithContentsOfFile_(str(_ARTWORK))
    if artwork is None:
        raise RuntimeError(f"Could not load {_ARTWORK}")

    body = size * _BODY_FRACTION
    inset = (size - body) / 2.0
    corner = body * _CORNER_FRACTION
    body_rect = NSMakeRect(inset, inset, body, body)

    img = NSImage.alloc().initWithSize_(NSSize(size, size))
    img.lockFocus()
    try:
        NSGraphicsContext.currentContext().setImageInterpolation_(NSImageInterpolationHigh)
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            body_rect, corner, corner
        ).addClip()
        artwork.drawInRect_fromRect_operation_fraction_(
            body_rect, NSZeroRect, NSCompositingOperationSourceOver, 1.0
        )
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

    with tempfile.TemporaryDirectory(prefix="gluco_iconset_") as tmp:
        iconset = Path(tmp) / "Gluco.iconset"
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
