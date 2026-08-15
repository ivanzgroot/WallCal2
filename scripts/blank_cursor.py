#!/usr/bin/env python3
"""
Generate a fully transparent Xcursor theme.

`unclutter` only works under X11. Raspberry Pi OS now defaults to labwc, a
Wayland compositor, where the pointer is drawn by the compositor itself and
there is no equivalent tool. Both X11 and wlroots-based compositors do honour
XCURSOR_THEME, so pointing them at a theme whose cursors are a single
transparent pixel hides the pointer everywhere.

Writes ~/.icons/wallcal-blank/ and prints the theme name.

Usage:  python3 scripts/blank_cursor.py [--name THEME] [--dir DIR]
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

# Xcursor on-disk format (see xcursor(3)):
#   file header : magic "Xcur", header size, version, table-of-contents count
#   TOC entry   : type, subtype (the nominal size), byte offset
#   image chunk : chunk header size, type, subtype, version,
#                 width, height, xhot, yhot, delay, then W*H ARGB pixels
MAGIC = 0x72756358           # "Xcur" read as a little-endian u32
FILE_HEADER_SIZE = 16
VERSION = 0x10000
CHUNK_TYPE_IMAGE = 0xFFFD0002
IMAGE_HEADER_SIZE = 36
TOC_ENTRY_SIZE = 12

#: Nominal sizes to publish. Themes advertise several so the compositor can
#: pick one for the current scale factor; every one of ours is blank anyway.
SIZES = (16, 24, 32, 48, 64, 96, 128)

#: Cursor names an application or compositor may ask for. Missing names fall
#: back to the default theme — and a visible pointer — so this list is
#: deliberately broad.
CURSOR_NAMES = (
    "left_ptr", "default", "arrow", "top_left_arrow", "pointer",
    "hand", "hand1", "hand2", "xterm", "text", "ibeam", "watch", "wait",
    "progress", "left_ptr_watch", "crosshair", "cross", "move", "fleur",
    "not-allowed", "help", "question_arrow", "grab", "grabbing",
    "all-scroll", "col-resize", "row-resize", "n-resize", "s-resize",
    "e-resize", "w-resize", "ne-resize", "nw-resize", "se-resize",
    "sw-resize", "sb_h_double_arrow", "sb_v_double_arrow",
)


def build_cursor_file(sizes=SIZES) -> bytes:
    """A cursor file holding one 1x1 fully transparent image per size."""
    count = len(sizes)
    toc_bytes = b""
    chunk_bytes = b""
    offset = FILE_HEADER_SIZE + TOC_ENTRY_SIZE * count

    for size in sizes:
        toc_bytes += struct.pack("<III", CHUNK_TYPE_IMAGE, size, offset + len(chunk_bytes))
        chunk_bytes += struct.pack(
            "<IIIIIIIII",
            IMAGE_HEADER_SIZE,   # chunk header size
            CHUNK_TYPE_IMAGE,    # type
            size,                # subtype: nominal size
            1,                   # image version
            1, 1,                # width, height
            0, 0,                # xhot, yhot
            0,                   # delay (not animated)
        ) + b"\x00\x00\x00\x00"  # one transparent ARGB pixel

    header = struct.pack("<IIII", MAGIC, FILE_HEADER_SIZE, VERSION, count)
    return header + toc_bytes + chunk_bytes


def install_theme(root: str, name: str) -> str:
    """Write the theme and return its directory."""
    theme_dir = os.path.join(root, name)
    cursors_dir = os.path.join(theme_dir, "cursors")
    os.makedirs(cursors_dir, exist_ok=True)

    with open(os.path.join(theme_dir, "index.theme"), "w") as fh:
        fh.write(f"[Icon Theme]\nName={name}\nComment=Invisible pointer for WallCal\n")

    payload = build_cursor_file()
    primary = os.path.join(cursors_dir, CURSOR_NAMES[0])
    with open(primary, "wb") as fh:
        fh.write(payload)

    # The rest are symlinks to the same blank cursor; fall back to copies on
    # filesystems that cannot link.
    for cursor_name in CURSOR_NAMES[1:]:
        target = os.path.join(cursors_dir, cursor_name)
        try:
            if os.path.lexists(target):
                os.unlink(target)
            os.symlink(CURSOR_NAMES[0], target)
        except OSError:
            with open(target, "wb") as fh:
                fh.write(payload)

    return theme_dir


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build a transparent cursor theme")
    parser.add_argument("--name", default="wallcal-blank", help="theme name")
    parser.add_argument("--dir", default=os.path.expanduser("~/.icons"),
                        help="icon directory to install into")
    args = parser.parse_args(argv)

    try:
        theme_dir = install_theme(args.dir, args.name)
    except OSError as exc:
        print(f"could not write the cursor theme: {exc}", file=sys.stderr)
        return 1

    print(args.name)
    print(f"installed at {theme_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
