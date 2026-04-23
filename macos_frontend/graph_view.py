"""Custom ``NSView`` that renders the recent glucose trace inside a menu item.

Lives in the menubar dropdown below the freshness text row. The drawing is
deliberately Spartan: faint horizontal threshold bands (targets implied by
the band edges, no numeric Y tick labels), a single polyline through the
cached readings, and a coloured dot on the latest sample. No
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

import logging
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

log = logging.getLogger("glucose.frontend.graph")

# --- layout (in points) ---
# The plot uses an inner rect: horizontal inset on left/right, vertical on
# top and bottom. The bottom band is reserved for time-axis labels only, so
# the chart runs from y = m_y to y = height − m_y.
_PLOT_EDGE_INSET_X = 16.0
_PLOT_EDGE_INSET_Y = 12.0

_LINE_WIDTH = 1.5
_DOT_RADIUS = 3.0
_THRESHOLD_GUIDE_WIDTH = 1.0
_THRESHOLD_GUIDE_DOT = 2.0
_THRESHOLD_GUIDE_GAP = 3.0
_BAND_ALPHA_BASE = 0.10
_BAND_ALPHA_ACTIVE = 0.24
_THRESHOLD_GUIDE_ALPHA = 0.55

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
        try:
            bounds = self.bounds()
            mx = _PLOT_EDGE_INSET_X
            my = _PLOT_EDGE_INSET_Y
            plot = NSMakeRect(
                mx,
                my,
                bounds.size.width - 2.0 * mx,
                bounds.size.height - 2.0 * my,
            )
            if plot.size.width <= 0 or plot.size.height <= 0:
                return

            self._draw_threshold_bands(plot)
            self._draw_threshold_guides(plot)
            if not self._points:
                self._draw_empty(bounds)
            else:
                self._draw_line(plot)
                self._draw_latest_dot(plot)
                # Bottom margin strip [0, m_y] — only when there is a trace to frame.
                self._draw_x_labels(plot)
        except Exception:  # noqa: BLE001
            # Never let a draw callback crash the whole menubar app.
            log.exception("Graph drawRect failed")

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
        active_zone = self._zone_key_for_value(self._points[-1][1]) if self._points else None
        bands = self._band_specs()
        for zone_key, low, high, color in bands:
            if high <= low:
                continue
            y0 = self._y_for(plot, low)
            y1 = self._y_for(plot, high)
            alpha = _BAND_ALPHA_ACTIVE if zone_key == active_zone else _BAND_ALPHA_BASE
            color.colorWithAlphaComponent_(alpha).setFill()
            NSRectFill(NSMakeRect(plot.origin.x, y0, plot.size.width, y1 - y0))

    def _draw_threshold_guides(self, plot) -> None:
        """Draw dotted guides on the active zone's lower/upper boundaries."""
        t = self._thresholds
        if t is None:
            return
        active_band = self._active_band_for_latest()
        if active_band is None:
            return
        _zone_key, low, high, color = active_band
        for value in (low, high):
            if not (_Y_MIN <= value <= _Y_MAX):
                continue
            y = self._y_for(plot, value)
            color.colorWithAlphaComponent_(_THRESHOLD_GUIDE_ALPHA).setStroke()
            x = plot.origin.x
            x_end = plot.origin.x + plot.size.width
            while x < x_end:
                seg = NSBezierPath.bezierPath()
                seg.setLineWidth_(_THRESHOLD_GUIDE_WIDTH)
                seg.moveToPoint_(NSMakePoint(x, y))
                seg.lineToPoint_(NSMakePoint(min(x + _THRESHOLD_GUIDE_DOT, x_end), y))
                seg.stroke()
                x += _THRESHOLD_GUIDE_DOT + _THRESHOLD_GUIDE_GAP

    def _draw_x_labels(self, plot) -> None:
        attrs = self._small_text_attrs()
        # Three ticks: full window back, halfway back, now. The middle label
        # collapses if the window is too short for cleanly halving.
        labels: list[tuple[float, str]] = [(0.0, f"-{self._hours}h")]
        if self._hours >= 2:
            labels.append((0.5, f"-{self._hours // 2}h"))
        labels.append((1.0, "Now"))
        # Sit inside the bottom margin band (height = _PLOT_EDGE_INSET_Y).
        sample = NSAttributedString.alloc().initWithString_attributes_(
            "-8h", attrs
        )
        label_h = float(sample.size().height)
        m_y = _PLOT_EDGE_INSET_Y
        axis_y = max(0.0, (m_y - label_h) / 2.0)
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
            attr.drawAtPoint_(NSMakePoint(draw_x, axis_y))

    def _draw_line(self, plot) -> None:
        now_s = datetime.now(UTC).timestamp()
        path = NSBezierPath.bezierPath()
        path.setLineWidth_(_LINE_WIDTH)
        # Round joins keep direction changes from looking jagged at this size.
        path.setLineJoinStyle_(1)  # NSLineJoinStyleRound
        left_x = plot.origin.x
        first = True
        for ts_s, value in self._points:
            x = self._x_for(plot, ts_s, now_s)
            y = self._y_for(plot, value)
            point = NSMakePoint(x, y)
            if first:
                # Oldest sample may be newer than "-{hours}h"; without a lead-in the
                # trace would start inset while "Now" still hugs the right edge.
                if x > left_x + 0.5:
                    path.moveToPoint_(NSMakePoint(left_x, y))
                    path.lineToPoint_(point)
                else:
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
        # Keep the latest point visually tied to the line colour.
        NSColor.labelColor().setFill()
        NSBezierPath.bezierPathWithOvalInRect_(rect).fill()

    def _draw_empty(self, bounds) -> None:
        attrs = self._small_text_attrs(size=11)
        text = NSAttributedString.alloc().initWithString_attributes_(
            "No graph data yet", attrs
        )
        size = text.size()
        mx = _PLOT_EDGE_INSET_X
        my = _PLOT_EDGE_INSET_Y
        inner_w = bounds.size.width - 2.0 * mx
        inner_h = bounds.size.height - 2.0 * my
        text.drawAtPoint_(
            NSMakePoint(
                mx + max(0.0, (inner_w - size.width) / 2.0),
                my + max(0.0, (inner_h - size.height) / 2.0),
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

    def _zone_key_for_value(self, value: float) -> str:
        t = self._thresholds
        if t is None:
            return "G"
        if value < t.low:
            return "L"
        if value > t.very_high:
            return "O"
        if value > t.high:
            return "Y"
        return "G"

    def _band_specs(self) -> tuple[tuple[str, float, float, NSColor], ...]:
        t = self._thresholds
        if t is None:
            return ()
        return (
            ("L", _Y_MIN, t.low, NSColor.systemRedColor()),
            ("G", t.low, t.high, NSColor.systemGreenColor()),
            ("Y", t.high, t.very_high, NSColor.systemYellowColor()),
            ("O", t.very_high, _Y_MAX, NSColor.systemOrangeColor()),
        )

    def _active_band_for_latest(self) -> tuple[str, float, float, NSColor] | None:
        if not self._points:
            return None
        active_zone = self._zone_key_for_value(self._points[-1][1])
        for band in self._band_specs():
            zone_key, low, high, _color = band
            if high <= low:
                continue
            if zone_key == active_zone:
                return band
        return None


    @staticmethod
    def _small_text_attrs(size: float = 9) -> dict:
        return {
            NSFontAttributeName: NSFont.systemFontOfSize_(size),
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
        }
