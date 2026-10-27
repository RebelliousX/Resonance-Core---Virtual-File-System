#!/usr/bin/env python3
"""
video_overlay.py - watches the game process and plays the real (MP4) video in
a borderless window over the dummy video the game is rendering.

The dummy videos the game opens are REAL files (hard links) in the game
directory: this game's 1998 AVI reader cannot play files served by the
WinFsp/FUSE drive (it parses the header + full index byte-perfectly and then
abandons the file without ever reading a frame - proven in the VFS
diagnostics log), while the same file as a real NTFS hard link plays fine.
So open/close detection works on the file system itself: this script keeps
one fully-shared read handle per dummy video and, once per poll, enumerates
the game process's handle table (NtQueryInformationProcess, class 51 - one
cheap call, ~0.02ms, constant in the number of dummies) and matches the
game's open file handles against the dummies by their object name (volume +
basename, read in memory via NtQueryObject - shared by every hard link to a
file, so the game opening the game-dir link matches this watch's
_vfs_data_ copy). When the game's PID appears, the dummy is open; when it
disappears, the dummy was closed. It then starts an in-process
pygame player (video_player.py) that
decodes the matching re-encoded mp4 with ffmpeg pipes and renders it into a
borderless window positioned exactly over the game window's client area,
while the audio plays through SDL_Mixer (pygame.mixer, which pygame-ce
bundles; WinMM waveOut is only the fallback).
The dummy videos the game actually opens are indexed from --source
(recursively) by pairing each mp4 with the sibling that shares its basename
but keeps the ORIGINAL extension from the sidecar .txt - so no "VIDEO" folder
name and no ".avi" extension is assumed.

VIRTUAL-VIDEO MODE (--video-events): an alternative detection backend. The
launcher junctions the game's video folder into the WinFsp/FUSE drive, the
VFS hides every matched .mp4/.txt set and serves each visible dummy
virtually; UniversalAudioVFS.open()/release() append "open"/"close" event
lines to _vfs_cache_\\video_events.jsonl and this script tails that log via
VirtualVideoWatch instead of constructing DummyWatch - so the class-51
handle-layout probing (candidates A/B/C), empirical layout discovery, the
fast NtQuery path and the class-47 fallback are all left unused at runtime.
Everything downstream (open debounce, close grace, players) is shared.

The player window type is selected with --window-type (see below): "child"
plays the video in a true WS_CHILD window of the game window (SetParent), and
"overlay" plays it in a separate top-level WS_EX_TOPMOST layered window that
floats over the game and follows its position/size on every frame. Either
way the window re-reads the game window's client geometry on every rendered
frame, so resizes and moves re-render seamlessly - playback never restarts.
(This is impossible with ffplay, which ignores externally-forced window
resizes; ffplay remains only as a fallback when pygame is not installed.)

Because the player window is WS_EX_NOACTIVATE it never takes keyboard focus:
the game keeps focus and Escape reaches the game directly. The player never
interprets Escape as a video-stop command; if the game handles Escape by
closing the dummy video, the watcher observes that close and stops the MP4.


Run with pythonw.exe so it survives launchers that kill python.exe.

Usage:
    video_overlay.py --source <root_dir_to_scan> [--process <game_exe,...>]
                     [--game <dir>] [--ffplay <path>] [--cache <path>]
                     [--maintain-aspect true|false] [--window-type child|overlay]
                     [--audio-type sdl_mixer|winmm] [--player-type pyav|ffmpeg]
                     [--sdl-audio-driver <driver>]
                     [--log <path>] [--diagnostics]

--window-type selects how the video player window is attached to the game:
  child   (default) - the video plays in a true WS_CHILD window of the game
          window (SetParent). It is clipped to the game's client area and
          moves/resizes with it automatically - the most seamless feel, the
          video looks like part of the game window. The player subclasses its
          WndProc to relay keys (Alt+Enter included) and clicks to the game.
          Caveat: a few games refuse to accept an external child window (the
          video may not appear, or the game may misbehave). If that happens,
          switch to "overlay".
  overlay - the video plays in a separate top-level WS_EX_TOPMOST layered
          window that floats exactly over the game window's client area and
          re-reads the game window's position and size on every rendered
          frame, so window moves and resizes are followed seamlessly. It is
          slightly less "attached" than a child window (it is a separate
          window on top of the game), but compatible with games that refuse
          child windows.
"""

import argparse
import ctypes
import ctypes.wintypes as w
import faulthandler
import json
import logging
import os
import re
import subprocess
import threading
import time

try:
    from video_player import (PygamePlayer, HAVE_PYGAME, META_CACHE,
                              warm_meta_cache, warm_ffmpeg, warm_pyav,
                              HAVE_PYAV, original_ext_of,
                              precreate_display, SHARED_DISPLAY)
except ImportError:  # video_player.py missing (or pygame not installed)
    PygamePlayer = None
    HAVE_PYGAME = False
    META_CACHE = {}
    warm_meta_cache = None
    warm_ffmpeg = None
    warm_pyav = None
    HAVE_PYAV = False
    original_ext_of = None
    precreate_display = None
    SHARED_DISPLAY = False

DEFAULT_FFPLAY = r"C:\Users\Thaer\AppData\Local\Microsoft\WinGet\Links\ffplay.exe"
# Game process name(s) to watch (lowercase exe filenames). Default is the
# Commandos BCD executable; the launcher passes the actual game exe it starts
# (e.g. comandos_w10.exe for Behind Enemy Lines) via --process (comma-separated
# list allowed, so a wrapper exe and the real game exe can both be watched).
PROCESS_NAMES = {"coman_mp.exe"}
_FIXED_PID = None  # test hook: --pid pins the watched process
# How the video player window attaches to the game (set from --window-type):
#   "child"   - true WS_CHILD of the game window (SetParent)
#   "overlay" - top-level floating WS_EX_TOPMOST window tracking the game
WINDOW_TYPE = "child"
# Which decoder decodes the h264/h265 mp4s (set from --player-type):
#   "pyav"   - in-process PyAV decode: no ffmpeg.exe spawn/warm-up at all,
#              so the first cutscene never waits on an external process.
#              Falls back to "ffmpeg" per player when PyAV is unavailable.
#   "ffmpeg" - classic external ffmpeg.exe rawvideo pipes + the asynchronous
#              ffmpeg warm-up job at startup (Windows exe/DLL cache).
PLAYER_TYPE = "pyav"
# Detection latency is part of A/V sync: the game starts its own (dummy)
# video playback the moment it opens the file, so the overlay must start the
# MP4 as fast as possible or the game's video ends first and cuts ours short.
# Open/close is detected by polling which processes hold a handle to each
# dummy (a per-process handle-table enumeration + an in-memory name match),
# which is why these two debounces exist:
# many games open AND close a dummy video several times in quick succession
# WITHOUT actually playing it (probing the file, reading its size/format,
# toggling it during setup) - a raw open/close is not a reliable
# "playback started/ended" signal. OPEN_DEBOUNCE requires the dummy to stay
# open for 25ms before the MP4 starts, so transient open/close blips are
# ignored and playback only begins when the open is genuine.
POLL_INTERVAL = 0.1
OPEN_DEBOUNCE = 0.025
# How long the dummy may stay fully closed before we stop its MP4. The same
# open/close flapping happens at the end: the game may close the dummy and
# immediately reopen it, or release several handles one by one, so stopping
# on the very first close killed the player mid-init and its window never
# appeared (audio-only). The grace lets a genuine close linger for 0.25s; if
# the dummy stays closed past that, playback is really over and the MP4
# stops (a reopen cancels the pending stop).
CLOSE_GRACE = 0.25
# How long a single handle-identity query may run before we give up on it.
# A first-touch NtQueryObject on some ANONYMOUS handles (pipes/devices) was
# measured to stall ~150-300s once on this machine; named files like the
# dummies are instant. The cap runs cold queries in a short-lived thread and
# abandons them, so the poll is never delayed by more than QUERY_CAP per new
# handle value. Already-touched handle values are provably instant and are
# queried directly (no thread).
QUERY_CAP = 0.25
# How many first-touch handle queries run concurrently per pass. The cold
# queries are the expensive ones (a stalled anonymous handle can sit in a
# capped thread for QUERY_CAP), so they are run in parallel in waves of this
# many. The whole cold pass has one shared QUERY_CAP deadline, so startup work
# cannot scale linearly with the game's unrelated handle count. Named dummy
# files normally answer in milliseconds; the short cap prevents a pathological
# unrelated handle from adding a visible startup delay.
COLD_QUERY_WAVE = 64
# How long a stalled handle value stays skipped before we dare retry it.
# The OS reuses handle values, so a value that once stalled could later
# refer to a real file; after POISON_TTL we re-query it (capped again).
POISON_TTL = 60.0
VIDEO_TITLE_PREFIX = "CommandosVideo_"

# SDL2 audio driver used for ffplay. On some machines the WASAPI backend
# mis-clocks the audio output (here it runs at ~0.63x, which makes the audio
# stutter and drags the video into slow motion because ffplay syncs video to
# the audio clock). DirectSound and winmm clock correctly; DirectSound is the
# default because it has lower latency than the winmm fallback.
FFPLAY_SDL_AUDIODRIVER = "directsound"

k32 = ctypes.WinDLL("kernel32")
user32 = ctypes.WinDLL("user32")

PROCESS_QUERY_INFORMATION = 0x400
CREATE_NO_WINDOW = 0x08000000

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
HWND_TOP = ctypes.c_void_p(0)
HWND_TOPMOST = ctypes.c_void_p(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x10
SWP_SHOWWINDOW = 0x40
SWP_FRAMECHANGED = 0x20

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012
VK_ESCAPE = 0x1B
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12       # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C

k32.OpenProcess.restype = ctypes.c_void_p
k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.GetExitCodeProcess.restype = w.BOOL
k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD)]
STILL_ACTIVE = 259

WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, ctypes.c_void_p, ctypes.c_void_p)
user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
user32.EnumWindows.restype = w.BOOL
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD)]
user32.GetWindowThreadProcessId.restype = w.DWORD
user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetWindowLongW.restype = w.LONG
user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, w.LONG]
user32.SetWindowLongW.restype = w.LONG
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = w.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, w.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

# --- Handle-holder detection (DummyWatch) ------------------------------
# Fast path: enumerate the GAME PROCESS's handle table once per poll
# (NtQueryInformationProcess, ProcessHandleInformation - class 51; measured
# ~0.02ms, independent of the number of dummies), keep only its File-type
# handles, duplicate each into this process and read its object NAME from
# memory (NtQueryObject, ObjectNameInformation - no disk I/O, cannot block).
# A dummy is open when a game handle's name matches the dummy's volume and
# basename - true for every hard link to the same file, so the game opening
# the game-dir link matches this watch's _vfs_data_ handle. (The file-ID
# query FileInternalInformation was the first choice but intermittently
# stalled ~90-150s on some handles on this machine - enough to freeze the
# poll loop - so the in-memory name query is used instead.) The old
# per-dummy query (NtQueryInformationFile, FileProcessIdsUsingFileInformation
# - class 47) is kept as a fallback: it walks the whole system's handle
# tables PER FILE (~32ms each here - 13 dummies ~ 430ms/poll, 400 dummies
# ~ 12.8s/poll).
ntdll = ctypes.WinDLL("ntdll")


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_long), ("Information", ctypes.c_void_p)]


