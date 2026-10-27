#!/usr/bin/env python3
"""
video_index.py - shared, universal "is this a player video?" logic for the VFS.

Single source of truth for the question "will the video player window play this
file?". Used by vfs_handler.py so [VFS VIDEO OPEN] / [VFS VIDEO CLOSED] fire
exactly for the files the overlay player will play, and everything else
non-audio is logged as a plain [VFS PASSTHROUGH].

The logic mirrors the overlay's own scan (video_overlay.build_video_index() and
avi_scan_helper.build_patterns()) so both sides always agree:

  * Every *.mp4 inside the VFS source folder is a re-encoded video.
  * The dummy file the game opens = the mp4's basename + the ORIGINAL extension
    recorded in the sidecar <basename>.txt (defaults to "avi").
  * A file is "played by the player" when its path ends with <subdir>/<dummy>.

Universal by design: no hardcoded folder names (no "VIDEO", no game folders)
and no hardcoded video extensions - everything is derived at runtime from
scanning the source folder and the sidecar .txt files the re-encode tool
writes. When no mp4s are indexed, the legacy VIDEO\\*.avi pattern is used,
exactly like the overlay's scanner.
"""
import os
import re

_VIDEO_EXT = ".mp4"


def original_ext_of(mp4):
    """Return the ORIGINAL video file extension (lowercase, without the dot)
    recorded in the sidecar <basename>.txt written by the re-encode script.
    This is the extension of the dummy file the game actually opens (e.g.
    .avi) - it is whatever the source video was. Defaults to "avi" when the
    sidecar is missing or has no such line. Mirrors
    video_player.original_ext_of()."""
    txt = os.path.splitext(mp4)[0] + ".txt"
    try:
        with open(txt, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.lower().startswith("original file extension"):
                    ext = line.split(":", 1)[1].strip().lstrip(".").lower()
                    if ext:
                        return ext
    except OSError:
        pass
    return "avi"


def build_video_specs(source_dir):
    """Scan source_dir recursively and return the [[subdir, dummy_name], ...]
    scan specs the overlay player uses - exactly what
    video_overlay.build_video_index() computes. subdir is relative to
    source_dir ("" for files at the scan root); dummy_name is the file the
    game opens (mp4 basename + original extension from the sidecar .txt)."""
    specs = []
    for root, _dirs, files in os.walk(source_dir):
        for fn in files:
            if not fn.lower().endswith(_VIDEO_EXT):
                continue
            mp4 = os.path.join(root, fn)
            stem = fn[:-4]
            ext = original_ext_of(mp4)
            subdir = os.path.relpath(root, source_dir).replace("\\", "/")
            if subdir == ".":
                subdir = ""
            specs.append([subdir, stem + "." + ext])
    return specs


def video_scan_patterns(source_dir):
    """Compile the same regexes the overlay's handle scanner uses to decide a
    file is a video it will play (mirrors avi_scan_helper.build_patterns()):
    a path ending with <subdir>/<dummy>, case-insensitive. Falls back to the
    legacy VIDEO\\*.avi pattern when no mp4s are indexed."""
    specs = build_video_specs(source_dir)
    pats = []
    if specs:
        for subdir, fname in specs:
            if subdir:
                pats.append(re.compile(r"[\\/]" + re.escape(subdir) + r"[\\/]"
                                       + re.escape(fname) + r"$", re.I))
            else:
                pats.append(re.compile(r"[\\/]" + re.escape(fname) + r"$", re.I))
    else:
        pats.append(re.compile(r"[\\/]VIDEO[\\/]([^\\/]+\.avi)$", re.I))
    return pats
