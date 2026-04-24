#!/usr/bin/env python3
"""Render GlucoBar.app icon assets (blood-toned droplet + CGM stroke) and build ``GlucoBar.icns``.

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


def _droplet_path(size: float):
    """Symmetric teardrop inside ``(0,0)–(size,size)`` with comfortable margins."""
    from AppKit import NSBezierPath

    pad = size * 0.06
    cx = size * 0.5
    tip_y = size - pad
    # Upper bulb: smooth round head; sides pinch into the tip.
    top_y = pad + size * 0.12
    shoulder_y = pad + size * 0.42
    path = NSBezierPath.bezierPath()
    path.moveToPoint_((cx, tip_y))
    path.curveToPoint_controlPoint1_controlPoint2_(
        (pad + size * 0.08, top_y + size * 0.06),
        (cx - size * 0.02, tip_y - size * 0.08),
        (pad + size * 0.02, shoulder_y),
    )
    path.curveToPoint_controlPoint1_controlPoint2_(
        (cx, top_y),
        (pad + size * 0.18, top_y + size * 0.02),
        (cx - size * 0.26, top_y + size * 0.02),
    )
    path.curveToPoint_controlPoint1_controlPoint2_(
        (size - pad - size * 0.08, top_y + size * 0.06),
        (cx + size * 0.26, top_y + size * 0.02),
        (size - pad - size * 0.18, top_y + size * 0.02),
    )
    path.curveToPoint_controlPoint1_controlPoint2_(
        (cx, tip_y),
        (size - pad - size * 0.02, shoulder_y),
        (cx + size * 0.02, tip_y - size * 0.08),
    )
    path.closePath()
    return path


def _draw_app_icon(pixels: int):
    """Return an ``NSImage`` ``pixels``×``pixels`` with blood-toned droplet + CGM stroke."""
    from AppKit import (
        NSBezierPath,
        NSColor,
        NSGradient,
        NSImage,
        NSLineCapStyleRound,
        NSLineJoinStyleRound,
    )
    from Foundation import NSMakeRect, NSSize

    size = float(pixels)
    img = NSImage.alloc().initWithSize_(NSSize(size, size))
    img.lockFocus()
    try:
        # Transparent corners (macOS rounds the dock tile; crisp silhouette still helps).
        NSColor.clearColor().setFill()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, size, size))

        drop = _droplet_path(size)
        # Deep crimson → slightly brighter red (reads as blood / glucose, not “error”).
        deep = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.05, 0.10, 1.0)
        mid = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.78, 0.12, 0.18, 1.0)
        grad = NSGradient.alloc().initWithStartingColor_endingColor_(deep, mid)
        grad.drawInBezierPath_angle_(drop, -45.0)

        # Soft specular edge (keeps shape readable at 16×16).
        NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.45, 0.42, 0.22).setStroke()
        hair = max(1.0, size * 0.018)
        drop.setLineWidth_(hair)
        drop.stroke()

        # Simple white trend line (abstract CGM curve) in the bulb.
        line = NSBezierPath.bezierPath()
        pad = size * 0.22
        w = size - 2 * pad
        h = size * 0.22
        base_y = pad + h * 0.55
        line.moveToPoint_((pad, base_y + h * 0.45))
        line.lineToPoint_((pad + w * 0.22, base_y + h * 0.05))
        line.lineToPoint_((pad + w * 0.45, base_y + h * 0.52))
        line.lineToPoint_((pad + w * 0.72, base_y + h * 0.0))
        line.lineToPoint_((pad + w, base_y + h * 0.38))
        NSColor.colorWithCalibratedWhite_alpha_(0.95, 0.92).setStroke()
        stroke_w = max(1.5, size * 0.055)
        line.setLineWidth_(stroke_w)
        line.setLineCapStyle_(NSLineCapStyleRound)
        line.setLineJoinStyle_(NSLineJoinStyleRound)
        line.stroke()
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