class _FILE_PROCESS_IDS(ctypes.Structure):
    _fields_ = [("NumberOfProcessIdsInBuffer", w.DWORD),
                ("ProcessIdList", ctypes.c_ulonglong * 64)]


ntdll.NtQueryInformationFile.restype = ctypes.c_long
ntdll.NtQueryInformationFile.argtypes = [
    ctypes.c_void_p,                    # FileHandle
    ctypes.POINTER(_IO_STATUS_BLOCK),   # IoStatusBlock
    ctypes.c_void_p,                    # FileInformation
    w.DWORD,                            # Length
    w.DWORD,                            # FileInformationClass
]

# Per-process handle table (ProcessHandleInformation, class 51).
# The entry layout is UNDOCUMENTED Native API and differs between Windows
# builds - even the SDK's winternl.h layout does not match reality on this
# machine. Instead of hardcoding one build's layout, DummyWatch probes the
# candidates below against its own dummy handles at startup (see
# _discover_layout) and uses whichever layout actually matches.
#
# Candidate A - the layout measured on this machine: 40-byte entries with
# HandleValue as a full ULONG_PTR at offset 0 (verified empirically against
# handles opened with CreateFileW; the documented USHORT layout reads
# zeros). Matches phnt's current PROCESS_HANDLE_TABLE_ENTRY_INFO exactly
# (winsiderss/phnt ntpsapi.h). ULONG_PTR fields are c_size_t, so this same
# definition adapts to 32-bit Python (28-byte entries).
class _PROCESS_HANDLE_TABLE_ENTRY_INFO_A(ctypes.Structure):
    _fields_ = [("HandleValue", ctypes.c_size_t),     # 0  ULONG_PTR
                ("HandleCount", ctypes.c_size_t),     # 8
                ("PointerCount", ctypes.c_size_t),    # 16
                ("GrantedAccess", w.DWORD),           # 24
                ("ObjectTypeIndex", w.WORD),          # 28
                ("_u3", w.WORD),                      # 30
                ("_u4", w.DWORD),                     # 32
                ("_u5", w.DWORD)]                     # 36


# Candidate B - a commonly-circulated USHORT-handle variant (undocumented;
# not in any Microsoft or MinGW winternl.h - only phnt-style reverse-engineered headers): 20-byte entries, USHORT handle.
class _PROCESS_HANDLE_TABLE_ENTRY_INFO_B(ctypes.Structure):
    _fields_ = [("HandleValue", w.WORD),              # 0  USHORT
                ("HandleCount", w.WORD),              # 2
                ("PointerCount", w.WORD),             # 4
                ("_pad", w.WORD),                     # 6
                ("GrantedAccess", w.DWORD),           # 8
                ("ObjectTypeIndex", w.WORD),          # 12
                ("HandleAttributes", w.WORD),         # 14
                ("Reserved", w.DWORD)]                # 16


# Candidate C - the older Process Hacker (sourceforge-era) layout: HANDLE-
# sized handle value, ULONG_PTR counters, then a CreatorBackTraceIndex field
# (44 bytes on 64-bit). Current phnt and this machine's kernel omit it.
class _PROCESS_HANDLE_TABLE_ENTRY_INFO_C(ctypes.Structure):
    _fields_ = [("HandleValue", ctypes.c_size_t),     # 0  HANDLE (ULONG_PTR)
                ("HandleCount", ctypes.c_size_t),     # 8
                ("PointerCount", ctypes.c_size_t),    # 16
                ("GrantedAccess", w.DWORD),           # 24
                ("CreatorBackTraceIndex", w.DWORD),   # 28
                ("ObjectTypeIndex", w.DWORD),         # 32
                ("HandleAttributes", w.DWORD),        # 36
                ("Reserved", w.DWORD)]                # 40


# Ordered probe list: tried in this order; the layout that matches the most
# of our own known dummy handles (with sane field values) wins.
_LAYOUT_CANDIDATES = [
    _PROCESS_HANDLE_TABLE_ENTRY_INFO_A,
    _PROCESS_HANDLE_TABLE_ENTRY_INFO_B,
    _PROCESS_HANDLE_TABLE_ENTRY_INFO_C,
]


class _PROCESS_HANDLE_SNAPSHOT_INFORMATION(ctypes.Structure):
    _fields_ = [("NumberOfHandles", ctypes.c_size_t),
                ("Reserved", ctypes.c_size_t),
                ("Handles", _PROCESS_HANDLE_TABLE_ENTRY_INFO_A * 1)]


class _Layout(object):
    """Decoded class-51 layout: how to walk the snapshot entries.

    struct_type: the ctypes entry struct when a named candidate matched,
    else None (empirically derived layout). header: snapshot header bytes
    (8 or 16). stride: bytes per entry. hv_off: offset of HandleValue within
    an entry. type_index: File object type index, or None (no type filter).
    type_index_off: offset of ObjectTypeIndex within an entry, or None.
    """
    __slots__ = ("struct_type", "header", "stride", "hv_off",
                 "type_index", "type_index_off")

    def __init__(self, struct_type, header, stride, hv_off,
                 type_index, type_index_off):
        self.struct_type = struct_type
        self.header = header
        self.stride = stride
        self.hv_off = hv_off
        self.type_index = type_index
        self.type_index_off = type_index_off


class _RawEntry(object):
    """Minimal decoded entry for empirically-derived layouts (no matching
    ctypes struct): the handle value plus an optional File type index."""

    __slots__ = ("HandleValue", "ObjectTypeIndex")

    def __init__(self, handle_value, object_type_index):
        self.HandleValue = handle_value
        self.ObjectTypeIndex = object_type_index


ntdll.NtQueryInformationProcess.restype = ctypes.c_long
ntdll.NtQueryInformationProcess.argtypes = [
    ctypes.c_void_p,                    # ProcessHandle
    w.DWORD,                            # ProcessInformationClass
    ctypes.c_void_p,                    # ProcessInformation
    w.DWORD,                            # ProcessInformationLength
    ctypes.POINTER(w.DWORD),            # ReturnLength
]
ntdll.NtDuplicateObject.restype = ctypes.c_long
ntdll.NtDuplicateObject.argtypes = [
    ctypes.c_void_p,                    # SourceProcessHandle
    ctypes.c_void_p,                    # SourceHandle
    ctypes.c_void_p,                    # TargetProcessHandle
    ctypes.POINTER(ctypes.c_void_p),    # TargetHandle
    w.DWORD,                            # DesiredAccess
    w.DWORD,                            # HandleAttributes
    w.DWORD,                            # Options
]
ntdll.NtClose.restype = ctypes.c_long
ntdll.NtClose.argtypes = [ctypes.c_void_p]
ntdll.NtQueryObject.restype = ctypes.c_long
ntdll.NtQueryObject.argtypes = [
    ctypes.c_void_p,                    # Handle
    w.DWORD,                            # ObjectInformationClass
    ctypes.c_void_p,                    # ObjectInformation
    w.DWORD,                            # ObjectInformationLength
    ctypes.POINTER(w.DWORD),            # ReturnLength
]
k32.GetCurrentProcess.restype = ctypes.c_void_p
k32.GetCurrentProcess.argtypes = []

k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                            w.DWORD, w.DWORD, ctypes.c_void_p]
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.IsWindowVisible.restype = w.BOOL
user32.IsWindow.argtypes = [ctypes.c_void_p]
user32.IsWindow.restype = w.BOOL
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.RECT)]
user32.GetWindowRect.restype = w.BOOL
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.restype = w.BOOL
user32.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
user32.AttachThreadInput.restype = w.BOOL
user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
user32.BringWindowToTop.restype = w.BOOL
user32.SetParent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.SetParent.restype = ctypes.c_void_p
user32.GetParent.argtypes = [ctypes.c_void_p]
user32.GetParent.restype = ctypes.c_void_p
user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.RECT)]
user32.GetClientRect.restype = w.BOOL


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t),
                ("time", w.DWORD),
                ("pt", w.POINT)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", w.DWORD),
                ("scanCode", w.DWORD),
                ("flags", w.DWORD),
                ("time", w.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD),
                ("flags", w.DWORD),
                ("hwndActive", ctypes.c_void_p),
                ("hwndFocus", ctypes.c_void_p),
                ("hwndCapture", ctypes.c_void_p),
                ("hwndMenuOwner", ctypes.c_void_p),
                ("hwndMoveSize", ctypes.c_void_p),
                ("hwndCaret", ctypes.c_void_p),
                ("rcCaret", w.RECT)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t)

user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.POINT)]
user32.ClientToScreen.restype = w.BOOL
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, w.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = w.BOOL
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.PostThreadMessageW.argtypes = [w.DWORD, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
user32.PostThreadMessageW.restype = w.BOOL
k32.GetCurrentThreadId.restype = w.DWORD
user32.GetGUIThreadInfo.argtypes = [w.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = w.BOOL
user32.SetFocus.argtypes = [ctypes.c_void_p]
user32.SetFocus.restype = ctypes.c_void_p


def dbg(msg):
    """Diagnostic logging (enabled via PLAYER_DEBUG env var, same as the
    player) - used to measure detection/startup latency."""
    f = os.environ.get("PLAYER_DEBUG")
    if not f:
        return
    try:
        with open(f, "a", buffering=1) as fh:
            fh.write("%.3f %s\n" % (time.time(), msg))
    except OSError:
        pass


def _process_is_running(h):
    """True if the process behind handle h is still running. OpenProcess on a
    dead pid can still SUCCEED while the process object is referenced by an
    open handle (the DummyWatch worker keeps one on the game), so a successful
    open is not enough - the exit code must be checked: a terminated process
    reports a real exit code, a running one reports STILL_ACTIVE (259)."""
    code = w.DWORD()
    if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
        return True  # cannot tell; assume running
    return code.value == STILL_ACTIVE


def find_game_pid():
    if _FIXED_PID is not None:
        # Test hook (--pid): watch a specific process instead of the game exe.
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, _FIXED_PID)
        if h:
            try:
                if _process_is_running(h):
                    return _FIXED_PID
                return None
            finally:
                k32.CloseHandle(ctypes.c_void_p(h))
        return None
    h = k32.CreateToolhelp32Snapshot(0x2, 0)
    if h == -1:
        return None
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        pid = None
        if k32.Process32FirstW(h, ctypes.byref(e)):
            while True:
                if e.szExeFile.lower() in PROCESS_NAMES:
                    pid = e.th32ProcessID
                    break
                if not k32.Process32NextW(h, ctypes.byref(e)):
                    break
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))
    return pid




# Video open/close detection polls the real dummy files the game plays (see
# DummyWatch): the game's 1998 AVI reader cannot play files served by the
# WinFsp/FUSE drive (it parses the header + index then abandons the file
# without reading a frame), so the dummies are real hard-linked files again
# and the signal comes from the file system itself.
# Video index: dummy name (e.g. "H_Intro.avi") -> {"mp4": abs path, "subdir":
# relative dir under source, "" if at root}. Built by build_video_index().
VIDEO_INDEX = {}
PATH_INDEX = {}  # dummy rel path (lower) -> dummy name, for VFS open events


def find_ffplay(explicit):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    p = __import__("shutil").which("ffplay")
    if p:
        return p
    return DEFAULT_FFPLAY if os.path.isfile(DEFAULT_FFPLAY) else None


def find_tool(name):
    """Resolve an ffmpeg-family tool: PATH first, then next to the default
    ffplay (the WinGet links dir the launcher puts on PATH)."""
    p = __import__("shutil").which(name)
    if p:
        return p
    d = os.path.join(os.path.dirname(DEFAULT_FFPLAY), name + ".exe")
    return d if os.path.isfile(d) else None


