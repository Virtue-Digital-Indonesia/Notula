"""
notula.appicon — the Dock/menu icon, drawn in code (a waveform on a blue tile).

Setting this at runtime (NSApplication.setApplicationIconImage_) makes the app
show as Notula with a real icon instead of the generic Python rocket, even
before it's bundled into a .app.
"""

from __future__ import annotations

from AppKit import NSImage, NSBezierPath, NSColor, NSGradient, NSMakeRect
from Foundation import NSMakeSize


def make_icon(size: float = 512.0) -> NSImage:
    img = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    img.lockFocus()

    # rounded-rect tile with a vertical blue gradient (Carbon Blue 60 → darker)
    inset = size * 0.055
    tile = NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset)
    radius = size * 0.225
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(tile, radius, radius)
    top = NSColor.colorWithSRGBRed_green_blue_alpha_(0.20, 0.54, 1.0, 1.0)
    bot = NSColor.colorWithSRGBRed_green_blue_alpha_(0.02, 0.19, 0.61, 1.0)
    NSGradient.alloc().initWithStartingColor_endingColor_(top, bot).drawInBezierPath_angle_(path, -90.0)

    # five white waveform bars (matches the header mark)
    NSColor.whiteColor().set()
    bars = [(0.30, 0.26), (0.415, 0.46), (0.53, 0.72), (0.645, 0.46), (0.76, 0.26)]
    bw = size * 0.058
    for xf, hf in bars:
        h = size * hf
        x = size * xf - bw / 2.0
        y = (size - h) / 2.0
        r = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x, y, bw, h), bw / 2.0, bw / 2.0)
        r.fill()

    img.unlockFocus()
    return img
