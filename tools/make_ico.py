#!/usr/bin/env python3
"""
Render assets/Notula.ico from the same artwork as the macOS icon.

    ./.venv/bin/python tools/make_ico.py

Without this, Notula.exe ships with PyInstaller's generic icon, and the app looks
like someone else's in the taskbar and the Start menu.

No Pillow needed. A Vista-or-later .ico is a 6-byte header, a 16-byte directory
entry per image, and then the images themselves — and each image is allowed to be
a PNG rather than a BMP, so the iconset PNGs go in verbatim. Sizes the iconset
doesn't already contain are produced with `sips`, which is why this is a macOS
tool; the .ico it writes is committed, so a Windows build never needs to run it.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICONSET = ROOT / "assets" / "Notula.iconset"
OUT = ROOT / "assets" / "Notula.ico"

# What Windows actually asks for: 16 in the title bar, 32 in the taskbar, 48 in
# Explorer's medium view, 256 for the extra-large view and the installer.
SIZES = [16, 24, 32, 48, 64, 128, 256]


def source_for(size: int, tmp: Path) -> bytes:
    """The PNG for one size — straight from the iconset when it has that size,
    otherwise downscaled from the largest artwork we have."""
    for name in (f"icon_{size}x{size}.png", f"icon_{size // 2}x{size // 2}@2x.png"):
        p = ICONSET / name
        if p.exists():
            return p.read_bytes()

    master = max(
        (p for p in ICONSET.glob("icon_*.png")),
        key=lambda p: p.stat().st_size, default=None)
    if master is None:
        sys.exit(f"no PNGs in {ICONSET} — run tools/make_icns.py first")
    dst = tmp / f"{size}.png"
    subprocess.run(["sips", "-z", str(size), str(size), str(master), "--out", str(dst)],
                   check=True, capture_output=True)
    return dst.read_bytes()


def main() -> int:
    if not ICONSET.is_dir():
        sys.exit(f"missing {ICONSET} — run tools/make_icns.py first")
    tmp = ROOT / "build" / "ico"
    tmp.mkdir(parents=True, exist_ok=True)

    images = [(s, source_for(s, tmp)) for s in SIZES]

    # ICONDIR, then one ICONDIRENTRY per image, then the image data
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 0 means 256: the field is one byte
            0 if size >= 256 else size,
            0,                            # palette size, 0 for truecolour
            0,                            # reserved
            1,                            # colour planes
            32,                           # bits per pixel
            len(data),
            offset,
        )
        blobs += data
        offset += len(data)

    OUT.write_bytes(header + entries + blobs)
    print(f"wrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB, "
          f"{len(images)} sizes: {', '.join(str(s) for s in SIZES)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