def ddraw_maintas_false(game_dir):
    """Return True if cnc-ddraw's ddraw.ini exists in game_dir and its
    effective 'maintas' (maintain aspect ratio) setting is false. A missing
    ini, or maintas being anything but an explicit false, returns False."""
    if not game_dir:
        return False
    ini_path = os.path.join(game_dir, "ddraw.ini")
    if not os.path.isfile(ini_path):
        return False
    try:
        with open(ini_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False

    # cnc-ddraw layout: the [ddraw] section holds defaults, and a game-specific
    # section named after the executable (without .exe) overrides them.
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            current = m.group(1).strip().lower()
            sections.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        sections[current][key.strip().lower()] = value.strip().lower()

    maintas = sections.get("ddraw", {}).get("maintas")
    # A game-specific section named after the executable (without .exe)
    # overrides the [ddraw] defaults; check every watched process name.
    for name in PROCESS_NAMES:
        exe_section = sections.get(name[:-4].lower())
        if exe_section and "maintas" in exe_section:
            maintas = exe_section["maintas"]

    return maintas == "false"


def should_stretch(game_dir, maintain_aspect=True):
    """Decide whether the overlay video should be stretched to fill the game
    window.

    maintain_aspect is the launcher's "maintain videos playback aspect ratio"
    flag, defaulting to True (letterbox, keep the source aspect ratio). Setting
    it to False always stretches.

    Precedence: when maintain_aspect is True but ddraw.ini (cnc-ddraw) is
    present with maintas=false, ddraw.ini takes precedence and the video is
    stretched, because the game itself is being stretched and the overlay must
    match it."""
    if not maintain_aspect:
        return True
    return ddraw_maintas_false(game_dir)


def _parse_bool(value, default=True):
    """Parse a true/false-ish CLI value passed by the launcher into a bool."""
    if value is None:
        return default
    v = str(value).strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return default


def build_video_index(source_dir):
    """Scan source_dir recursively and index every re-encoded mp4 next to its
    original-format sibling (the dummy the game opens). Returns
    {dummy_name: {"mp4": abs_path, "subdir": rel_dir}} and fills PATH_INDEX
    with {dummy_rel_path_lower: dummy_name} for resolving the VFS's open
    events (their paths are lowercased; the names keep the on-disk case).

    The dummy file is the mp4's basename + the ORIGINAL extension from the
    sidecar .txt (e.g. H_Intro.avi), and it can live in any subfolder - there
    is no hardcoded "VIDEO" directory and no hardcoded ".avi" extension.
    """
    global VIDEO_INDEX, PATH_INDEX
    index = {}
    path_index = {}
    for root, _dirs, files in os.walk(source_dir):
        for fn in files:
            if not fn.lower().endswith(".mp4"):
                continue
            mp4 = os.path.join(root, fn)
            stem = fn[:-4]
            ext = original_ext_of(mp4) if original_ext_of else "avi"
            subdir = os.path.relpath(root, source_dir).replace("\\", "/")
            if subdir == ".":
                subdir = ""
            dummy = stem + "." + ext
            index[dummy] = {"mp4": mp4, "subdir": subdir}
            rel = (subdir + "/" + dummy) if subdir else dummy
            path_index[rel.lower()] = dummy
    VIDEO_INDEX = index
    PATH_INDEX = path_index
    return index


def find_game_main_window(game_pid):
    """Return the game's visible top-level window (ignores IME/hidden hosts)."""
    win = None

    @WNDENUMPROC
    def cb(hwnd, lparam):
        nonlocal win
        wp = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wp))
        if wp.value != game_pid or not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        t = title.value
        if t and t not in ("ActiveMovie Window", "MSCTFIME UI", "Default IME"):
            win = hwnd
        return True

    user32.EnumWindows(cb, None)
    return win


def find_video_window(ffplay_pid):
    """Find the ffplay top-level window by title prefix, falling back to any
    visible window of that process."""
    win = None
    fallback = None

    @WNDENUMPROC
    def cb(hwnd, lparam):
        nonlocal win, fallback
        wp = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wp))
        if wp.value != ffplay_pid:
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if title.value.startswith(VIDEO_TITLE_PREFIX):
            win = hwnd
            return False
        if fallback is None and user32.IsWindowVisible(hwnd):
            fallback = hwnd
        return True

    user32.EnumWindows(cb, None)
    return win if win else fallback


def _game_thread_has_focus(gw):
    """True if the GAME's window thread currently holds keyboard focus.
    GetForegroundWindow() is NOT a reliable proxy here: once the pygame
    player window is a WS_CHILD of the game, the game is the top-level
    ancestor of the focused child, so GetForegroundWindow() reports the game
    even though keyboard focus sits on the player child - and the game only
    honors Alt+Enter/ESC when IT owns the keyboard focus.

    GetGUIThreadInfo on the game's thread reports the focused window even
    when it is a DESCENDANT of the game window - which includes our pygame
    child. So the focused window must additionally be owned by the game's
    thread: hwndFocus is then the game window itself (or one of the game's
    own child windows), never our player window (a different thread)."""
    gw_tid = user32.GetWindowThreadProcessId(gw, None)
    if not gw_tid:
        return False
    gui = GUITHREADINFO()
    gui.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(gw_tid, ctypes.byref(gui)):
        return False
    if not gui.hwndFocus:
        return False
    focus_tid = user32.GetWindowThreadProcessId(gui.hwndFocus, None)
    return focus_tid == gw_tid


def focus_game_window(game_pid, timeout=4.0):
    """Give the game window KEYBOARD focus when playback starts so the user
    can press Alt+Enter (fullscreen toggle) and ESC without clicking the game
    first - the game only acts on them when its thread holds keyboard focus.
    The overlay is a background pythonw process, so the usual foreground-lock
    restriction must be bypassed.

    Runs in a background thread and retries until the game's thread actually
    holds keyboard focus (up to timeout seconds): SDL's set_mode steals focus
    when the player window is created (after this is first called), so a
    single attempt is unreliable - it must keep trying until the game owns
    focus again. Exits as soon as it does, so it never fights the user."""

    def worker():
        deadline = time.time() + timeout
        while time.time() < deadline:
            gw = find_game_main_window(game_pid)
            if not gw:
                time.sleep(0.1)
                continue
            if _game_thread_has_focus(gw):
                return
            try:
                # NO synthetic keypress here. The old fake-Alt trick (inject
                # Alt down/up to satisfy the foreground lock) made the GAME
                # skip its cutscene: Commandos treats a keypress during a
                # video as "skip", so the dummy AVI playback ended instantly,
                # the game dropped to the menu (menu music) while the overlay
                # MP4 kept playing. AttachThreadInput to the foreground
                # thread's input queue below already grants SetForegroundWindow
                # input access WITHOUT producing any input event, so the fake
                # key is neither needed nor safe.
                fg = user32.GetForegroundWindow()
                fg_thread = user32.GetWindowThreadProcessId(fg, None)
                gw_thread = user32.GetWindowThreadProcessId(gw, None)
                our_thread = k32.GetCurrentThreadId()
                attached = []
                # Sharing the foreground thread's input queue makes
                # SetForegroundWindow succeed even from a background process
                # (the queue share counts as input access).
                if fg_thread and fg_thread != our_thread:
                    if user32.AttachThreadInput(our_thread, fg_thread, True):
                        attached.append(fg_thread)
                if gw_thread and gw_thread != our_thread and gw_thread != fg_thread:
                    if user32.AttachThreadInput(our_thread, gw_thread, True):
                        attached.append(gw_thread)
                user32.BringWindowToTop(gw)
                user32.SetForegroundWindow(gw)
                # Move KEYBOARD focus to the game window itself. This is the
                # part that makes Alt+Enter/ESC reach the game: foreground
                # alone is not enough once the player is a child of the game.
                if gw_thread:
                    user32.SetFocus(gw)
                for t in reversed(attached):
                    user32.AttachThreadInput(our_thread, t, False)
                if _game_thread_has_focus(gw):
                    dbg("focus: game window %s has keyboard focus" % gw)
                    return
            except Exception:
                pass
            time.sleep(0.1)

    if os.environ.get("OVERLAY_NO_FOCUS"):
        return  # bisect: focus stealing disabled
    threading.Thread(target=worker, daemon=True).start()





class OverlayState:
    def __init__(self):
        self.overlay_hwnds = {}
        self.game_win = None
        self.playing = False
        self.last_embed = {}
        self.gone_since = {}   # avi_name -> time its dummy last fully closed
        self.overlay = None


state = OverlayState()



def set_dpi_aware():
    """Run in physical pixel space (like SDL2/ffplay) so window coordinates are
    consistent between the watcher, ffplay and the (cnc-ddraw) game window."""
    try:
        sdpac = ctypes.WinDLL("user32", use_last_error=True).SetProcessDpiAwarenessContext
        sdpac.restype = w.BOOL
        sdpac.argtypes = [ctypes.c_void_p]
        PROCESS_PER_MONITOR_DPI_AWARE = ctypes.c_void_p(-4)
        if not sdpac(PROCESS_PER_MONITOR_DPI_AWARE):
            ctypes.WinDLL("user32").SetProcessDPIAware()
    except Exception:
        try:
            ctypes.WinDLL("user32").SetProcessDPIAware()
        except Exception:
            pass


