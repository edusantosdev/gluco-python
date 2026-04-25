#!/usr/bin/env python3
"""Render GlucoBar.app icon assets (yellow SF Symbols droplet) and build ``GlucoBar.icns``.

Requires macOS (uses AppKit drawing + ``iconutil``). Run from the repo root::

    .venv/bin/python scripts/build_glucobar_icns.py

Output: ``scripts/app_bundle/GlucoBar.icns``
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUT_ICNS = _REPO_ROOT / "scripts" / "app_bundle" / "GlucoBar.icns"

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

def _draw_app_icon(pixels: int):
    """Return an ``NSImage`` ``pixels``×``pixels`` with a yellow library droplet icon."""
    from AppKit import (
        NSBezierPath,
        NSColor,
        NSCompositingOperationSourceOver,
        NSImage,
        NSImageSymbolConfiguration,
    )
    from Foundation import NSMakeRect, NSSize

    size = float(pixels)
    img = NSImage.alloc().initWithSize_(NSSize(size, size))
    img.lockFocus()
    try:
        # Transparent corners (macOS rounds the dock tile; crisp silhouette still helps).
        NSColor.clearColor().setFill()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, size, size))

        symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_("drop.fill", None)
        if symbol is None:
            raise RuntimeError("Could not load SF Symbol: drop.fill")

        yellow = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.96, 0.78, 0.16, 1.0)
        config = NSImageSymbolConfiguration.configurationWithHierarchicalColor_(yellow)
        symbol = symbol.imageWithSymbolConfiguration_(config) or symbol

        pad = size * 0.08
        rect = NSMakeRect(pad, pad, size - 2 * pad, size - 2 * pad)
        symbol.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            rect,
            NSMakeRect(0, 0, 0, 0),
            NSCompositingOperationSourceOver,
            1.0,
            True,
            None,
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
