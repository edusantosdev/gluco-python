"""Custom ``NSView`` that renders the recent glucose trace inside a menu item.

Lives in the menubar dropdown above the existing text rows. The drawing is
deliberately Spartan: faint horizontal threshold bands, a single polyline
through the cached readings, and a coloured dot on the latest sample. No
animation, no interaction — a glance graph, not an explorer.

Threading note
--------------

``setPoints_``/``setThresholds_`` may be called from any thread (rumps timer
ticks run on the main thread today, but we want this to stay safe if that
ever changes). They mutate plain Python attributes (atomic in CPython) and
then call ``setNeedsDisplay:``, which AppKit documents as thread-safe — the
actual ``drawRect:`` always runs on the main thread the next runloop pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import objc
from AppKit import (
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakePoint,
    NSMakeRect,
    NSRectFill,
    NSView,
)

# --- layout (in points) ---
# Plot area = view bounds minus padding. Left padding leaves room for a
# couple of mg/dL labels; bottom padding leaves room for the time axis.
_PAD_LEFT = 28
_PAD_RIGHT = 8
_PAD_TOP = 6
_PAD_BOTTOM = 16

_LINE_WIDTH = 1.5
_DOT_RADIUS = 3.0

# Y-axis bounds in mg/dL. Pinned (not auto-fit) so the graph reads
# consistently across glances — the eye learns where "high" is.
# 40–300 covers the clinical range; values outside are clamped, which
# is the right behaviour visually (an off-scale dot still pegs at the edge).
_Y_MIN = 40.0
_Y_MAX = 300.0


@dataclass(frozen=True)
class GraphThresholds:
    """mg/dL boundaries for the four colour bands."""

    low: float
    high: float
    very_high: float


class GlucoseGraphView(NSView):
    """Menu-item-embeddable view that draws the last ``hours`` of readings."""

    def initWithFrame_hours_(self, frame, hours):  # noqa: N802 - Obj-C selector
        self = objc.super(GlucoseGraphView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._points: list[tuple[float, float]] = []  # (unix_seconds, mg_dl)
        self._thresholds: GraphThresholds | None = None
        self._hours = int(hours)
        return self

    # --- public setters (any thread) ---

    def setPoints_(self, points):  # noqa: N802 - Obj-C selector
        """Replace the rendered series. ``points`` is iterable of ``(unix_s, mg_dl)``."""
        self._points = list(points or [])
        self.setNeedsDisplay_(True)

    def setThresholds_(self, thresholds):  # noqa: N802 - Obj-C selector
        self._thresholds = thresholds
        self.setNeedsDisplay_(True)

    # --- drawing ---

    def drawRect_(self, _dirty):  # noqa: N802 - Obj-C selector
        bounds = self.bounds()
        plot = NSMakeRect(
            _PAD_LEFT,
            _PAD_BOTTOM,
            bounds.size.width - _PAD_LEFT - _PAD_RIGHT,
            bounds.size.height - _PAD_TOP - _PAD_BOTTOM,
        )
        if plot.size.width <= 0 or plot.size.height <= 0:
            return

        self._draw_threshold_bands(plot)
        self._draw_y_labels(plot)
        self._draw_x_labels(plot)
        if not self._points:
            self._draw_empty(bounds)
            return
        self._draw_line(plot)
        self._draw_latest_dot(plot)

    # --- coordinate mapping ---

    def _y_for(self, plot, value: float) -> float:
        # Clamp into [Y_MIN, Y_MAX] so off-scale points peg to an edge.
        v = max(_Y_MIN, min(_Y_MAX, value))
        frac = (v - _Y_MIN) / (_Y_MAX - _Y_MIN)
        return plot.origin.y + frac * plot.size.height

    def _x_for(self, plot, ts_seconds: float, now_seconds: float) -> float:
        # Right edge = now; left edge = now - hours*3600.
        # Points older than the window get pegged to the left edge (we
        # don't expect this in practice; the daemon trims to ~12h).
        span = self._hours * 3600.0
        age = max(0.0, min(span, now_seconds - ts_seconds))
        frac = 1.0 - (age / span)
        return plot.origin.x + frac * plot.size.width

    # --- pieces ---

    def _draw_threshold_bands(self, plot) -> None:
        t = self._thresholds
        if t is None:
            return
        bands = (
            (_Y_MIN, t.low, NSColor.systemRedColor()),
            (t.low, t.high, NSColor.systemGreenColor()),
            (t.high, t.very_high, NSColor.systemYellowColor()),
            (t.very_high, _Y_MAX, NSColor.systemOrangeColor()),
        )
        for low, high, color in bands:
            if high <= low:
                continue
            y0 = self._y_for(plot, low)
            y1 = self._y_for(plot, high)
            color.colorWithAlphaComponent_(0.10).setFill()
            NSRectFill(NSMakeRect(plot.origin.x, y0, plot.size.width, y1 - y0))

    def _draw_y_labels(self, plot) -> None:
        t = self._thresholds
        if t is None:
            return
        attrs = self._small_text_attrs()
        for value in (t.low, t.high):
            y = self._y_for(plot, value)
            label = NSAttributedString.alloc().initWithString_attributes_(
                f"{int(value)}", attrs
            )
            size = label.size()
            label.drawAtPoint_(
                NSMakePoint(plot.origin.x - size.width - 3, y - size.height / 2)
            )

    def _draw_x_labels(self, plot) -> None:
        attrs = self._small_text_attrs()
        # Three ticks: full window back, halfway back, now. The middle label
        # collapses if the window is too short for cleanly halving.
        labels: list[tuple[float, str]] = [(0.0, f"-{self._hours}h")]
        if self._hours >= 2:
            labels.append((0.5, f"-{self._hours // 2}h"))
        labels.append((1.0, "now"))
        baseline_y = plot.origin.y - 12
        for frac, text in labels:
            x = plot.origin.x + frac * plot.size.width
            attr = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            size = attr.size()
            if frac == 0.0:
                draw_x = x
            elif frac == 1.0:
                draw_x = x - size.width
            else:
                draw_x = x - size.width / 2
            attr.drawAtPoint_(NSMakePoint(draw_x, baseline_y))

    def _draw_line(self, plot) -> None:
        now_s = datetime.now(UTC).timestamp()
        path = NSBezierPath.bezierPath()
        path.setLineWidth_(_LINE_WIDTH)
        # Round joins keep direction changes from looking jagged at this size.
        path.setLineJoinStyle_(1)  # NSLineJoinStyleRound
        first = True
        for ts_s, value in self._points:
            x = self._x_for(plot, ts_s, now_s)
            y = self._y_for(plot, value)
            point = NSMakePoint(x, y)
            if first:
                path.moveToPoint_(point)
                first = False
            else:
                path.lineToPoint_(point)
        NSColor.labelColor().setStroke()
        path.stroke()

    def _draw_latest_dot(self, plot) -> None:
        now_s = datetime.now(UTC).timestamp()
        ts_s, value = self._points[-1]
        x = self._x_for(plot, ts_s, now_s)
        y = self._y_for(plot, value)
        rect = NSMakeRect(
            x - _DOT_RADIUS, y - _DOT_RADIUS, _DOT_RADIUS * 2, _DOT_RADIUS * 2
        )
        NSBezierPath.bezierPathWithOvalInRect_(rect)
        # Fill colour matches the band the latest reading sits in, so the
        # "where am I right now" cue mirrors the menubar title colour.
        self._color_for_value(value).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(rect).fill()

    def _draw_empty(self, bounds) -> None:
        attrs = self._small_text_attrs(size=11)
        text = NSAttributedString.alloc().initWithString_attributes_(
            "No graph data yet", attrs
        )
        size = text.size()
        text.drawAtPoint_(
            NSMakePoint(
                (bounds.size.width - size.width) / 2,
                (bounds.size.height - size.height) / 2,
            )
        )

    # --- helpers ---

    def _color_for_value(self, value: float):
        t = self._thresholds
        if t is None:
            return NSColor.labelColor()
        if value < t.low:
            return NSColor.systemRedColor()
        if value > t.very_high:
            return NSColor.systemOrangeColor()
        if value > t.high:
            return NSColor.systemYellowColor()
        return NSColor.systemGreenColor()

    @staticmethod
    def _small_text_attrs(size: float = 9) -> dict:
        return {
            NSFontAttributeName: NSFont.systemFontOfSize_(size),
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
        }