def embed_overlay(ffplay_pid, game_pid, force=False):
    """Keep the ffplay window attached to the game window's client area.

    When the window type is "child" (the default; OVERLAY_FORCE_FFPLAY_CHILD
    forces it for bisect), the window is made a true WS_CHILD of the game
    window (SetParent). Otherwise (window type "overlay") it stays a top-level
    WS_EX_TOPMOST | WS_EX_NOACTIVATE window covering the game window's full
    client area (a top-level window is composited by DWM above the game
    window, so there is no parent/child repaint fight). The window is only
    moved/resized when its position/size is actually wrong; the Z-order is
    re-asserted every poll with SWP_NOMOVE|SWP_NOSIZE, so it neither slips
    behind the game nor flickers. Each player's window is tracked by its pid."""
    game_win = find_game_main_window(game_pid)
    if not game_win:
        return False
    state.game_win = game_win
    hwnd = state.overlay_hwnds.get(ffplay_pid)
    if not hwnd or not user32.IsWindow(hwnd):
        hwnd = find_video_window(ffplay_pid)
        if not hwnd:
            return False
        state.overlay_hwnds[ffplay_pid] = hwnd
        logging.info("adopted window pid=%s hwnd=%s", ffplay_pid, hwnd)

    # Target: the game window's full client area.
    crect = w.RECT()
    user32.GetClientRect(game_win, ctypes.byref(crect))
    cw, ch = crect.right, crect.bottom
    if cw <= 0 or ch <= 0:
        return False

    if WINDOW_TYPE == "child" or os.environ.get("OVERLAY_FORCE_FFPLAY_CHILD"):
        # True child: SetParent + WS_CHILD + no-activate. Child coordinates
        # are parent-client-relative, so position is always (0, 0) and only
        # the size is set. This is the setup that never froze the game.
        if user32.GetParent(hwnd) != game_win:
            user32.SetParent(hwnd, game_win)
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        user32.SetWindowLongW(hwnd, GWL_STYLE,
                              (style | WS_CHILD) & ~WS_POPUP)
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                              (ex | WS_EX_NOACTIVATE) & ~WS_EX_TOPMOST)
        vrect = w.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(vrect))
        ok = (vrect.right - vrect.left == cw and vrect.bottom - vrect.top == ch)
        if force or not ok:
            user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, cw, ch,
                                SWP_SHOWWINDOW | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        else:
            # The game's resize/fullscreen code can re-raise its own window
            # without changing the final size. Repair that sibling ordering on
            # every inexpensive watcher poll, not only on a size change.
            user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
                                SWP_SHOWWINDOW)
        if user32.GetForegroundWindow() == hwnd:
            user32.SetForegroundWindow(game_win)
        now = time.time()
        if now - state.last_embed.get(ffplay_pid, 0) > 2.5:
            state.last_embed[ffplay_pid] = now
            logging.info("embed-child pid=%s hwnd=%s size=%dx%d",
                         ffplay_pid, hwnd, cw, ch)
        return True

    # Topmost + no-activate: stays above the game but never steals its focus.
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    want_ex = WS_EX_TOPMOST | WS_EX_NOACTIVATE
    changed = (ex & want_ex) != want_ex
    if changed:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | want_ex)

    # Where the client area sits on screen vs. where the window currently is.
    origin = w.POINT(0, 0)
    user32.ClientToScreen(game_win, ctypes.byref(origin))
    vrect = w.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(vrect))
    ok = (vrect.left == origin.x and vrect.top == origin.y and
          vrect.right == origin.x + cw and vrect.bottom == origin.y + ch)

    if force or changed or not ok:
        flags = SWP_SHOWWINDOW | SWP_NOACTIVATE
        if force or changed:
            flags |= SWP_FRAMECHANGED
        user32.SetWindowPos(hwnd, HWND_TOPMOST, origin.x, origin.y, cw, ch, flags)

    # Re-assert the topmost band every poll without moving/resizing. This is
    # local to the ffplay window, so it does not block on the game's thread and
    # cannot reproduce the old playback-stutter path.
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
                        SWP_SHOWWINDOW)

    if user32.GetForegroundWindow() == hwnd:
        user32.SetForegroundWindow(game_win)

    now = time.time()
    if now - state.last_embed.get(ffplay_pid, 0) > 2.5:
        state.last_embed[ffplay_pid] = now
        logging.info("embed pid=%s hwnd=%s rect=(%d,%d,%d,%d)",
                     ffplay_pid, hwnd, vrect.left, vrect.top, vrect.right, vrect.bottom)
    return True


