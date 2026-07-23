"""Render assets/Notula.icns from appicon.make_icon() (run once for the build)."""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from AppKit import NSApplication, NSBitmapImageRep  # noqa: E402
import appicon  # noqa: E402

try:
    from AppKit import NSBitmapImageFileTypePNG as PNG_TYPE
except ImportError:
    PNG_TYPE = 4  # NSPNGFileType

NSApplication.sharedApplication()  # a graphics context for offscreen drawing


def render_png(px, path):
    img = appicon.make_icon(px)
    rep = NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
    png = rep.representationUsingType_properties_(PNG_TYPE, {})
    png.writeToFile_atomically_(path, True)


def main():
    assets = os.path.join(ROOT, "assets")
    iconset = os.path.join(assets, "Notula.iconset")
    os.makedirs(iconset, exist_ok=True)
    specs = [
        (16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
        (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
        (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x"),
    ]
    for px, name in specs:
        render_png(px, os.path.join(iconset, f"icon_{name}.png"))
    icns = os.path.join(assets, "Notula.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    print("wrote", icns, os.path.getsize(icns), "bytes")


if __name__ == "__main__":
    main()