def load_video_meta_cache(cache_path):
    """Load the VFS's pre-probed video metadata (video_meta.json, written
    next to the index cache during indexing) into META_CACHE so playback
    never runs ffprobe mid-game. Entries are keyed by dummy rel path; META_
    CACHE is keyed by dummy basename (same as warm_meta_cache)."""
    if not cache_path:
        return
    meta_path = os.path.join(os.path.dirname(os.path.abspath(cache_path)),
                             "video_meta.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(entries, dict):
        return
    for rel, e in entries.items():
        if not isinstance(e, dict):
            continue
        w, h, fps = e.get("width"), e.get("height"), e.get("fps")
        if not w or not h or not fps:
            continue
        name = os.path.basename(rel.replace("/", os.sep))
        if name not in META_CACHE:
            META_CACHE[name] = (w, h, fps, e.get("duration"))


class DummyWatch:
    """Detects when the game opens/closes the dummy video files.

    The game cannot PLAY a dummy served by the VFS/FUSE drive: its 1998 AVI
    reader parses the header + full index byte-perfectly and then abandons
    the file without ever reading a frame (proven in the VFS diagnostics
    log), while the same file as a real NTFS hard link plays fine. So the
    dummies are real files again, and the open/close signal comes from the
    file system itself: this overlay keeps one fully-shared read handle per
    dummy (passive - the share mask never interferes with the game's opens)
    and, once per poll, enumerates the game process's handle table
    (NtQueryInformationProcess, class 51) to see which File-type handles the
    game currently holds, matching them to the dummies by object name:
    volume + basename, read in memory with NtQueryObject
    (ObjectNameInformation). The name is the same for every hard link to
    the same file, so the game opening the game-dir link matches this
    watch's _vfs_data_ copy. If the game holds a dummy's name, the dummy is
    open; when it no longer does, the dummy was closed.

    Cost is constant in the number of dummies (one cheap per-process
    enumeration + one in-memory name query per game file handle), unlike the
    old per-dummy NtQueryInformationFile class-47 query which walked the
    whole system's handle tables once per dummy (~32ms each - ~430ms/poll
    for 13 dummies, ~12.8s/poll for 400). Class 47 remains as the fallback
    path.

    The computation runs in a worker thread: held_by() only reads the latest
    cached snapshot, so the poll loop can never block. This matters because
    a first-touch NtQueryObject on some ANONYMOUS handles (pipes/devices)
    was measured to stall ~150-300s on this machine - named files like the
    dummies never stall, and after a handle's first query it is instant -
    but even if a stall happens, the overlay keeps running on the previous
    snapshot instead of freezing."""

    FILE_PROCESS_IDS_USING_FILE = 47
    PROCESS_HANDLE_INFORMATION = 51
    OBJECT_NAME_INFORMATION = 1
    STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
    DUPLICATE_SAME_ACCESS = 0x2
    PROCESS_QUERY_INFORMATION = 0x400
    PROCESS_DUP_HANDLE = 0x40
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    FILE_SHARE_DELETE = 0x4
    OPEN_EXISTING = 3
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    def __init__(self, dummy_files):
        """dummy_files: {avi_name: absolute_path_to_dummy}."""
        self._paths = dict(dummy_files)
        self._handles = {}
        self._name_to_key = {}   # (volume, basename) -> dummy name
        for name, path in self._paths.items():
            h = k32.CreateFileW(path, self.GENERIC_READ,
                                self.FILE_SHARE_READ | self.FILE_SHARE_WRITE | self.FILE_SHARE_DELETE,
                                None, self.OPEN_EXISTING, 0, None)
            if h and h != self.INVALID_HANDLE:
                self._handles[name] = h
                key = self._object_key(h)
                if key is not None:
                    self._name_to_key[key] = name
                else:
                    logging.warning("dummy watch: cannot read object name of %s",
                                    path)
            else:
                logging.warning("dummy watch: cannot open %s (err=%d)",
                                path, ctypes.get_last_error())
        self._proc_enum_buf = None
        self._last_enum_used = 0
        self._game_proc = None
        self._game_pid = None
        # A capped native name query may outlive the scan pass. Keep process
        # handles by PID until shutdown so PID changes cannot close a handle
        # still used by one of those detached query threads.
        self._game_procs = {}
        # The class-51 entry layout is undocumented and differs between
        # Windows builds; probe the candidates against our own dummy handles
        # (see _discover_layout). None here means no candidate matched and
        # the fast path stays disabled - the class-47 fallback is used.
        self._layout = self._discover_layout()
        self._file_type_index = (self._layout.type_index
                                 if self._layout is not None else None)

        # Worker-thread cache: the fast path runs in a background thread and
        # publishes its latest result here; held_by() reads it under a lock
        # and never performs the (potentially stalling) queries itself.
        self._cache = set()
        self._cache_pid = None
        self._cache_at = 0.0
        self._cache_lock = threading.Lock()
        self._stop = threading.Event()
        self._kick = threading.Event()  # warm_up() wakes the worker immediately
        self._worker = None
        self._target_pid = None
        # Handle values already queried once (proven instant afterwards) and
        # handle values that stalled (skipped until POISON_TTL elapses).
        self._touched = set()
        self._touched_signatures = {}
        self._poisoned = {}   # handle_value -> poison_until (monotonic)
        # Once a handle is identified as one of our dummy videos, retain that
        # association and query only these handles on every later poll. The
        # previous implementation re-queried every touched file handle, which
        # made both first-frame and ESC latency grow with the game's handle
        # table size.
        self._dummy_handles = {}  # dummy name -> set(game handle values)

    # -- worker thread ---------------------------------------------------

    def start(self):
        """Launch the background poller. Call once after construction."""
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop,
                                        name="dummywatch", daemon=True)
        self._worker.start()

    def _worker_loop(self):
        while not self._stop.is_set():
            # Sleep between passes; warm_up() wakes us immediately when the
            # game process first appears, so the very first handle queries
            # (the cold, capped ones) start as early as possible - ideally
            # during the game's splash, before it opens any dummy video.
            self._kick.wait(POLL_INTERVAL)
            self._kick.clear()
            pid = self._target_pid
            if pid is not None:
                try:
                    # Warm the known dummy files once per PID in parallel.
                    # This avoids waiting for a dummy late in the process
                    # handle table while the optimized name scan continues to
                    # maintain the normal fast path.
                    if not hasattr(self, "_class47_pid") or self._class47_pid != pid:
                        held = self._held_by_class47(pid)
                        self._class47_pid = pid
                    else:
                        held = set()
                    fast = self._held_by_names(pid)
                    if fast is None:
                        if not held:
                            held = self._held_by_class47(pid)
                    else:
                        held |= fast
                except Exception:
                    logging.exception("dummy watch worker error")
                    held = None
                if held is not None:
                    with self._cache_lock:
                        # Do not let a result from an old process instance
                        # become visible after PID reuse or a relaunch.
                        if self._target_pid == pid:
                            self._cache = held
                            self._cache_pid = pid
                            self._cache_at = time.monotonic()

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _object_name(h):
        """Full NT object name of handle h (e.g.
        '\\Device\\HarddiskVolume9\\path\\file.avi'), or None when the
        object is anonymous/unnamed. Read from the object header in memory
        (NtQueryObject, ObjectNameInformation) - no disk I/O, cannot block."""
        size = 256
        while True:
            buf = ctypes.create_string_buffer(size)
            needed = w.DWORD()
            st = ntdll.NtQueryObject(h, DummyWatch.OBJECT_NAME_INFORMATION,
                                     buf, size, ctypes.byref(needed))
            if st == DummyWatch.STATUS_INFO_LENGTH_MISMATCH:
                size = needed.value + 8 if needed.value else size * 2
                continue
            if st != 0:
                return None
            # UNICODE_STRING: USHORT Length, USHORT MaxLength, PWSTR Buffer
            length = ctypes.cast(buf, ctypes.POINTER(w.WORD))[0]
            bufptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_size_t))[1]
            if not bufptr or not length:
                return None
            return ctypes.wstring_at(bufptr, length // 2)

    @staticmethod
    def _object_key(h):
        """(volume, basename) identity of the file behind handle h - the
        same for every hard link to a file (both links share volume and
        basename), so it matches the game-dir link against this watch's
        _vfs_data_ copy. Returns None when the object is anonymous/unnamed."""
        name = DummyWatch._object_name(h)
        if not name:
            return None
        parts = [p for p in name.replace("/", "\\").split("\\") if p]
        if not parts:
            return None
        base = parts[-1].lower()
        if not base:
            return None
        head = parts[0].lower()
        if head == "device" and len(parts) >= 2:
            vol = ("\\device\\" + parts[1]).lower()
        elif head in ("??", "global??") and len(parts) >= 2:
            vol = parts[1].lower()
        else:
            vol = head
        return (vol, base)

    def _discover_layout(self):
        """Pick the class-51 entry layout that matches THIS Windows build.

        The entry structure is undocumented Native API and its layout differs
        between builds (no SDK header defines it; even Process Hacker's older
        sourceforge-era layout does not match this machine). First the known
        candidate layouts are probed against our own dummy handles - whose
        values we know exactly. If none matches, the layout is derived
        EMPIRICALLY from the raw enumeration bytes (_discover_layout_empirically)
        before ever falling back to the slow class-47 path. Returns a _Layout,
        or None when even the empirical derivation fails (fast path disabled).
        """
        if not self._handles:
            return None
        buf, count = self._enum_process_handles(k32.GetCurrentProcess())
        if buf is None or count <= 0:
            return None
        known = set(self._handles.values())
        best = None
        best_score = 0
        for struct_type in _LAYOUT_CANDIDATES:
            stride = ctypes.sizeof(struct_type)
            # The snapshot header is 8 or 16 bytes depending on the build
            # (ULONG/ULONG vs ULONG_PTR/ULONG_PTR); probe both.
            for header in (8, 16):
                # The snapshot's reported byte count must agree with the
                # candidate stride/header before casting entries. Besides
                # rejecting a false layout, this prevents a wrong candidate
                # from reading past the returned snapshot buffer.
                if self._last_enum_used != header + count * stride:
                    continue
                score = 0
                type_index = None
                sane = True
                for i in range(count):
                    e = ctypes.cast(
                        ctypes.byref(buf, header + i * stride),
                        ctypes.POINTER(struct_type)).contents
                    hv = int(e.HandleValue)
                    if hv not in known:
                        continue
                    if (int(e.GrantedAccess) == 0 or int(e.ObjectTypeIndex) == 0
                            or int(e.HandleCount) < 1):
                        sane = False
                        break
                    current_type = int(e.ObjectTypeIndex)
                    if type_index is None:
                        type_index = current_type
                    elif current_type != type_index:
                        # All anchors are File objects; a disagreement means
                        # this candidate is decoding the fields incorrectly.
                        sane = False
                        break
                    score += 1
                # A partial match is not sufficient: every handle opened by
                # this watcher must be found and decode consistently. This is
                # what sends uncertain candidates to empirical detection.
                if sane and score == len(known) and score > best_score:
                    best = _Layout(struct_type, header, stride, 0,
                                   type_index, None)
                    best_score = score
        if best is not None:
            return best
        # No named candidate matched this build: measure the real layout from
        # the raw bytes instead of giving up. Class 47 remains the last resort
        # for layouts nobody has ever seen.
        return self._discover_layout_empirically()

    def _discover_layout_empirically(self):
        """Derive the class-51 entry layout from the raw enumeration bytes.

        This process's own handles are the anchors: their exact values are
        known, so their positions in the buffer fix the entry stride, the
        snapshot header size and the HandleValue offset - with no prior
        knowledge of the struct. The ObjectTypeIndex field is then pinned by
        comparing the entries of File anchors (the dummies plus one extra
        file opened with a different access mask) against known non-File
        anchors (an event and a mutex). Returns a _Layout, or None when the
        bytes are inconsistent (caller falls back to class 47)."""
        anchors = self._open_anchor_handles()
        try:
            buf, count = self._enum_process_handles(k32.GetCurrentProcess())
            if buf is None or count <= 0:
                return None
            used = self._last_enum_used
            raw = buf.raw
            ps = ctypes.sizeof(ctypes.c_size_t)
            file_values = set(self._handles.values())
            nonfile_values = set()
            if "__file__" in anchors:
                file_values.add(anchors["__file__"])
            for k in ("__event__", "__mutex__"):
                if k in anchors:
                    nonfile_values.add(anchors[k])
            known_values = file_values | nonfile_values
            # Every offset where one of our known handle values appears as a
            # little-endian pointer-sized integer. Spurious byte coincidences
            # are possible, so all offsets are collected per value and the
            # true ones are selected below by grid membership.
            offsets_by_value = {}
            for off in range(0, used - ps + 1):
                v = int.from_bytes(raw[off:off + ps], "little")
                if v in known_values:
                    offsets_by_value.setdefault(v, []).append(off)
            all_offsets = sorted(o for lst in offsets_by_value.values()
                                 for o in lst)
            if len(all_offsets) < 3:
                return None
            # The true stride is the only value in a sane range where a
            # clear majority of matches share one residue modulo it AND the
            # used length equals header + count * stride (pins the 8 vs 16
            # byte header). The majority residue tolerates spurious matches.
            stride = header = hv_off = None
            for s in range(16, 65, 4):
                counts = {}
                for m in all_offsets:
                    counts[m % s] = counts.get(m % s, 0) + 1
                r, cnt = max(counts.items(), key=lambda kv: kv[1])
                if cnt < 3:
                    continue
                for h in (ps, 2 * ps):
                    if used != h + count * s:
                        continue
                    off_in = (r - h) % s
                    if off_in % ps or off_in + ps > s:
                        continue
                    hits = sum(1 for m in all_offsets
                               if (m - h - off_in) % s == 0)
                    if hits >= max(3, len(all_offsets) // 2):
                        stride, header, hv_off = s, h, off_in
                        break
                if stride is not None:
                    break
            if stride is None:
                return None
            # Pin the ObjectTypeIndex field: a 4-byte value shared by every
            # File anchor (even across different access masks - which rules
            # out GrantedAccess), absent from the event/mutex anchors, small
            # and non-zero (rules out HandleCount/PointerCount/Reserved).
            entries = {}
            for v, lst in offsets_by_value.items():
                for o in lst:
                    if ((o - header - hv_off) % stride == 0
                            and 0 <= (o - header - hv_off) // stride < count):
                        entries[v] = (o - header - hv_off) // stride
                        break
            # The derived grid must account for every anchor. A majority
            # match alone is not enough because a coincidental byte pattern
            # could otherwise produce a plausible-looking layout.
            if any(v not in entries for v in known_values):
                return None
            if not nonfile_values:
                return _Layout(None, header, stride, hv_off, None, None)
            type_index = None
            type_index_off = None
            for p in range(0, stride - 3, 4):
                f = None
                ok = True
                for v in file_values:
                    e = entries.get(v)
                    if e is None:
                        ok = False
                        break
                    val = int.from_bytes(
                        raw[header + e * stride + p:
                            header + e * stride + p + 4], "little")
                    if f is None:
                        f = val
                    elif val != f:
                        ok = False
                        break
                if not ok or not f or f >= 512:
                    continue
                if any(v in entries and int.from_bytes(
                        raw[header + entries[v] * stride + p:
                            header + entries[v] * stride + p + 4], "little")
                       == f for v in nonfile_values):
                    continue
                # A second agreeing candidate means the test is ambiguous;
                # drop the type filter rather than risk a wrong one.
                if type_index_off is not None:
                    return _Layout(None, header, stride, hv_off, None, None)
                type_index, type_index_off = f, p
            return _Layout(None, header, stride, hv_off,
                           type_index, type_index_off)
        finally:
            self._close_anchor_handles(anchors)

    def _open_anchor_handles(self):
        """Extra handles opened only for empirical layout discovery: a file
        opened with a different access mask (its ObjectTypeIndex stays File
        although GrantedAccess differs - that separates the two fields), plus
        one event and one mutex as known non-File objects. Returns
        {label: handle_value} for the newly opened handles."""
        anchors = {}
        try:
            path = next(iter(self._paths.values()))
            h = k32.CreateFileW(path, self.GENERIC_READ | 0x40000000,
                                self.FILE_SHARE_READ | self.FILE_SHARE_WRITE
                                | self.FILE_SHARE_DELETE,
                                None, self.OPEN_EXISTING, 0, None)
            if h and h != self.INVALID_HANDLE:
                anchors["__file__"] = h
        except Exception:
            pass
        try:
            k32.CreateEventW.restype = ctypes.c_void_p
            k32.CreateEventW.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD,
                                         ctypes.c_wchar_p]
            h = k32.CreateEventW(None, False, False, None)
            if h:
                anchors["__event__"] = h
        except Exception:
            pass
        try:
            k32.CreateMutexW.restype = ctypes.c_void_p
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, w.DWORD,
                                         ctypes.c_wchar_p]
            h = k32.CreateMutexW(None, False, None)
            if h:
                anchors["__mutex__"] = h
        except Exception:
            pass
        return anchors

    def _close_anchor_handles(self, anchors):
        for h in anchors.values():
            try:
                k32.CloseHandle(ctypes.c_void_p(h))
            except Exception:
                pass

    def _entry(self, buf, i, raw=None):
        """The i-th class-51 entry, decoded with the probed/derived layout.
        Returns a ctypes struct (named candidates) or a _RawEntry with
        .HandleValue / .ObjectTypeIndex (empirically derived layout)."""
        lay = self._layout
        base = lay.header + i * lay.stride
        if lay.struct_type is not None:
            return ctypes.cast(ctypes.byref(buf, base),
                               ctypes.POINTER(lay.struct_type)).contents
        if raw is None:
            raw = buf.raw
        ps = ctypes.sizeof(ctypes.c_size_t)
        hv = int.from_bytes(raw[base + lay.hv_off:base + lay.hv_off + ps],
                            "little")
        if lay.type_index_off is None:
            return _RawEntry(hv, None)
        ti = int.from_bytes(raw[base + lay.type_index_off:
                                base + lay.type_index_off + 4], "little")
        return _RawEntry(hv, ti)

    def _enum_process_handles(self, proc_handle):
        """(buffer, count) for one process's handle table (class 51), or
        (None, 0) on failure. Entries are decoded via _entry(). Reuses/grows
        a cached buffer."""
        size = 1 << 16
        if self._proc_enum_buf is not None:
            size = len(self._proc_enum_buf)
        while True:
            if self._proc_enum_buf is None or len(self._proc_enum_buf) != size:
                self._proc_enum_buf = ctypes.create_string_buffer(size)
            needed = w.DWORD()
            st = ntdll.NtQueryInformationProcess(
                proc_handle, self.PROCESS_HANDLE_INFORMATION,
                self._proc_enum_buf, size, ctypes.byref(needed))
            if st == self.STATUS_INFO_LENGTH_MISMATCH:
                size = max(size * 2, needed.value + 4096)
                continue
            if st != 0:
                return None, 0
            self._last_enum_used = int(needed.value)
            info = ctypes.cast(self._proc_enum_buf,
                               ctypes.POINTER(_PROCESS_HANDLE_SNAPSHOT_INFORMATION)).contents
            return self._proc_enum_buf, int(info.NumberOfHandles)

    def _game_process(self, pid):
        """Return a PID-scoped process handle with the access we need
        (PROCESS_QUERY_INFORMATION | PROCESS_DUP_HANDLE).

        Handles are kept until watcher shutdown because a capped query thread
        may still be using the handle after its scan pass returns.
        """
        if pid in self._game_procs:
            self._game_proc = self._game_procs[pid]
            self._game_pid = pid
            return self._game_proc
        h = k32.OpenProcess(self.PROCESS_QUERY_INFORMATION | self.PROCESS_DUP_HANDLE,
                            False, pid)
        if h:
            self._game_procs[pid] = h
        self._game_proc = h if h else None
        self._game_pid = pid
        return self._game_proc

    def _dup_name(self, proc, handle_value):
        """Duplicate one of the game's handles into this process and return
        its (volume, basename) object identity, or None. The duplicate is
        momentary and closed before returning."""
        dup = ctypes.c_void_p()
        st = ntdll.NtDuplicateObject(
            proc, ctypes.c_void_p(handle_value), k32.GetCurrentProcess(),
            ctypes.byref(dup), 0, 0, self.DUPLICATE_SAME_ACCESS)
        if st != 0:
            return None
        key = self._object_key(dup)
        ntdll.NtClose(dup)
        return key

    def _held_by_names(self, pid):
        """Fast path: enumerate once, then query only relevant handles.

        Newly seen handles are queried in bounded cold waves to discover a
        dummy video. Handles already identified as dummy videos are queried
        directly on every pass so close/skip detection remains quick. Other
        previously touched handles are not re-queried: doing so was the source
        of the intermittent 1-2 second latency on large game handle tables.
        """
        proc = self._game_process(pid)
        if not proc or not self._name_to_key or self._layout is None:
            return None
        buf, count = self._enum_process_handles(proc)
        if buf is None:
            return None
        raw = buf.raw
        held = set()
        now = time.monotonic()
        current = set()
        signatures = {}
        for i in range(count):
            e = self._entry(buf, i, raw)
            # type_index None (empirically derived layout without a pinned
            # File index) means no type filter: query every handle.
            if e.ObjectTypeIndex is not None \
                    and e.ObjectTypeIndex != self._file_type_index:
                continue
            hv = int(e.HandleValue)
            current.add(hv)
            base = self._layout.header + i * self._layout.stride
            signatures[hv] = raw[base:base + self._layout.stride]

        # A handle value can be reused by Windows after a file closes. Drop
        # observations for values that disappeared, and force a fresh query
        # when the entry bytes change even if the numeric handle is reused.
        for hv in set(self._touched) - current:
            self._touched.discard(hv)
            self._touched_signatures.pop(hv, None)
            self._poisoned.pop(hv, None)

        # Remove handles that disappeared from the game table. A later reuse
        # of that value is then cold again and will be queried safely.
        known_values = set()
        for name, values in list(self._dummy_handles.items()):
            values.intersection_update(current)
            if not values:
                del self._dummy_handles[name]
            else:
                known_values.update(values)

        # Dummy handles are the only touched handles that must be rechecked
        # every pass; this is the fast closure/ESC path. Keep these queries
        # bounded too: a supposedly warm NtQueryObject can still stall on a
        # closing handle under Windows, and an unbounded call here delays menu
        # transition as well as video startup.
        active_values = {
            hv: name
            for name, values in self._dummy_handles.items()
            for hv in values
        }
        active_results = {}
        active_abandoned = set()
        if active_values:
            active_results, active_abandoned, _ = self._query_cold(
                proc, list(active_values))
        for hv, name in active_values.items():
            if hv in active_abandoned:
                # A query timeout is inconclusive; retain the association and
                # keep the video alive until a later pass resolves it.
                held.add(name)
                known_values.add(hv)
                continue
            key = active_results.get(hv)
            mapped = self._name_to_key.get(key) if key is not None else None
            values = self._dummy_handles.get(name)
            if mapped == name:
                held.add(name)
                continue
            if values is not None:
                values.discard(hv)
            known_values.discard(hv)
            if mapped is not None:
                self._dummy_handles.setdefault(mapped, set()).add(hv)
                known_values.add(hv)
                held.add(mapped)
        for name in list(self._dummy_handles):
            if not self._dummy_handles[name]:
                del self._dummy_handles[name]

        # Only handles never inspected for this PID need cold name queries.
        cold = [hv for hv in current
                if (hv not in self._touched
                    or self._touched_signatures.get(hv) != signatures.get(hv))
                and hv not in known_values
                and self._poisoned.get(hv, 0.0) <= now]
        if cold:
            t_cold = time.monotonic()
            results, abandoned, attempted = self._query_cold(proc, cold)
            for hv in attempted:
                if hv in abandoned:
                    # Stalled: skip this value for a while, then retry
                    # (capped again on its next sighting after POISON_TTL).
                    self._poisoned[hv] = now + POISON_TTL
                    continue
                self._touched.add(hv)
                self._touched_signatures[hv] = signatures.get(hv)
                key = results.get(hv)
                if key is not None:
                    name = self._name_to_key.get(key)
                    if name is not None:
                        self._dummy_handles.setdefault(name, set()).add(hv)
                        held.add(name)
            dbg("cold handle pass: %d handle(s) in %.3fs (%d stalled, %d deferred)"
                % (len(attempted), time.monotonic() - t_cold,
                   len(abandoned), len(cold) - len(attempted)))
        return held

    def _query_cold(self, proc, cold):
        """Query newly seen handles in bounded waves.

        The entire pass has one deadline, rather than one QUERY_CAP wait per
        handle. Handles not reached before that deadline remain cold for the
        next pass, so a large handle table cannot postpone the first video by
        several seconds (or permanently get marked as already queried).
        """
        results = {}
        abandoned = set()
        attempted = set()
        deadline = time.monotonic() + QUERY_CAP
        for start in range(0, len(cold), COLD_QUERY_WAVE):
            if time.monotonic() >= deadline:
                break
            wave = cold[start:start + COLD_QUERY_WAVE]
            threads = []
            for hv in wave:
                attempted.add(hv)

                def _query(hv=hv):
                    try:
                        results[hv] = self._dup_name(proc, hv)
                    except Exception:
                        results[hv] = None

                t = threading.Thread(target=_query, daemon=True)
                t.start()
                threads.append((hv, t))

            # Poll the whole wave together. Joining in handle order would let
            # one stalled anonymous handle hide a dummy that already returned
            # in another thread.
            while time.monotonic() < deadline:
                if any(self._name_to_key.get(results.get(hv)) is not None
                       for hv in attempted):
                    break
                if not any(t.is_alive() for _hv, t in threads):
                    break
                time.sleep(0.005)

            for hv, t in threads:
                if t.is_alive():
                    abandoned.add(hv)
            # The only result that matters for playback is a known dummy.
            if any(self._name_to_key.get(results.get(hv)) is not None
                   for hv in attempted):
                break
            # Continue with the next wave even when this wave contains a
            # pathological handle. The shared deadline still bounds the pass,
            # and the dummy may be in a later part of the handle table.
        return results, abandoned, attempted


    def _held_by_class47(self, pid):
        """Fallback: per-dummy query, run in parallel with one total cap.

        This is used once when a game PID appears. The old implementation ran
        one whole-system query after another, so 13 dummies could cost roughly
        400 ms or more. Each known dummy now gets its own short-lived worker and
        the aggregate wait is bounded, giving a reliable first snapshot without
        blocking the overlay main loop.
        """
        results = {}
        threads = []
        deadline = time.monotonic() + QUERY_CAP

        def query(name, handle):
            try:
                buf = _FILE_PROCESS_IDS()
                iosb = _IO_STATUS_BLOCK()
                st = ntdll.NtQueryInformationFile(
                    handle, ctypes.byref(iosb), ctypes.byref(buf),
                    ctypes.sizeof(buf), self.FILE_PROCESS_IDS_USING_FILE)
                if st == 0:
                    results[name] = any(
                        p == pid
                        for p in buf.ProcessIdList[:buf.NumberOfProcessIdsInBuffer])
                else:
                    results[name] = False
            except Exception:
                results[name] = False

        for name, handle in self._handles.items():
            t = threading.Thread(target=query, args=(name, handle), daemon=True)
            t.start()
            threads.append(t)

        while time.monotonic() < deadline and any(t.is_alive() for t in threads):
            time.sleep(0.005)
        held = {name for name, is_held in results.items() if is_held}
        dbg("class47 warm pass: %d dummy(s), %d held" % (len(threads), len(held)))
        return held

    def warm_up(self, pid):
        """Start querying the game's handles immediately for this PID."""
        if self._target_pid != pid:
            self._target_pid = pid
            self._class47_pid = None
            self._touched.clear()
            self._touched_signatures.clear()
            self._poisoned.clear()
            self._dummy_handles.clear()
            with self._cache_lock:
                self._cache.clear()
                self._cache_pid = None
                self._cache_at = 0.0
        self._kick.set()

    def held_by(self, pid):
        """Return the latest snapshot belonging to `pid`, without querying."""
        if self._target_pid != pid:
            self.warm_up(pid)
        with self._cache_lock:
            if self._cache_pid != pid:
                return set()
            return set(self._cache)

    def close(self):
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        for h in self._handles.values():
            k32.CloseHandle(h)
        self._handles.clear()
        for h in self._game_procs.values():
            k32.CloseHandle(h)
        self._game_procs.clear()
        self._game_proc = None


class VirtualVideoWatch:
    """Detects dummy-video open/close from the VFS's JSONL event log.

    Virtual-video mode (--video-events): the game's VIDEO folder is a
    junction into the WinFsp/FUSE drive, the matched .mp4/.txt sets are
    hidden there, and each visible dummy is served virtually. The VFS
    (UniversalAudioVFS) appends one JSON line per dummy lifecycle step:
        {"time": ..., "event": "open"|"close", "name": "H_Intro.avi",
         "path": "video/h_intro.avi"}
    These maintain a per-dummy OPEN REFCOUNT (the game/WinFsp may open a
    dummy several times before closing it); held_by() exposes exactly the
    interface the main loop consumes from DummyWatch.

    Players are started IMMEDIATELY on the first open - no debounce, no
    calibrated delay. That is safe here because the dummy AVI is a silent,
    near-static stand-in: anything our player does while the game spends
    ~1-1.5 s building its DirectShow graph happens over a black screen with
    no competing audio, and the dummy's full-length timeline (always >= the
    mp4's duration) means an early start still finishes comfortably before
    the game closes the dummy. The player itself gates picture and sound to
    its first rendered frame, so nothing plays into the void either way.

    No handle tables are ever touched in this mode: the class-51 entry-layout
    probing (candidates A/B/C), the empirical byte-level layout discovery,
    the fast per-process enumeration and the class-47 fallback all remain in
    DummyWatch for the default hard-link mode and stay unused here.

    `path` is resolved through PATH_INDEX (lowercase VFS-rel path -> dummy
    name), with a basename lookup into VIDEO_INDEX as fallback; unknown paths
    (not an indexed dummy) are ignored."""

    virtual_mode = True

    TAIL_POLL = 0.02  # seconds between reads while no new bytes arrive

    def __init__(self, events_path):
        self._path = events_path
        self._counts = {}   # dummy name -> open handle count (>0 = "held")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # Signalled whenever new events were applied; the main loop waits on
        # this instead of a fixed sleep, so reaction is immediate.
        self._wake = threading.Event()
        self._worker = None

    def start(self):
        """Launch the tailer thread. Call once after construction."""
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._tail_loop,
                                        name="virtualvideowatch", daemon=True)
        self._worker.start()

    def poke_wait(self, timeout):
        """Block until new events arrive or `timeout` elapses (main-loop tick).
        Replaces time.sleep(POLL_INTERVAL): event -> wake latency ~0 ms."""
        self._wake.wait(timeout)
        self._wake.clear()

    def _tail_loop(self):
        fh = None
        pending = ""
        try:
            while not self._stop.is_set():
                if fh is None:
                    try:
                        # The launcher deletes any stale event log before the
                        # VFS starts, so whatever exists now belongs to this
                        # run - always read from the beginning. Until the VFS
                        # writes its first event the file simply may not exist
                        # yet; retry until it appears.
                        fh = open(self._path, "r", encoding="utf-8",
                                  errors="replace")
                    except OSError:
                        self._stop.wait(0.05)
                        continue
                chunk = fh.read()
                if chunk:
                    pending += chunk
                    *lines, pending = pending.split("\n")
                    if lines:
                        self._apply(lines)
                    # Drain bursts immediately (open+close pairs arrive
                    # together); only idle passes pay the poll delay.
                    continue
                self._stop.wait(self.TAIL_POLL)
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass

    @staticmethod
    def _resolve(event):
        """Map one event dict to an indexed dummy name (or None)."""
        rel = str(event.get("path") or "").strip().lower().lstrip("/")
        if rel:
            name = PATH_INDEX.get(rel)
            if name:
                return name
            base = os.path.basename(rel.replace("/", os.sep))
        else:
            base = str(event.get("name") or "").strip()
        return base if base in VIDEO_INDEX else None

    def _apply(self, lines):
        deltas = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue  # torn/partial line should be impossible (VFS locks
                          # whole-line appends), but never die on garbage
            if not isinstance(ev, dict):
                continue
            name = self._resolve(ev)
            if name is None:
                continue
            kind = ev.get("event")
            if kind == "open":
                deltas[name] = deltas.get(name, 0) + 1
            elif kind == "close":
                deltas[name] = deltas.get(name, 0) - 1
        if not deltas:
            return
        with self._lock:
            for name, delta in deltas.items():
                count = self._counts.get(name, 0) + delta
                if count > 0:
                    self._counts[name] = count
                else:
                    self._counts.pop(name, None)
                dbg("vvs %s %s -> refcount=%d" % (
                    "open" if delta > 0 else "close", name,
                    self._counts.get(name, 0)))
        self._wake.set()

    def warm_up(self, pid):
        """Interface parity with DummyWatch: events are global to the mounted
        VFS (one game instance per mount), so there is nothing to warm."""
        return

    def held_by(self, pid):
        """Dummy names currently believed open (refcount > 0)."""
        with self._lock:
            return set(self._counts)

    def close(self):
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None


class Overlay:
    def __init__(self, source_dir, ffplay, diagnostics=False, sdl_audio_driver=None,
                 game_dir=None, maintain_aspect=True, ffmpeg=None, ffprobe=None,
                 window_type="child", audio_type="sdl_mixer",
                 player_type="pyav"):
        self.source_dir = source_dir
        self.ffplay = ffplay
        self.diagnostics = diagnostics
        self.sdl_audio_driver = sdl_audio_driver
        self.game_dir = game_dir
        self.maintain_aspect = maintain_aspect
        self.window_type = window_type  # "child" (SetParent) or "overlay" (floating)
        self.audio_type = audio_type    # "sdl_mixer" (default) or "winmm"
        self.player_type = player_type  # "pyav" (in-process) or "ffmpeg" (external)
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.players = {}   # avi_name -> PygamePlayer | subprocess.Popen (or None)
        self.ffplay_err = {}

    def start(self, avi_name, game_pid=None):
        if avi_name in self.players:
            return
        info = VIDEO_INDEX.get(avi_name)
        if info:
            mp4 = info["mp4"]
        else:
            # Fallback: the video sits directly in --source (legacy layout).
            mp4 = os.path.join(self.source_dir,
                               os.path.splitext(avi_name)[0] + ".mp4")
        if not os.path.isfile(mp4):
            logging.warning("%s: no matching mp4 (%s)", avi_name, mp4)
            self.players[avi_name] = None
            return
        if HAVE_PYGAME and not os.environ.get("OVERLAY_FORCE_FFPLAY"):
            self._start_pygame(avi_name, mp4, game_pid)
            return
        if not self.ffplay:
            logging.error("%s: ffplay not found", avi_name)
            self.players[avi_name] = None
            return
        cmd = [self.ffplay, "-noborder", "-autoexit",
               "-volume", "100", "-window_title", "CommandosVideo_" + avi_name]
        if game_pid:
            gw = find_game_main_window(game_pid)
            if gw:
                cr = w.RECT()
                if user32.GetClientRect(gw, ctypes.byref(cr)) and cr.right > 0 and cr.bottom > 0:
                    cmd += ["-x", str(cr.right), "-y", str(cr.bottom)]
                    # If cnc-ddraw is present and set to NOT maintain aspect
                    # ratio (maintas=false), stretch the video to fill the
                    # window instead of letterboxing it.
                    if should_stretch(self.game_dir, self.maintain_aspect):
                        cmd += ["-vf", "setdar=%d/%d" % (cr.right, cr.bottom)]
        cmd.append(mp4)
        errf = None
        try:
            if self.diagnostics:
                errf = open(os.path.join(
                    os.environ.get("TEMP", r"C:\Users\Thaer\AppData\Local\Temp"),
                    "ffplay_%s.log" % re.sub(r"\W+", "_", avi_name)), "wb")
            env = None
            if self.sdl_audio_driver:
                env = os.environ.copy()
                env["SDL_AUDIODRIVER"] = self.sdl_audio_driver
            p = subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW,
                                 stdout=subprocess.DEVNULL,
                                 stderr=errf if errf is not None else subprocess.DEVNULL,
                                 env=env)
        except OSError as e:
            logging.error("%s: failed to start ffplay: %s", avi_name, e)
            self.players[avi_name] = None
            return
        self.players[avi_name] = p
        self.ffplay_err[avi_name] = errf
        logging.info("started ffplay pid=%d for %s cmd=%s", p.pid, avi_name, " ".join(cmd))
        if game_pid:
            # Adopt the new window as soon as it appears: force topmost +
            # no-activate + exact position, tracked by this process's pid.
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if embed_overlay(p.pid, game_pid, force=True):
                    break
                time.sleep(0.05)
            focus_game_window(game_pid)

    def _start_pygame(self, avi_name, mp4, game_pid):
        """Start the in-process pygame player for this video. It renders the
        mp4 into a borderless window over the game window's client area - a
        true child of the game window (window_type=child, the default) or a
        floating top-level overlay (window_type=overlay) - tracking the
        game's client geometry every frame so moves and resizes are followed
        seamlessly."""
        if os.environ.get("OVERLAY_NO_PLAYER"):
            logging.info("%s: player disabled (bisect)", avi_name)
            self.players[avi_name] = None
            return
        if not (self.ffmpeg and self.ffprobe):
            logging.error("%s: ffmpeg not found; video overlay disabled", avi_name)
            self.players[avi_name] = None
            return
        w0, h0 = 640, 480
        if game_pid:
            gw = find_game_main_window(game_pid)
            if gw:
                cr = w.RECT()
                if user32.GetClientRect(gw, ctypes.byref(cr)) and cr.right > 0 and cr.bottom > 0:
                    w0, h0 = cr.right, cr.bottom
                dbg("start_pygame: game window %s client=%dx%d" % (gw, cr.right, cr.bottom))
        # pygame has a single display per process: stop any previous pygame
        # player before starting a new one (cutscenes are sequential anyway).
        for name, other in list(self.players.items()):
            if name != avi_name and isinstance(other, PygamePlayer):
                self.stop(name)
        stretch = should_stretch(self.game_dir, self.maintain_aspect)
        player = PygamePlayer(mp4, "CommandosVideo_" + avi_name, game_pid, w0, h0,
                              stretch, ffmpeg_exe=self.ffmpeg, ffprobe_exe=self.ffprobe,
                              video_meta=META_CACHE.get(avi_name),
                              window_mode=self.window_type,
                              audio_type=self.audio_type,
                              decoder=self.player_type)
        player.start()
        self.players[avi_name] = player
        dbg("pygame player thread launched for %s (warm-display=%s)" %
            (avi_name, player._use_shared() or SHARED_DISPLAY))
        logging.info("started pygame player for %s (stretch=%s, window=%s, audio=%s, decoder=%s)",
                     avi_name, stretch, self.window_type, self.audio_type,
                     player.decoder)
        if game_pid:
            # The player window is created HIDDEN (pygame.HIDDEN) so SDL never
            # activates it and keyboard focus stays on the game, which is what
            # Alt+Enter/ESC need to reach it. Re-grab focus once the child
            # window is attached anyway (the game must be foreground for
            # Alt+Enter/ESC, and this covers a game that started without
            # focus). This runs in focus_game_window's own background thread
            # (no blocking); it returns instantly when the game already has
            # focus, so no fake key input is ever injected in the normal case.
            def refocus_after_attach():
                player.attached_evt.wait(timeout=6.0)
                focus_game_window(game_pid)

            threading.Thread(target=refocus_after_attach, name="refocus",
                             daemon=True).start()
            focus_game_window(game_pid)

    def stop(self, avi_name):
        dbg("overlay.stop: %s" % avi_name)
        p = self.players.pop(avi_name, None)
        if not p:
            return
        if isinstance(p, PygamePlayer):
            p.stop(timeout=2.0)
            logging.info("stopped player for %s", avi_name)
            state.overlay_hwnds.pop(getattr(p, "pid", None), None)
            return self._after_stop()
        errf = self.ffplay_err.pop(avi_name, None)
        if p.poll() is None:
            try:
                p.terminate()
                try:
                    p.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=1.0)
            except OSError:
                pass
        tail = ""
        if errf:
            try:
                errf.close()
                with open(errf.name, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 2048))
                    tail = f.read().decode("utf-8", errors="replace").strip().replace("\n", " | ")
                os.remove(errf.name)
            except OSError:
                pass
        logging.info("stopped ffplay for %s (exit=%s)%s",
                     avi_name, p.poll(), (" err: " + tail) if tail else "")
        state.overlay_hwnds.pop(p.pid, None)
        self._after_stop()

    @staticmethod
    def _after_stop():
        state.last_embed.clear()

    def stop_all(self):
        for name in list(self.players):
            self.stop(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="root dir to scan recursively for mp4 files (the "
                         "_vfs_data_ mirror); subfolders and original "
                         "extensions are discovered from the files themselves")
    ap.add_argument("--process", default=None,
                    help="comma-separated game process name(s) to watch "
                         "(exe filenames, e.g. comandos_w10.exe; default "
                         "coman_mp.exe)")
    ap.add_argument("--game", default=None,
                    help="game dir (used to locate ddraw.ini for stretch behavior)")
    ap.add_argument("--maintain-aspect", default="true",
                    help="maintain videos' playback aspect ratio (true/false, "
                         "default true); ddraw.ini maintas=false overrides true")
    ap.add_argument("--window-type", default="child",
                    choices=("child", "overlay"),
                    help="how the video player window attaches to the game: "
                         "child (default, true WS_CHILD of the game window via "
                         "SetParent) or overlay (top-level floating WS_EX_TOPMOST "
                         "window that tracks the game window)")
    ap.add_argument("--audio-type", default="sdl_mixer",
                    choices=("sdl_mixer", "winmm"),
                    help="how the video's audio is played: sdl_mixer (default, "
                         "SDL_Mixer via pygame.mixer, with WinMM waveOut as the "
                         "automatic fallback) or winmm (legacy WinMM waveOut "
                         "directly, no SDL_Mixer)")
    ap.add_argument("--player-type", dest="player_type", default="pyav",
                    help="which decoder plays the h264/h265 mp4s: pyav (default, "
                         "in-process PyAV decode - no ffmpeg.exe spawn or warm-up, "
                         "so no first-video playback delay; falls back to ffmpeg "
                         "when PyAV is unavailable) or ffmpeg (classic external "
                         "ffmpeg.exe pipes with the asynchronous Windows cache "
                         "warm-up job)")
    ap.add_argument("--ffplay", default=None)
    ap.add_argument("--sdl-audio-driver", default=FFPLAY_SDL_AUDIODRIVER,
                    help="SDL2 audio driver for ffplay (directsound, winmm, wasapi, "
                         "dummy; default directsound)")
    ap.add_argument("--log", default=r"C:\Users\Thaer\AppData\Local\Temp\opencode\video_overlay.log",
                    help="diagnostic log file path (used only with --diagnostics)")
    ap.add_argument("--diagnostics", action="store_true",
                    help="write a diagnostic log file (default: off)")
    ap.add_argument("--pid", type=int, default=None,
                    help="watch a specific process id instead of the game exe "
                         "(test hook)")
    ap.add_argument("--cache", default=None,
                    help="VFS index cache path; the pre-probed video metadata "
                         "(video_meta.json) lives next to it")
    ap.add_argument("--video-events", default=None, dest="video_events",
                    help="virtual-video mode: path to the VFS's JSONL event log "
                         "(video_events.jsonl). When set, open/close detection "
                         "tails that log (VirtualVideoWatch) instead of polling "
                         "the game's handle table (DummyWatch)")
    args = ap.parse_args()

    global _FIXED_PID, PROCESS_NAMES, WINDOW_TYPE
    if args.pid:
        _FIXED_PID = args.pid
    if args.process:
        names = {n.strip().lower() for n in args.process.split(",") if n.strip()}
        if names:
            PROCESS_NAMES = names

    window_type = args.window_type if args.window_type in ("child", "overlay") else "child"
    WINDOW_TYPE = window_type
    player_type = str(getattr(args, "player_type", "") or "").strip().lower()
    if player_type not in ("pyav", "ffmpeg"):
        player_type = "pyav"
    PLAYER_TYPE = player_type

    if args.diagnostics:
        logging.basicConfig(filename=args.log, level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        logging.info("=== video_overlay started (source=%s) ===", args.source)
    else:
        logging.disable(logging.CRITICAL)
    if not args.cache:
        logging.warning("--cache not set: pre-probed video metadata unavailable "
                        "(videos still play, but ffprobe may run when one starts)")
    set_dpi_aware()
    faulthandler.enable()

    # The launcher waits for a "ready" marker (written once the poll loop is
    # up) before starting the game, so the first dummy video is never delayed
    # by overlay startup. Remove any stale marker from a previous run first,
    # so a crashed run can never fake readiness.
    ready_path = None
    if args.cache:
        ready_path = os.path.join(os.path.dirname(os.path.abspath(args.cache)),
                                  "video_overlay.ready")
    if ready_path:
        try:
            os.remove(ready_path)
        except OSError:
            pass

    ffplay = find_ffplay(args.ffplay)
    if not ffplay:
        logging.error("ffplay not found; no overlay will be shown")
    ffmpeg = find_tool("ffmpeg") if HAVE_PYGAME else None
    ffprobe = find_tool("ffprobe") if HAVE_PYGAME else None
    if HAVE_PYGAME and not (ffmpeg and ffprobe):
        logging.error("ffmpeg/ffprobe not found; pygame player disabled")
    # Load the VFS's pre-probed video metadata (written during indexing) so
    # playback never runs ffprobe mid-game.
    load_video_meta_cache(args.cache)
    # Index every re-encoded mp4 under --source (recursively): each maps to
    # the dummy file the game opens (same basename, ORIGINAL extension from
    # the sidecar .txt), wherever it lives - no VIDEO dir or .avi assumed.
    build_video_index(args.source)
    logging.info("video index: %d video(s)", len(VIDEO_INDEX))
    # Metadata is already loaded from the VFS cache/sidecars above. Avoid
    # launching one ffprobe subprocess per video: on a cold machine that
    # creates a burst of competing processes and can delay the first cutscene.
    # Missing metadata remains a last-resort player fallback.

    # Pre-create the pygame display (a HIDDEN borderless window) NOW, before
    # the game even runs, so the first video's window already exists when the
    # game opens it - pygame's display init + window creation (~0.3-1.5s on a
    # cold start) moves off the video-start critical path. Child mode only:
    # the overlay window type must be created VISIBLE at show time (see
    # video_player._run_inner), and a pre-created hidden window shown later
    # could come up behind the game. OVERLAY_NO_PRECREATE disables this for
    # bisect.
    if (HAVE_PYGAME and precreate_display is not None
            and window_type == "child"
            and not os.environ.get("OVERLAY_NO_PRECREATE")):
        precreate_display()
        # Warm the selected decoder after the pygame display is ready. Both
        # paths are asynchronous and best-effort; the game launch path never
        # waits for process creation or decoder completion.
        if player_type == "ffmpeg":
            # Classic behavior: one tiny asynchronous ffmpeg job warms
            # Windows' exe/DLL cache so a later video start is cheaper.
            if ffmpeg and warm_ffmpeg is not None:
                warm_ffmpeg(ffmpeg)
        elif warm_pyav is not None:
            # PyAV mode: load PyAV's bundled FFmpeg libraries into this
            # process now instead of on the first video's critical path.
            warm_pyav()
            if not HAVE_PYAV and ffmpeg and warm_ffmpeg is not None:
                # PyAV requested but unavailable: players fall back to the
                # ffmpeg pipeline, so warm it too.
                warm_ffmpeg(ffmpeg)

    maintain_aspect = _parse_bool(args.maintain_aspect, True)
    overlay = Overlay(args.source, ffplay, diagnostics=args.diagnostics,
                      sdl_audio_driver=args.sdl_audio_driver, game_dir=args.game,
                      maintain_aspect=maintain_aspect, ffmpeg=ffmpeg, ffprobe=ffprobe,
                      window_type=window_type, audio_type=args.audio_type,
                      player_type=player_type)
    state.overlay = overlay
    game_pid = None
    pending = {}  # avi_name -> first_seen time
    names = set()  # dummy videos the game currently has open (from the watch)
    active = overlay.players
    if args.video_events:
        # Virtual-video mode: the game opens the dummies through the FUSE
        # junction and the VFS itself reports open/close via the JSONL event
        # log - no handle-table polling of any kind runs in this mode.
        logging.info("virtual-video mode: dummy open/close events from %s",
                     args.video_events)
        watch = VirtualVideoWatch(args.video_events)
    else:
        # Default hard-link mode: watch the REAL dummy files the game plays.
        # The game's 1998 AVI reader cannot play files served by the VFS/FUSE
        # drive, so the dummies are real hard-linked files and the open/close
        # signal comes from polling which process holds a handle to each one
        # (see DummyWatch).
        dummy_files = {}
        for name, info in VIDEO_INDEX.items():
            subdir = info.get("subdir") or ""
            dummy_files[name] = os.path.join(args.source, subdir, name)
        watch = DummyWatch(dummy_files)
    watch.start()
    # The poll loop is up (index built, watch running, display pre-created):
    # signal readiness to the launcher so it can start the game.
    if ready_path:
        try:
            with open(ready_path, "w", encoding="ascii") as f:
                f.write("state=ready\n")
            logging.info("overlay ready marker written: %s", ready_path)
        except OSError:
            logging.warning("cannot write overlay ready marker: %s", ready_path)
    exited_logged = set()

    try:
        while True:
            pid = find_game_pid()
            if pid is None:
                if game_pid is not None:
                    logging.info("game process exited; stopping players")
                    overlay.stop_all()
                    break
                time.sleep(0.3)
                continue
            if game_pid != pid:
                game_pid = pid
                logging.info("game pid = %d", pid)
                # Kick the dummy-watch worker immediately: its first handle
                # queries are the cold, capped ones, so running them during
                # the game's splash (before it opens any dummy video) keeps
                # the intro from being delayed by them.
                watch.warm_up(pid)

            # `names` = the dummies the game currently holds open; both
            # watcher types expose that through held_by(). Virtual-video
            # mode starts players IMMEDIATELY (no debounce): the dummy AVI
            # is a silent near-static stand-in, so an early start only ever
            # runs ahead of the game's silent DirectShow graph-build window,
            # and the dummy's full-length timeline guarantees the mp4 still
            # ends before the dummy is closed. Hard-link mode keeps the
            # open-debounce because there the dummy IS the real file the
            # game's filters probe and flap.
            vvm = getattr(watch, "virtual_mode", False)
            names = watch.held_by(pid)
            now = time.time()

            for nm in names:
                if vvm:
                    if nm not in active:
                        dbg("avi open -> start: %s" % nm)
                        overlay.start(nm, pid)
                    continue
                if nm not in active and nm not in pending:
                    dbg("avi seen: %s" % nm)
                    pending[nm] = now
                elif nm in pending and (now - pending[nm]) >= OPEN_DEBOUNCE:
                    dbg("avi debounced -> start: %s" % nm)
                    overlay.start(nm, pid)
                    pending.pop(nm, None)
            for nm in list(pending):
                if nm not in names:
                    del pending[nm]
            for nm in list(active):
                if nm not in names:
                    # Once the player window has attached, a closed dummy is a
                    # real skip/end signal and can stop immediately. During
                    # pre-attach startup retain the grace period because the
                    # old game briefly probes/flaps handles before playback.
                    player = active.get(nm)
                    if (isinstance(player, PygamePlayer)
                            and player.attached_evt.is_set()):
                        state.gone_since.pop(nm, None)
                        overlay.stop(nm)
                        continue
                    # The game closed the dummy video (last handle released) -
                    # mirror its state and stop the MP4. A short grace absorbs
                    # the game's open/close flapping (it can open AND close a
                    # dummy several times in quick succession without playing
                    # it): without it the player was killed mid-init and its
                    # window never appeared (audio-only). If the dummy stays
                    # closed past the grace, playback is really over - the game
                    # moved on or skipped - so stop the MP4 instead of letting
                    # it run over the menu. A reopen (name back in `names`)
                    # cancels the pending stop.
                    since = state.gone_since.get(nm)
                    if since is None:
                        state.gone_since[nm] = now
                    elif now - since >= CLOSE_GRACE:
                        state.gone_since.pop(nm, None)
                        overlay.stop(nm)
                else:
                    state.gone_since.pop(nm, None)

            for avi_name, p in list(active.items()):
                if p is None:
                    continue
                if isinstance(p, PygamePlayer):
                    # The pygame player owns its window: it re-reads the game
                    # window's client rect every frame and re-renders, so it
                    # handles positioning, resizing and topmost on its own.
                    if not p.is_alive():
                        if avi_name not in exited_logged:
                            logging.warning("%s: player ended on its own", avi_name)
                            exited_logged.add(avi_name)
                    elif p.stalled():
                        # Watchdog: the render loop stopped pumping (window
                        # would be frozen "Not Responding") - tear the player
                        # down so its window disappears instead of lingering.
                        # kill_fast() (not stop()) is used here: the render
                        # loop is stalled and cannot run the interrupt hold, so
                        # the window must be hidden synchronously by the caller.
                        logging.warning("%s: player stalled; forcing stop", avi_name)
                        dbg("watchdog: forcing stop of %s" % avi_name)
                        overlay.players.pop(avi_name, None)
                        p.kill_fast()
                    continue
                if p.poll() is not None:
                    if avi_name not in exited_logged:
                        logging.warning("%s: ffplay exited on its own (exit=%s)",
                                        avi_name, p.poll())
                        exited_logged.add(avi_name)
                    continue
                embed_overlay(p.pid, pid)

            # Virtual-video mode: wake the instant the next event lands
            # (event -> reaction ~0 ms). Handle-table mode keeps the fixed
            # polling cadence.
            if vvm:
                watch.poke_wait(POLL_INTERVAL)
            else:
                time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        overlay.stop_all()
    except Exception:
        logging.exception("fatal error")
        overlay.stop_all()
        raise
    finally:
        watch.close()


if __name__ == "__main__":
    main()
