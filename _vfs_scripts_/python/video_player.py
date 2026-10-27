#!/usr/bin/env python3
"""
video_player.py - in-process video player for video_overlay.py.

Decodes the real MP4 with ffmpeg (raw RGB frames piped on demand) and streams
its audio through SDL_Mixer (pygame.mixer - pygame-ce bundles SDL_mixer),
falling back to WinMM waveOut (ctypes) when the mixer is unavailable, while
rendering into a borderless pygame window positioned exactly over the game
window's client area. The
window type is chosen by video_overlay.py via the window_mode argument:

  child   (default) - a true CHILD of the game window (SetParent). It is
          clipped to the parent's client area and moves/resizes with it
          automatically. Keyboard and mouse input reach the game: the player
          window subclasses its WndProc and relays key presses (Alt+Enter
          included) and mouse button events to the game window, so they work
          regardless of which window has focus.

  overlay (floating) - a separate top-level WS_EX_TOPMOST layered window that
          floats over the game window and re-reads the game window's client
          position AND size on every rendered frame, following moves and
          resizes seamlessly. WS_EX_NOACTIVATE keeps keyboard focus on the
          game (ESC reaches it unchanged) and WS_EX_TRANSPARENT passes mouse
          clicks through to the game. Use this when a game refuses to accept
          an external child window.

No focus manipulation is done inside the player - SetFocus/SetForegroundWindow
are synchronous cross-process calls that wedge our thread while the game is
busy; the overlay handles focus from its own background thread.

Unlike ffplay, this player owns its whole pipeline, so resizing is seamless:
every rendered frame it re-reads the game window's client rectangle and, if
it changed, immediately resizes its own window and re-blits the current frame
to the new size. Playback never restarts.

Audio master clock: both SDL_Mixer and waveOut play at real time on this
machine (SDL_Mixer's output is driven by the audio device callback), so the
clock is wall time since playback started. SDL_AUDIODRIVER defaults to
directsound inside the player for the same reason the overlay pins ffplay's
driver: the Windows WASAPI path is known to mis-clock on this machine.

Run inside the video_overlay.py process (a pygame display can be created from
a worker thread; the thread that creates it must pump SDL events, which the
render loop does). video_overlay.py falls back to ffplay when pygame is not
installed.
"""

import ctypes
import ctypes.wintypes as w
import logging
import os
import queue
import re
import subprocess
import threading
import time

DEBUG_LOG = os.environ.get("PLAYER_DEBUG")
_debug_fh = None


def dbg(msg):
    """Temporary diagnostic logging (enabled via PLAYER_DEBUG env var)."""
    global _debug_fh
    if not DEBUG_LOG:
        return
    if _debug_fh is None:
        _debug_fh = open(DEBUG_LOG, "a", buffering=1)
    _debug_fh.write("%.3f %s\n" % (time.time(), msg))

try:
    import pygame
    HAVE_PYGAME = True
except ImportError:  # pragma: no cover - fallback path exercised by caller
    HAVE_PYGAME = False

# Optional in-process decoder (VIDEO_PLAYER_TYPE=pyav): PyAV bundles its own
# FFmpeg shared libraries and loads them inside THIS process, so h264/h265
# videos are decoded without ever spawning ffmpeg.exe - no external process
# cold-start, no Windows exe/DLL cache warm-up needed on a video's critical
# path. When unavailable (not installed) every player transparently falls
# back to the classic ffmpeg-subprocess pipeline.
try:
    import av as _av  # noqa: F401
    HAVE_PYAV = True
except ImportError:
    _av = None
    HAVE_PYAV = False

CREATE_NO_WINDOW = 0x08000000

# The overlay starts one tiny, asynchronous FFmpeg job before the game runs.
# This causes Windows to resolve/map ffmpeg.exe and its dependent DLLs while
# there is no video-start latency. It is deliberately best-effort and never
# waits on process creation or completion.
_ffmpeg_warmup_process = None
_ffmpeg_warmup_lock = threading.Lock()


def warm_ffmpeg(ffmpeg_exe):
    """Best-effort asynchronous FFmpeg executable/DLL warm-up."""
    global _ffmpeg_warmup_process
    if not ffmpeg_exe or not os.path.isfile(ffmpeg_exe):
        return False
    with _ffmpeg_warmup_lock:
        if (_ffmpeg_warmup_process is not None
                and _ffmpeg_warmup_process.poll() is None):
            return True
        try:
            _ffmpeg_warmup_process = subprocess.Popen(
                [ffmpeg_exe, "-hide_banner", "-loglevel", "quiet", "-nostdin",
                 "-f", "lavfi", "-i", "color=black:s=16x16:r=1",
                 "-frames:v", "1", "-f", "null", "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
            return True
        except OSError:
            _ffmpeg_warmup_process = None
            return False


def warm_pyav():
    """Best-effort asynchronous PyAV warm-up.

    Importing av loads PyAV's bundled FFmpeg DLLs once for the whole process;
    doing that at overlay startup (like warm_ffmpeg does for ffmpeg.exe)
    keeps it off the first video's critical path entirely. Purely best-effort:
    if av is not installed the import fails silently and players fall back to
    the ffmpeg subprocess pipeline."""
    def run():
        t0 = time.time()
        try:
            import av  # noqa: F401
            dbg("pyav warmed up (import took %.2fs)" % (time.time() - t0))
        except Exception:
            pass
    t = threading.Thread(target=run, name="pyav-warmup", daemon=True)
    t.start()
    return t


def _av_video_meta(mp4):
    """Return (width, height, fps, duration) or None, probed with PyAV.

    Used instead of ffprobe when the player decodes with PyAV, so a missing
    sidecar/cache entry never has to spawn an external tool either."""
    try:
        import av
        container = av.open(mp4)
    except Exception:
        return None
    try:
        s = next((x for x in container.streams if x.type == "video"), None)
        if s is None:
            return None
        cc = s.codec_context
        width, height = cc.width, cc.height
        if not width or not height:
            return None
        try:
            rate = s.average_rate or getattr(s, "base_rate", None)
            fps = float(rate) if rate else 0.0
        except (TypeError, ValueError):
            fps = 0.0
        duration = None
        try:
            if s.duration is not None and s.time_base is not None:
                duration = float(s.duration) * float(s.time_base)
        except (TypeError, ValueError):
            pass
        if not fps:
            return None
        return width, height, fps, duration
    finally:
        try:
            container.close()
        except Exception:
            pass


def _decode_audio_av(mp4):
    """Decode the WHOLE audio track into memory as s16le stereo PCM at
    AUDIO_RATE using PyAV (in-process; no ffmpeg subprocess). Mirrors what
    the classic `ffmpeg -f s16le` audio pipe produces. Returns b"" on any
    failure (the caller then plays silent)."""
    try:
        import av
        container = av.open(mp4)
    except Exception:
        return b""
    chunks = []
    resampler = None
    try:
        streams = container.streams.audio
        if not streams:
            return b""
        stream = streams[0]
        resampler = av.AudioResampler(format="s16", layout="stereo",
                                      rate=AUDIO_RATE)
        for packet in container.demux(stream):
            for frame in packet.decode():
                out = resampler.resample(frame)
                if out is None:
                    continue
                # Older PyAV returns one frame, newer ones a list.
                if hasattr(out, "planes"):
                    out = [out]
                for rf in out:
                    want = rf.samples * AUDIO_BLOCK
                    raw = bytes(rf.planes[0])[:want]
                    if raw:
                        chunks.append(raw)
        return b"".join(chunks)
    except Exception as e:
        # Keep whatever was decoded before the error: partial audio beats
        # silence, and the render loop tolerates a shorter track.
        dbg("pyav audio decode error: %r" % e)
        return b"".join(chunks)
    finally:
        try:
            container.close()
        except Exception:
            pass


SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x10
SWP_SHOWWINDOW = 0x40
SWP_HIDEWINDOW = 0x80
SWP_FRAMECHANGED = 0x20
SW_SHOW = 5
SW_HIDE = 0
GWL_STYLE = -16
GWL_EXSTYLE = -20
GWLP_WNDPROC = -4
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_CLIPCHILDREN = 0x02000000
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
ULW_ALPHA = 0x2
AC_SRC_OVER = 0x0
HWND_TOP = ctypes.c_void_p(0)
HWND_TOPMOST = ctypes.c_void_p(-1)
HWND_NOTOPMOST = ctypes.c_void_p(-2)
# Re-assert z-order independently of geometry polling. This is a local
# SetWindowPos on our own HWND (not the game's HWND), so it is cheap while
# recovering promptly when the game re-raises/recreates windows during a
# fullscreen transition.
Z_ORDER_ASSERT_INTERVAL = 0.05
VK_RETURN = 0x0D
# KF_ALTDOWN (bit 29 of lParam) marks Alt held on a WM_SYSKEY* message.
KF_ALTDOWN = 0x20000000
WM_PAINT = 0x000F

# Input messages forwarded from the player window to the game window
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_SYSCHAR = 0x0106
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MBUTTONDBLCLK = 0x0209
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
# NOTE: WM_MOUSEMOVE is deliberately NOT forwarded - it is high-frequency and
# the game does not need moves during playback; posting every move would flood
# the game's message queue.
FORWARD_MSGS = frozenset((WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_SYSKEYDOWN,
                          WM_SYSKEYUP, WM_SYSCHAR,
                          WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK,
                          WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK,
                          WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK,
                          WM_MOUSEWHEEL, WM_MOUSEHWHEEL))

# GDI presentation constants
SRCCOPY = 0x00CC0020
BI_RGB = 0

# WinMM / waveOut constants
WAVE_FORMAT_PCM = 1
WAVE_MAPPER = 0xFFFFFFFF
WHDR_DONE = 0x00000001
WHDR_PREPARED = 0x00000002
WHDR_INQUEUE = 0x00000010
MMSYSERR_NOERROR = 0

AUDIO_RATE = 22050
AUDIO_CHANNELS = 2
AUDIO_SAMPLE_BYTES = 2          # s16le
AUDIO_BLOCK = AUDIO_CHANNELS * AUDIO_SAMPLE_BYTES
AUDIO_BUF_MSEC = 100
AUDIO_NBUF = 8                  # 800ms pipeline; polling refill every ~50ms
AUDIO_FEED_INTERVAL = 0.05
# SDL_Mixer chunk size in samples (pygame.mixer.pre_init buffersize). At
# 22050 Hz, 512 samples is ~23ms - low latency, and the SDL audio thread
# services the callback easily on modern hardware. Raise to 1024/2048 if
# crackles or underruns ever appear on a slow machine.
SDL_MIXER_BUFSIZE = 512

# A/V sync tolerance (seconds). The audio clock is the master.
AV_LEAD = 0.03                  # start showing a frame slightly before its PTS
AV_DROP_BEHIND = 0.25           # drop frames that are this far behind the clock
AV_START_TIMEOUT = 0.35         # never make a cold audio init hide the first frame
# When the video is INTERRUPTED (the game closed the dummy video on ESC), keep
# the last rendered frame visible for this long before the window hides. The
# game stops its own video and redraws the menu UNDER our window; hiding while
# the game's video surface is still black (mid-transition) flashes black to the
# user (the black flash). Holding the last frame covers the game's transition,
# so the hide reveals the already-drawn menu instead. Tune with
# OVERLAY_INTERRUPT_HOLD_MS (default 50 - verified to cover the game's menu
# redraw; the launcher sets it). Natural EOF already holds the last frame until
# the audio finishes, which covers the game's end-of-video path.
try:
    INTERRUPT_HOLD = float(os.environ.get("OVERLAY_INTERRUPT_HOLD_MS", 50)) / 1000.0
except (TypeError, ValueError):
    INTERRUPT_HOLD = 0.05


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", w.WORD), ("nChannels", w.WORD),
                ("nSamplesPerSec", w.DWORD), ("nAvgBytesPerSec", w.DWORD),
                ("nBlockAlign", w.WORD), ("wBitsPerSample", w.WORD),
                ("cbSize", w.WORD)]


class WAVEHDR(ctypes.Structure):
    pass


WAVEHDR._fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", w.DWORD),
                    ("dwBytesRecorded", w.DWORD), ("dwUser", ctypes.c_size_t),
                    ("dwFlags", w.DWORD), ("dwLoops", w.DWORD),
                    ("lpNext", ctypes.POINTER(WAVEHDR)), ("reserved", ctypes.c_size_t)]


_winmm = ctypes.WinDLL("winmm")
_k32 = ctypes.WinDLL("kernel32")
_user32 = ctypes.WinDLL("user32")

_winmm.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), w.UINT,
                               ctypes.POINTER(WAVEFORMATEX), ctypes.c_size_t,
                               ctypes.c_size_t, w.DWORD]
_winmm.waveOutOpen.restype = w.UINT
_winmm.waveOutPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), w.UINT]
_winmm.waveOutPrepareHeader.restype = w.UINT
_winmm.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), w.UINT]
_winmm.waveOutWrite.restype = w.UINT
_winmm.waveOutUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), w.UINT]
_winmm.waveOutUnprepareHeader.restype = w.UINT
_winmm.waveOutReset.argtypes = [ctypes.c_void_p]
_winmm.waveOutReset.restype = w.UINT
_winmm.waveOutClose.argtypes = [ctypes.c_void_p]
_winmm.waveOutClose.restype = w.UINT

_user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(w.BOOL, ctypes.c_void_p, ctypes.c_void_p),
                                ctypes.c_void_p]
_user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD)]
_user32.GetWindowThreadProcessId.restype = w.DWORD
_user32.GetWindowTextW.argtypes = [ctypes.c_void_p, w.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.IsWindow.argtypes = [ctypes.c_void_p]
_user32.IsWindow.restype = w.BOOL
_user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
_user32.IsWindowVisible.restype = w.BOOL
_user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.ShowWindow.restype = w.BOOL
_user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.RECT)]
_user32.GetClientRect.restype = w.BOOL
_user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.POINT)]
_user32.ClientToScreen.restype = w.BOOL
_user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.GetWindowLongW.restype = w.LONG
_user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, w.LONG]
_user32.SetWindowLongW.restype = w.LONG
_user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_user32.SetWindowPos.restype = w.BOOL
_user32.SetParent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.SetParent.restype = ctypes.c_void_p
_user32.GetParent.argtypes = [ctypes.c_void_p]
_user32.GetParent.restype = ctypes.c_void_p
_user32.PostMessageW.argtypes = [ctypes.c_void_p, w.UINT, w.WPARAM, w.LPARAM]
_user32.PostMessageW.restype = w.BOOL
SM_CXSCREEN = 0
SM_CYSCREEN = 1


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", w.BYTE), ("BlendFlags", w.BYTE),
                ("SourceConstantAlpha", w.BYTE), ("AlphaFormat", w.BYTE)]


_user32.UpdateLayeredWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.POINTER(w.POINT), ctypes.POINTER(w.SIZE),
                                        ctypes.c_void_p, ctypes.POINTER(w.POINT),
                                        w.DWORD, ctypes.POINTER(BLENDFUNCTION), w.DWORD]
_user32.UpdateLayeredWindow.restype = w.BOOL
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.SetCursorPos.restype = w.BOOL
_user32.ShowCursor.argtypes = [w.BOOL]
_user32.ShowCursor.restype = ctypes.c_int
class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", ctypes.c_void_p), ("fErase", w.BOOL),
                ("rcPaint", w.RECT), ("fRestore", w.BOOL), ("fIncUpdate", w.BOOL),
                ("rgbReserved", w.BYTE * 32)]


_user32.GetDC.argtypes = [ctypes.c_void_p]
_user32.GetDC.restype = ctypes.c_void_p
_user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.ReleaseDC.restype = ctypes.c_int
_user32.BeginPaint.argtypes = [ctypes.c_void_p, ctypes.POINTER(PAINTSTRUCT)]
_user32.BeginPaint.restype = ctypes.c_void_p
_user32.EndPaint.argtypes = [ctypes.c_void_p, ctypes.POINTER(PAINTSTRUCT)]
_user32.EndPaint.restype = w.BOOL
_user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.CallWindowProcW.argtypes = [ctypes.c_ssize_t, ctypes.c_void_p, w.UINT, w.WPARAM, w.LPARAM]
_user32.CallWindowProcW.restype = ctypes.c_ssize_t

_gdi32 = ctypes.WinDLL("gdi32")
_gdi32.StretchDIBits.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, w.UINT,
                                 w.DWORD]
_gdi32.StretchDIBits.restype = ctypes.c_int
_gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi32.SelectObject.restype = ctypes.c_void_p
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.DeleteObject.restype = w.BOOL
_gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
_gdi32.DeleteDC.restype = w.BOOL


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
                ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD),
                ("biClrImportant", w.DWORD)]


_gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER),
                                    w.UINT, ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.c_void_p, w.DWORD]
_gdi32.CreateDIBSection.restype = ctypes.c_void_p


WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, ctypes.c_void_p, ctypes.c_void_p)
WNDPROC_T = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, w.UINT,
                               w.WPARAM, w.LPARAM)


class WaveOutStream:
    """Minimal WinMM waveOut player fed from an in-memory PCM buffer.

    Refills finished buffers from a polling feeder thread (the CALLBACK_EVENT
    notification is unreliable here) and exposes a real-time wall clock since
    the first buffer was queued - waveOut plays at true speed on this machine
    (verified: 100ms buffers complete every 100ms), unlike WASAPI's ~0.6x.

    clock_offset (seconds) is added to clock(): used when the audio stream is
    opened mid-video (background decode) so the master clock continues from
    where the video already is instead of jumping back to zero.
    """

    def __init__(self, pcm, rate=AUDIO_RATE, channels=AUDIO_CHANNELS,
                 sample_bytes=AUDIO_SAMPLE_BYTES, buf_msec=AUDIO_BUF_MSEC,
                 nbuf=AUDIO_NBUF, clock_offset=0.0):
        self.pcm = pcm
        self.rate = rate
        self.channels = channels
        self.block = channels * sample_bytes
        self.buf_bytes = max(1, int(rate * buf_msec / 1000)) * self.block
        self.nbuf = nbuf
        self.clock_offset = clock_offset
        self.hwo = ctypes.c_void_p()
        self.hdr = []
        self.raw_buffers = []
        self.offset = 0
        self.queued = 0
        self.closed = False
        self.started_at = None
        self.finished = threading.Event()
        self._feed_thread = None
        self._close_lock = threading.Lock()

    def prepare(self):
        """Open the device and allocate/prepare all ring buffers. Nothing is
        queued yet, so the device stays silent until start()."""
        fmt = WAVEFORMATEX()
        fmt.wFormatTag = WAVE_FORMAT_PCM
        fmt.nChannels = self.channels
        fmt.nSamplesPerSec = self.rate
        fmt.wBitsPerSample = self.block // self.channels * 8
        fmt.nBlockAlign = self.block
        fmt.nAvgBytesPerSec = self.rate * self.block
        fmt.cbSize = 0
        rc = _winmm.waveOutOpen(ctypes.byref(self.hwo), WAVE_MAPPER,
                                ctypes.byref(fmt), 0, 0, 0)
        if rc != MMSYSERR_NOERROR:
            raise OSError("waveOutOpen failed: %d" % rc)
        for i in range(self.nbuf):
            raw = ctypes.create_string_buffer(self.buf_bytes)
            h = WAVEHDR()
            h.lpData = ctypes.addressof(raw)
            h.dwBufferLength = 0
            h.dwUser = i
            h.dwFlags = 0
            if _winmm.waveOutPrepareHeader(self.hwo, ctypes.byref(h),
                                           ctypes.sizeof(h)) != MMSYSERR_NOERROR:
                raise OSError("waveOutPrepareHeader failed")
            self.hdr.append(h)
            self.raw_buffers.append(raw)

    def start(self):
        """Queue the priming buffers (playback begins at the first
        waveOutWrite), stamp the wall clock and launch the feeder thread."""
        def feed():
            try:
                while not self.closed:
                    time.sleep(AUDIO_FEED_INTERVAL)
                    if self.closed:
                        break
                    with self._close_lock:
                        if not self.closed:
                            self._service()
            except Exception:
                pass

        self._feed_thread = threading.Thread(target=feed, name="waveout-feed",
                                             daemon=True)
        self._feed_thread.start()
        # Prime the whole pipeline before playback starts: with every buffer
        # queued (8 x 100ms = 800ms of audio) the device never has to wait on
        # the 50ms feeder poll, so playback runs at real time. Queuing only
        # the first buffer throttles the device to the feeder's poll rate
        # (measured: 60s of audio took 81s and stuttered).
        for i in range(self.nbuf):
            if self.offset < len(self.pcm):
                self._refill(i)
        if self.offset > 0:
            self.started_at = time.time()

    def open(self):
        """Convenience: prepare() + start() (open-and-play in one call)."""
        self.prepare()
        self.start()

    def _service(self):
        for i in range(self.nbuf):
            h = self.hdr[i]
            if h.dwFlags & WHDR_DONE:
                h.dwFlags &= ~WHDR_DONE
                self.queued -= 1
                if self.offset < len(self.pcm):
                    self._refill(i)
        if self.queued == 0:
            self.finished.set()

    def _refill(self, i):
        h = self.hdr[i]
        n = min(self.buf_bytes, len(self.pcm) - self.offset)
        if n <= 0:
            return
        ctypes.memmove(h.lpData, self.pcm[self.offset:self.offset + n], n)
        self.offset += n
        h.dwBufferLength = n
        _winmm.waveOutWrite(self.hwo, ctypes.byref(h), ctypes.sizeof(h))
        self.queued += 1

    def clock(self):
        """Master clock: offset + seconds since the first buffer was queued."""
        if self.started_at is None:
            return self.clock_offset
        return self.clock_offset + (time.time() - self.started_at)

    def close(self):
        # Stop/reset and unprepare must be serialized with the feeder thread.
        # In particular, never let a late feeder refill a buffer after the
        # playback session has been cancelled.
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
            if self.hwo.value:
                _winmm.waveOutReset(self.hwo)
                for h in self.hdr:
                    _winmm.waveOutUnprepareHeader(self.hwo, ctypes.byref(h),
                                                  ctypes.sizeof(h))
                _winmm.waveOutClose(self.hwo)
                self.hwo = ctypes.c_void_p()
            self.finished.set()


class SdlMixerStream:
    """SDL_Mixer (pygame.mixer) player fed from an in-memory PCM buffer.

    pygame.mixer IS SDL_Mixer: pygame-ce bundles SDL_mixer, and the whole
    decoded PCM clip is handed to it as one Sound. SDL_Mixer streams that
    sound to the device from its own audio thread with its own buffering (the
    chunk size set via pre_init above), so unlike waveOut there is no polling
    feeder thread and no WAVEHDR juggling. The clock is wall time since
    play() started: SDL_Mixer playback is driven by the audio device callback
    at real time, so the offset-based clock is as accurate as the waveOut one.

    SDL_AUDIODRIVER is defaulted to "directsound" (unless already set): the
    WASAPI backend is known to mis-clock on this machine (see the module
    docstring), so pin the SDL audio backend to DirectSound like the overlay
    does for ffplay.

    clock_offset (seconds) is added to clock(): used when the audio stream is
    opened mid-video (background decode) so the master clock continues from
    where the video already is instead of jumping back to zero.
    """

    def __init__(self, pcm, rate=AUDIO_RATE, channels=AUDIO_CHANNELS,
                 sample_bytes=AUDIO_SAMPLE_BYTES, clock_offset=0.0):
        self.pcm = pcm
        self.rate = rate
        self.channels = channels
        self.sample_bytes = sample_bytes
        self.clock_offset = clock_offset
        self.sound = None
        self.channel = None
        self.started_at = None
        self.finished = threading.Event()
        self._watch_thread = None
        self._close_lock = threading.Lock()

    def prepare(self):
        """Do everything that costs time EXCEPT starting playback: pin the
        SDL audio backend, (re-)initialize the mixer at our format and build
        the Sound from the whole PCM buffer (a full copy - this is the slow
        part). After prepare(), start() is a single channel grab + play()."""
        # Pin the SDL audio backend before the mixer initializes (no-op if
        # the user set SDL_AUDIODRIVER themselves).
        os.environ.setdefault("SDL_AUDIODRIVER", "directsound")
        fmt = pygame.mixer.get_init()
        want = (self.rate, -16, self.channels)
        if fmt is None:
            pygame.mixer.pre_init(self.rate, -16, self.channels,
                                  SDL_MIXER_BUFSIZE)
            pygame.mixer.init()
        elif (fmt[0], fmt[1], fmt[2]) != want:
            # A previous playback initialized the mixer at a different
            # format. Sound(buffer=) requires the mixer's exact format, so
            # re-initialize at ours.
            pygame.mixer.quit()
            pygame.mixer.pre_init(self.rate, -16, self.channels,
                                  SDL_MIXER_BUFSIZE)
            pygame.mixer.init()
        self.sound = pygame.mixer.Sound(buffer=self.pcm)

    def start(self):
        """Grab a channel and begin playback; the wall clock starts now."""
        chan = pygame.mixer.find_channel(True)  # force: steal a free channel
        if chan is None:
            raise pygame.error("SDL_Mixer: no free audio channel")
        self.channel = chan
        chan.play(self.sound)
        self.started_at = time.time()

        def watch():
            # Mirror the waveOut feeder's job: detect the end of playback so
            # the render loop can hold the last video frame until the audio
            # has finished (the end-of-video wait in _run_inner).
            while not self.finished.is_set():
                time.sleep(0.05)
                if self.channel is not None and not self.channel.get_busy():
                    self.finished.set()
                    return

        self._watch_thread = threading.Thread(target=watch, name="mixer-watch",
                                              daemon=True)
        self._watch_thread.start()

    def open(self):
        """Convenience: prepare() + start() (open-and-play in one call)."""
        self.prepare()
        self.start()

    def clock(self):
        """Master clock: offset + seconds since playback started."""
        if self.started_at is None:
            return self.clock_offset
        return self.clock_offset + (time.time() - self.started_at)

    def close(self):
        with self._close_lock:
            if self.finished.is_set() and self.channel is None:
                return
            self.finished.set()
            if self.channel is not None:
                try:
                    self.channel.stop()
                except Exception:
                    pass
                self.channel = None
            self.sound = None


class _FrameReader(threading.Thread):
    """Reads rawvideo frames from the ffmpeg pipe in a separate thread so the
    render loop is never blocked on ffmpeg (a stalled pipe would otherwise
    leave the window unresponsive). The bounded queue applies backpressure:
    the reader pauses when the render loop is behind, preserving frame order
    and A/V sync."""

    def __init__(self, proc, frame_bytes, maxsize=4):
        super().__init__(name="frame-reader", daemon=True)
        self.proc = proc
        self.frame_bytes = frame_bytes
        self.frames = queue.Queue(maxsize=maxsize)
        self.eof = threading.Event()

    def run(self):
        try:
            while True:
                frame = self.proc.stdout.read(self.frame_bytes)
                if not frame or len(frame) < self.frame_bytes:
                    return
                self.frames.put(frame)
        except (OSError, ValueError):
            # Cancellation closes the pipe to wake a reader that is blocked in
            # read(); EOF is still published for the render-loop teardown.
            return
        finally:
            self.eof.set()


class _AvFrameReader(threading.Thread):
    """In-process video frame reader backed by PyAV (the pyav decoder path).

    Decodes the mp4's video stream inside THIS process (PyAV bundles its own
    FFmpeg libraries) and publishes exact rgb24 frames of width x height on
    the same bounded queue contract as _FrameReader, so the render loop is
    identical for both decoders: frames.get(timeout), eof event, backpressure
    via the queue bound. The reformat step converts any input pixel layout
    (h264 yuv420p, h265 gbrp, ...) to tightly packed rgb24; plane rows are
    copied per-linesize because PyAV pads each row to an alignment.
    """

    def __init__(self, mp4, width, height, maxsize=4):
        super().__init__(name="pyav-frame-reader", daemon=True)
        self.mp4 = mp4
        self.width = width
        self.height = height
        self.frame_bytes = width * height * 3
        self.frames = queue.Queue(maxsize=maxsize)
        self.eof = threading.Event()
        self._stop = threading.Event()

    def stop(self):
        """Cancel from another thread: unblocks a decode loop and a put()
        blocked on the full queue."""
        self._stop.set()
        try:
            while True:
                self.frames.get_nowait()
        except queue.Empty:
            pass

    def run(self):
        container = None
        try:
            import av
            container = av.open(self.mp4)
            stream = container.streams.video[0]
            # Multithreaded decode inside the library - this is what keeps
            # the render loop fed without any external process.
            stream.thread_type = "AUTO"
            row = self.width * 3
            for frame in container.decode(stream):
                if self._stop.is_set():
                    return
                img = frame.reformat(width=self.width, height=self.height,
                                     format="rgb24")
                plane = img.planes[0]
                line_size = plane.line_size
                raw = memoryview(plane)
                if line_size == row:
                    data = bytes(raw[:self.frame_bytes])
                else:
                    data = b"".join(raw[y * line_size:y * line_size + row]
                                    for y in range(self.height))
                del raw
                # Bounded put with stop checks: backpressure identical to
                # the pipe reader, but cancellable without killing a pipe.
                while not self._stop.is_set():
                    try:
                        self.frames.put(data, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except Exception as e:
            dbg("pyav video reader error: %r" % e)
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
            self.eof.set()


def find_window_by_pid_and_title(pid, title_prefix):
    """Return the first visible top-level window of pid whose title starts
    with title_prefix (None matches any visible window of that pid)."""
    found = [None]

    @WNDENUMPROC
    def cb(hwnd, lparam):
        wp = w.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wp))
        if wp.value != pid or not _user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, title, 256)
        if title_prefix is None or title.value.startswith(title_prefix):
            found[0] = hwnd
            return False
        return True

    _user32.EnumWindows(cb, None)
    return found[0]


def find_game_main_window(game_pid):
    """Return the game's visible top-level window (ignores IME/hidden hosts)."""
    win = [None]

    @WNDENUMPROC
    def cb(hwnd, lparam):
        wp = w.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wp))
        if wp.value != game_pid or not _user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, title, 256)
        t = title.value
        if t and t not in ("ActiveMovie Window", "MSCTFIME UI", "Default IME"):
            win[0] = hwnd
        return True

    _user32.EnumWindows(cb, None)
    return win[0]


def _client_rect_on_screen(hwnd):
    """Return (x, y, w, h): the window's client area mapped to screen
    coordinates. In child mode it sizes the player window to the game's
    client area (position is always (0, 0) in parent coordinates); in overlay
    mode it provides BOTH the on-screen position and the size the floating
    window must cover. These calls read the window's structure directly (no
    SendMessage to the game's thread), so they cannot block it."""
    crect = w.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(crect)):
        return None
    if crect.right <= 0 or crect.bottom <= 0:
        return None
    origin = w.POINT(0, 0)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    return origin.x, origin.y, crect.right, crect.bottom


# avi filename -> (width, height, fps, duration). Filled from the sidecar
# .txt files (instant, no ffprobe) at overlay startup and by each player.
META_CACHE = {}


# Set by precreate_display(): the overlay owns one pygame display + a hidden
# borderless window for the whole process, and each PygamePlayer session
# reuses it instead of creating (and destroying) a display per video. This
# moves the expensive cold-start display init + window creation off every
# video's critical path.
SHARED_DISPLAY = False


class _DisplayThread:
    """Owns the single pygame display for the whole overlay process.

    pygame's display must be created AND used from one thread: calling
    set_mode/event.pump from a different thread than the one that created the
    display hangs the whole process (measured - the render thread froze inside
    set_mode while holding the GIL, taking the overlay's poll loop down with
    it). So when pre-creation is enabled, a dedicated daemon thread creates
    the display + hidden window at overlay startup and every PygamePlayer
    session runs its ENTIRE render loop on this same thread, sequentially
    (cutscenes are sequential anyway - the overlay stops the previous player
    before starting the next one).
    """

    def __init__(self):
        self._queue = queue.Queue()
        self._stop_evt = threading.Event()
        self._ready = threading.Event()
        self._ok = False
        self.thread = threading.Thread(target=self._run,
                                       name="display-owner", daemon=True)

    def start(self):
        """Create the display + hidden window and start the session loop.
        Returns True on success (the caller should then set SHARED_DISPLAY)."""
        self.thread.start()
        self._ready.wait(timeout=15.0)
        return self._ok

    def submit(self, player):
        """Run `player`'s session on the display thread after any currently
        running session finishes."""
        player._finished.clear()
        self._queue.put(player)

    def _run(self):
        t0 = time.time()
        try:
            pygame.display.init()
            pygame.display.set_mode((640, 480), pygame.NOFRAME | pygame.HIDDEN)
            pygame.display.set_caption("CommandosVideo_")
            self._ok = True
            dbg("precreated display (hidden) in %.2fs" % (time.time() - t0))
        except pygame.error as e:
            logging.warning("display pre-create failed (%s); players will "
                            "create their own window", e)
        finally:
            self._ready.set()
        while not self._stop_evt.is_set():
            try:
                job = self._queue.get(timeout=0.1)
            except queue.Empty:
                # Idle: still service our window's messages (WM_DESTROY etc.),
                # so a game exiting while our hidden window is around is never
                # left blocked waiting on this thread.
                try:
                    pygame.event.pump()
                except Exception:
                    pass
                continue
            if job is None:
                break
            try:
                job._run_shared()
            except Exception:
                logging.exception("%s: player crashed", job.window_title)
            finally:
                job._finished.set()

    def close(self):
        self._stop_evt.set()
        try:
            self._queue.put(None)
        except Exception:
            pass


_display_thread = _DisplayThread()


def _dbg_window_state(player, hwnd, where):
    """Diagnostic (PLAYER_DEBUG): log the player window's visibility,
    geometry and styles - used to verify the window actually shows on screen
    each session, not just that the attach code ran."""
    try:
        visible = bool(_user32.IsWindowVisible(hwnd))
        rect = w.RECT()
        ok = _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        style = _user32.GetWindowLongW(hwnd, GWL_STYLE) or 0
        ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) or 0
        dbg("%s: hwnd=%s visible=%d rect=%s style=0x%x ex=0x%x" % (
            where, hwnd, int(visible),
            ("%dx%d+%d+%d" % (rect.right - rect.left, rect.bottom - rect.top,
                               rect.left, rect.top)) if ok else "?",
            style, ex))
    except Exception:
        pass


def precreate_display():
    """Start the display-owner thread, which creates the pygame display and a
    HIDDEN borderless window once, at overlay startup, before the game even
    runs. Every PygamePlayer session (shared mode) then runs on that same
    thread and only resizes, attaches and shows the window when a video
    starts - so pygame's display init + window creation (~0.3-1.5s on a cold
    start) is never on a video's critical path. The window is created HIDDEN
    (child mode's policy) and shown per session by _attach_to_game with
    SWP_NOACTIVATE, so keyboard focus never leaves the game. Returns True on
    success (sets SHARED_DISPLAY). Disable with the OVERLAY_NO_PRECREATE env
    var (the overlay gates on it too)."""
    global SHARED_DISPLAY
    if not HAVE_PYGAME:
        return False
    if _display_thread.thread.is_alive():
        return SHARED_DISPLAY
    SHARED_DISPLAY = _display_thread.start()
    return SHARED_DISPLAY


def _meta_from_txt(mp4):
    """Read the sidecar .txt (written by the re-encode script) for video
    metadata - instant, no subprocess. Returns (w, h, fps, None) or None."""
    txt = os.path.splitext(mp4)[0] + ".txt"
    try:
        with open(txt, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    res = fps = None
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("resolution"):
            m = re.search(r"(\d+)\s*[xX]\s*(\d+)", line)
            if m:
                res = (int(m.group(1)), int(m.group(2)))
        elif "frame" in line.lower() and "second" in line.lower():
            m = re.search(r"(\d+(?:\.\d+)?)", line)
            if m:
                fps = float(m.group(1))
    if res and fps:
        return res[0], res[1], fps, None
    return None


def original_ext_of(mp4):
    """Return the ORIGINAL video file extension (lowercase, without the dot)
    recorded in the sidecar <name>.txt written by the re-encode script. This is
    the extension of the dummy file the game actually opens (e.g. .avi), which
    is NOT necessarily .avi - it is whatever the source video was. Defaults to
    "avi" when the txt is missing or has no such line."""
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


def warm_meta_cache(source_dir, ffprobe_exe="ffprobe"):
    """Fill META_CACHE from every mp4's sidecar .txt (in parallel) in the
    background - pure file reads, so it completes in milliseconds and the
    player never waits on ffprobe. Walks source_dir recursively (videos may
    live in any subfolder, not just a top-level VIDEO dir) and keys each entry
    by the dummy filename the game opens (basename + ORIGINAL extension from
    the sidecar txt, e.g. H_Intro.avi). Returns the daemon thread."""
    def probe(mp4):
        avi = os.path.splitext(os.path.basename(mp4))[0] + "." + original_ext_of(mp4)
        if avi in META_CACHE:
            return
        info = _meta_from_txt(mp4)
        if info is None:
            info = _ffprobe_video(mp4,
                                  os.path.dirname(ffprobe_exe) if ffprobe_exe else "")
        if info:
            META_CACHE[avi] = info

    def run():
        try:
            threads = []
            for root, _dirs, files in os.walk(source_dir):
                for name in files:
                    if name.lower().endswith(".mp4"):
                        t = threading.Thread(target=probe,
                                             args=(os.path.join(root, name),),
                                             daemon=True)
                        t.start()
                        threads.append(t)
            for t in threads:
                t.join()
        except OSError:
            pass

    t = threading.Thread(target=run, name="meta-warmup", daemon=True)
    t.start()
    return t


def _ffprobe_video(mp4, ffmpeg_dir=""):
    """Return (width, height, fps, duration) or None."""
    exe = os.path.join(ffmpeg_dir, "ffprobe.exe") if ffmpeg_dir else "ffprobe"
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-show_entries", "format=duration",
             "-of", "csv=p=0", mp4],
            capture_output=True, timeout=20,
            creationflags=CREATE_NO_WINDOW).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None
    width = height = fps = duration = None
    for line in out.splitlines():
        fields = [f.strip() for f in line.strip().split(",") if f.strip()]
        if len(fields) >= 3:
            try:
                width = int(fields[0])
                height = int(fields[1])
                num, _, den = fields[2].partition("/")
                fps = float(num) / float(den) if den else float(num)
            except (ValueError, ZeroDivisionError):
                pass
        elif len(fields) == 1:
            try:
                duration = float(fields[0])
            except ValueError:
                pass
    if not width or not height or not fps:
        return None
    return width, height, fps, duration


class PygamePlayer:
    """A single video playback session: ffmpeg-piped frames rendered into a
    borderless pygame window positioned exactly over the game window's client
    area - as a child of the game window (window_mode="child") or as a
    floating top-level overlay that tracks the game window (window_mode=
    "overlay") - tracking the game's client geometry exactly.
    """

    def __init__(self, mp4, window_title, game_pid, initial_w, initial_h,
                 stretch, ffmpeg_exe="ffmpeg", ffprobe_exe="ffprobe",
                 video_meta=None, window_mode="child", audio_type="sdl_mixer",
                 decoder="pyav"):
        self.mp4 = mp4
        self.window_title = window_title
        self.game_pid = game_pid
        self.window_mode = window_mode  # "child" (SetParent) or "overlay" (floating top-level)
        self.audio_type = audio_type    # "sdl_mixer" (default) or "winmm"
        # Video decoder: "pyav" = in-process PyAV decode of h264/h265 (falls
        # back to the ffmpeg subprocess pipeline when PyAV is unavailable);
        # "ffmpeg" = the classic external-ffmpeg rawvideo pipe.
        self.decoder = decoder if (decoder == "ffmpeg" or HAVE_PYAV) else "ffmpeg"
        self.stretch = stretch
        self.ffmpeg = ffmpeg_exe
        self.ffprobe = ffprobe_exe
        self.video_meta = video_meta   # (w, h, fps, duration) or None
        self.initial_w = max(1, initial_w)
        self.initial_h = max(1, initial_h)
        self.stop_flag = threading.Event()
        self.thread = None
        self.video_proc = None
        self.audio_proc = None
        self.audio_pcm = b""
        self._audio_thread = None
        self._reader = None
        self._cleanup_lock = threading.Lock()
        self._wave_lock = threading.Lock()
        self.wave = None
        self.hwnd = None
        self.screen = None
        self._scratch = None        # current-size render target (GDI-presented)
        self._current_size = None   # (w, h) of the overlay window right now
        self._game_win = None
        self._orig_wndproc = None   # subclass: original WndProc (input forwarder)
        self._wndproc_obj = None    # keeps the WNDPROC_T object alive
        self._parent_hwnd = None    # game window that receives forwarded input
        self._attached = False      # our window is attached over the game window
        self.attached_evt = threading.Event()  # set when the attach completes
        self._target = None         # (x, y) of the game client on screen (overlay mode)
        self._last_topmost = 0.0    # last time we re-asserted the topmost z-order (overlay)
        self._last_rect_read = 0.0  # throttle: last GetClientRect(game) read
        self._last_z_assert = 0.0    # last local z-order assertion
        self._game_windowed = None  # None=unknown, True=windowed, False=fullscreen/borderless
        self._video_start = None    # wall clock when the first frame was shown
        # A/V start gate state (see _release_av / _load_audio): the clock and
        # the audio stream start together, once BOTH the first frame has been
        # drawn on a ready window AND the audio stream is fully prepared.
        self._frame_drawn = False
        self._audio_ready = threading.Event()  # stream prepared (or silent)
        self._audio_pending = None  # prepared-but-not-started stream
        self._av_released = False
        self._av_lock = threading.Lock()
        self._dib_mem_dc = None     # cached UpdateLayeredWindow scratch DC
        self._dib_bmp = None        # cached DIB section handle
        self._dib_size = None       # (w, h) of the cached DIB
        self._heartbeat = time.time()  # bumped every loop iteration (watchdog)
        # Which display this session uses: "none" (not created yet), "shared"
        # (the overlay's pre-created display is reused; see precreate_display)
        # or "owned" (this session created the display itself). Decides
        # whether teardown quits the display or just hides the window.
        self._display_state = "none"
        # Shared-display mode: this session runs on the overlay's display
        # thread (see _DisplayThread) instead of its own thread, reusing the
        # pre-created window. _finished is set when the session ends, so
        # is_alive() works without an owned thread.
        self._shared = False
        self._finished = threading.Event()
        self._finished.set()
        self._stack_watch = threading.Thread(target=self._stack_watchdog,
                                             name="player-stack-watch", daemon=True)
        self._stack_watch.start()

    def _stack_watchdog(self):
        """Dump the render thread's Python stack if the heartbeat goes stale.
        Only used for diagnosing the intermittent freeze; a few lines in the
        debug log are harmless. In shared-display mode the session runs on the
        display thread, so that is the thread whose stack gets dumped."""
        while True:
            time.sleep(2.0)
            if not self.is_alive():
                return
            th = self.thread
            if th is None and self._shared:
                th = _display_thread.thread
            if th is None:
                continue
            if time.time() - self._heartbeat <= 3.0:
                continue
            if getattr(self, "_stack_dumped", False):
                continue
            self._stack_dumped = True
            try:
                import sys as _sys
                frames = _sys._current_frames()
                f = frames.get(th.ident)
                lines = []
                while f is not None:
                    lines.append("%s:%d %s" % (f.f_code.co_filename,
                                                f.f_lineno, f.f_code.co_name))
                    f = f.f_back
                dbg("SELF-STALL stack: " + " < ".join(lines))
            except Exception:
                pass

    # -- lifecycle ---------------------------------------------------------

    def _use_shared(self):
        """True when this session should run on the overlay's display-owner
        thread instead of its own thread. Once the display thread is alive
        (pre-creation succeeded; child mode only), it is the ONLY thread
        allowed to touch the pygame display - every session, shared or
        owned, must run there. A session that had to recreate its window
        after the game destroyed it abandons the pre-created display
        (SHARED_DISPLAY becomes False) but must still run on this thread:
        its own-thread set_mode would race the display thread's idle event
        pump (cross-thread pygame set_mode hangs the whole process -
        measured)."""
        if self.window_mode != "child":
            return False
        return _display_thread.thread.is_alive()

    def start(self):
        if self._use_shared():
            self._shared = True
            _display_thread.submit(self)
            return
        self.thread = threading.Thread(target=self._run, name="video-player",
                                       daemon=True)
        self.thread.start()

    def is_alive(self):
        if self._shared:
            return not self._finished.is_set()
        return self.thread is not None and self.thread.is_alive()

    def stalled(self, timeout=5.0):
        """True if the render loop has not pumped for `timeout` seconds while
        the thread is still alive - the window would be frozen (no message
        pump). The overlay watchdog uses this to tear the player down."""
        stalled = (self.is_alive()
                   and (time.time() - self._heartbeat) > timeout)
        if stalled:
            import sys as _sys
            frames = _sys._current_frames()
            th = self.thread
            if th and th.ident in frames:
                f = frames[th.ident]
                lines = []
                while f is not None:
                    lines.append("%s:%d %s" % (f.f_code.co_filename,
                                                f.f_lineno, f.f_code.co_name))
                    f = f.f_back
                dbg("PLAYER STALLED, stack: " + " < ".join(lines))
            else:
                dbg("PLAYER STALLED, thread=%r ident=%r frames=%r" %
                    (th, th and th.ident, list(frames)))
        return stalled

    def _close_wave(self):
        with self._wave_lock:
            wave = self.wave
            self.wave = None
        if wave is not None:
            try:
                wave.close()
            except Exception:
                pass

    def _show_window_now(self):
        """Show the player after its backing surface has been cleared."""
        hwnd = self.hwnd
        if not hwnd or not _user32.IsWindow(hwnd):
            return
        insert_after = HWND_TOPMOST if self.window_mode == "overlay" else HWND_TOP
        try:
            _user32.SetWindowPos(
                hwnd, insert_after, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
                SWP_SHOWWINDOW)
        except Exception:
            try:
                _user32.ShowWindow(hwnd, SW_SHOW)
            except Exception:
                pass

    def _hide_window_now(self):
        """Hide the player immediately from the cancellation caller.

        The shared pygame display must still be torn down by its owner thread,
        but Win32 permits hiding the window from another thread. This removes
        the player before the game reaches its menu while the owner finishes
        stopping decoder/audio workers asynchronously.
        """
        hwnd = self.hwnd
        if hwnd and _user32.IsWindow(hwnd):
            try:
                _user32.ShowWindow(hwnd, SW_HIDE)
            except Exception:
                try:
                    _user32.SetWindowPos(
                        hwnd, HWND_TOP, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
                        SWP_HIDEWINDOW)
                except Exception:
                    pass

    def kill_fast(self):
        """Signal immediate stop from an external cancellation path. Unlike
        stop(), this hides the window right away - used by the overlay watchdog
        when the render loop is stalled and cannot run the interrupt hold (a
        stuck player's window must not linger on screen)."""
        self.stop_flag.set()
        self._kill_video_proc()
        self._kill_audio_proc()
        self._close_wave()
        self._hide_window_now()

    def stop(self, timeout=0.0):
        """Cancel playback; display-thread cleanup continues asynchronously.

        Decoder/audio cancellation is synchronous, but the window is NOT hidden
        here: after the render loop notices stop_flag it keeps the last frame
        visible for INTERRUPT_HOLD (see _run_inner), so the hide reveals the
        game's already-redrawn menu instead of its momentarily-black video
        surface (the black flash on ESC). The display owner then hides the
        window and performs the remaining SDL teardown. `timeout` is retained
        for callers/API compatibility but intentionally does not block the
        normal cancellation path.
        """
        self.stop_flag.set()
        self._kill_video_proc()
        self._kill_audio_proc()
        self._close_wave()

    def _kill_video_proc(self):
        # PyAV path: there is no subprocess to terminate - cancel the reader
        # thread instead (it unblocks its queue and closes the container).
        if self._reader is not None and hasattr(self._reader, "stop"):
            try:
                self._reader.stop()
            except Exception:
                pass
        try:
            proc = self.video_proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
            if proc is not None and proc.stdout is not None:
                proc.stdout.close()
        except (OSError, ValueError):
            pass

    def _kill_audio_proc(self):
        try:
            proc = self.audio_proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
        except OSError:
            pass

    def _cleanup(self):
        with self._cleanup_lock:
            try:
                if self.video_proc is not None and self.video_proc.poll() is None:
                    self.video_proc.kill()
            except OSError:
                pass
            self._close_wave()
        # NB: pygame.display.quit() is deliberately NOT called here. It may run
        # on the overlay's thread (via stop()), and quitting SDL from a thread
        # other than the one that created the display - while that thread is
        # still inside SDL - can deadlock. The player thread always quits its
        # own display in _run()'s finally, so a stuck thread here can only be
        # recovered by the process-level watchdog, never by SDL teardown.

    # -- main render loop --------------------------------------------------

    def _clock(self):
        """Master clock. Once the audio stream is live it is the audio clock;
        until then (background decode still running) wall time since the first
        frame, so the video starts immediately instead of waiting for audio.

        The audio clock is sanity-checked against wall time: if the audio
        device stops advancing (it can wedge when the game competes for the
        audio device), the master clock falls back to wall time so playback
        continues instead of freezing on the audio clock."""
        if self.wave is not None:
            ac = self.wave.clock()
            wall = (time.time() - self._video_start) if self._video_start else 0.0
            if ac is not None:
                # audio ahead of wall by more than ~2s -> it restarted from a
                # stale offset; behind by more than ~2s -> the device stalled.
                # Either way, trust wall time (still A/V synced enough).
                if abs(ac - wall) > 2.0:
                    return wall
                return ac
            return wall
        if self._video_start is None:
            return 0.0
        return time.time() - self._video_start

    def _load_audio(self, ready, go):
        """Decode the whole audio track into memory in the background. Once
        the video's first frame has been drawn (go event - see the A/V start
        gate in _run_inner) the audio stream is opened with clock_offset =
        the current video clock, so sound starts exactly with the picture and
        nothing is trimmed (the decode finished while the window was being
        created). If the decode is slow, elapsed > 0 and the PCM is trimmed
        by the same amount so sync is still exact.

        The output backend follows self.audio_type: "sdl_mixer" (the default)
        plays through SDL_Mixer (pygame.mixer) and falls back to WinMM waveOut
        only if the mixer fails; "winmm" plays through legacy WinMM waveOut
        directly, skipping SDL_Mixer entirely."""
        if os.environ.get("OVERLAY_NO_AUDIO"):
            self._audio_ready.set()  # bisect: silent playback, never hold gate
            ready.set()
            return
        try:
            if self.stop_flag.is_set():
                ready.set()
                return
            if self.decoder == "pyav":
                # In-process PyAV decode: no ffmpeg.exe spawn, so the audio
                # track never waits on an external process cold-start.
                pcm = _decode_audio_av(self.mp4) if HAVE_PYAV else b""
                if self.stop_flag.is_set():
                    ready.set()
                    return
            else:
                self.audio_proc = subprocess.Popen(
                    [self.ffmpeg, "-v", "error", "-i", self.mp4, "-map", "0:a:0",
                     "-f", "s16le", "-ac", str(AUDIO_CHANNELS),
                     "-ar", str(AUDIO_RATE), "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW)
                if self.stop_flag.is_set():
                    self._kill_audio_proc()
                out, _ = self.audio_proc.communicate(timeout=30)
                pcm = out if (self.audio_proc.returncode == 0 and out) else b""
        except subprocess.TimeoutExpired:
            self._kill_audio_proc()
            try:
                self.audio_proc.communicate(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
            pcm = b""
        except (OSError, subprocess.SubprocessError):
            pcm = b""
        finally:
            self.audio_proc = None
        self.audio_pcm = pcm
        if not pcm:
            # Silent video (or decode failure): never let the A/V gate wait
            # on us - the render loop releases as soon as its frame is drawn.
            self._audio_ready.set()
            ready.set()
            return
        if self.stop_flag.is_set():
            ready.set()
            return
        # Prepare the output stream NOW, in parallel with the video pipeline's
        # first-frame decode: mixer init, the whole-buffer Sound build and the
        # waveOut device open are the expensive parts. Staging them here makes
        # the eventual start() a single call, so the A/V gate can release the
        # instant BOTH sides are ready and sound always begins exactly with
        # frame 0 (previously the stream opened whenever the full decode
        # happened to finish - on a slow one the narration's first syllable
        # was gone before any sound played).
        stream = None
        backend = None
        if self.audio_type != "winmm":
            # Default: SDL_Mixer (pygame.mixer) first; WinMM waveOut is only
            # the fallback if the mixer fails (no audio device, an old pygame
            # without Sound(buffer=), etc.).
            try:
                stream = SdlMixerStream(pcm)
                stream.prepare()
                backend = "sdl_mixer"
            except Exception as e:
                logging.warning("%s: SDL_Mixer unavailable (%s); falling "
                                "back to waveOut", self.window_title, e)
                stream = None
        if stream is None:
            try:
                stream = WaveOutStream(pcm)
                stream.prepare()
                backend = "winmm"
            except Exception as e:
                logging.warning("%s: audio unavailable (%s); playing "
                                "silent", self.window_title, e)
                stream = None
        if stream is not None:
            self._audio_pending = stream
        # Success OR silent-failure: either way the gate must not wait.
        self._audio_ready.set()
        self._release_av(go)
        ready.set()

        # Wait for the A/V gate (clock start). Poll so a stop() during the
        # wait is noticed promptly instead of after the full timeout.
        deadline = time.time() + 10.0
        while not self.stop_flag.is_set():
            if go.wait(timeout=0.2):
                break
            if time.time() >= deadline:
                break
        # Only proceed on a GENUINE gate release: go can also be set by
        # session teardown (or the deadline force) - in those cases the
        # staged stream must be discarded, never played.
        if self.stop_flag.is_set() or not self._av_released:
            self._discard_pending()
            return

        elapsed = self._clock()
        trim = int(elapsed * AUDIO_RATE) * AUDIO_BLOCK
        trim = max(0, min(trim, len(pcm) - AUDIO_RATE * AUDIO_BLOCK))
        if trim > 0 and self._audio_pending is not None:
            # Degraded join: the gate forced open before we finished preparing
            # (AV_START_TIMEOUT). Rebuild at the right playback position.
            try:
                self._audio_pending.close()
            except Exception:
                pass
            self._audio_pending = None
            try:
                if backend == "sdl_mixer":
                    stream = SdlMixerStream(pcm[trim:], clock_offset=elapsed)
                else:
                    stream = WaveOutStream(pcm[trim:], clock_offset=elapsed)
                stream.prepare()
            except Exception as e:
                logging.warning("%s: audio re-prepare failed (%s); playing "
                                "silent", self.window_title, e)
                stream = None
        elif self._audio_pending is not None:
            stream = self._audio_pending
        self._audio_pending = None
        if stream is None:
            return
        dbg("audio stream starting at offset %.3fs" % elapsed)
        stream.start()
        with self._wave_lock:
            cancelled = self.stop_flag.is_set()
            if not cancelled:
                self.wave = stream
        if cancelled and stream is not None:
            stream.close()

    def _discard_pending(self):
        """Tear down a prepared-but-not-started audio stream."""
        st = self._audio_pending
        self._audio_pending = None
        if st is not None:
            try:
                st.close()
            except Exception:
                pass

    def _release_av(self, go, forced_reason=None):
        """Start the playback clock exactly when picture and sound can both
        begin. Called by the render loop after the first real frame has been
        drawn on a ready window AND by the audio worker once its output
        stream is fully prepared - whichever happens last wins, so neither
        side ever starts alone (no silent moving lips, no sound over black).
        `forced_reason` bypasses both conditions (gate timeout)."""
        with self._av_lock:
            if self._av_released:
                return
            if forced_reason is None and not (
                    self._frame_drawn and self._audio_ready.is_set()):
                return
            self._av_released = True
            self._video_start = time.time()
        if forced_reason:
            dbg("av gate FORCED (%s)" % forced_reason)
        else:
            dbg("av gate released: frame drawn + audio prepared")
        go.set()

    def _finish_session(self):
        """Make every exit path cancel all external workers before the window
        is hidden or the shared display is returned to its idle queue."""
        self.stop_flag.set()
        self._kill_video_proc()
        self._kill_audio_proc()
        self._close_wave()
        if (not self.stop_flag.is_set()
                and self._audio_thread is not None
                and self._audio_thread.is_alive()):
            self._audio_thread.join(timeout=0.1)

    def _run(self):
        """Playback session body. Everything is wrapped so that no matter what
        fails, the pygame display is not left behind - a surviving window with
        a dead message-pumping thread freezes ("Not Responding") and blocks
        nothing else, so it must never be left behind. When the session owns
        the display it is quit; when it reused the overlay's pre-created
        display, the window is hidden for the next session instead."""
        try:
            self._run_inner()
        except Exception:
            logging.exception("%s: player crashed", self.window_title)
        finally:
            self._finish_session()
            self._teardown_display()

    def _run_shared(self):
        """Session body for the shared-display path. Runs on the display
        thread (the thread that created the display), so every pygame display
        call happens on its owning thread. Mirrors _run()."""
        try:
            self._run_inner()
        except Exception:
            logging.exception("%s: player crashed", self.window_title)
        finally:
            self._finish_session()
            self._teardown_display()

    def _run_inner(self):
        global SHARED_DISPLAY
        if self.stop_flag.is_set():
            return
        dbg("=== _run start mp4=%s ===" % self.mp4)
        t0 = time.time()
        info = self.video_meta
        if info is None:
            info = _meta_from_txt(self.mp4)
        if info is None and self.decoder == "pyav":
            # PyAV mode: probe in-process too - never spawn an external tool.
            info = _av_video_meta(self.mp4)
            dbg("pyav probe took %.2fs -> %r" % (time.time() - t0, info))
        if info is None:
            info = _ffprobe_video(self.mp4, os.path.dirname(self.ffprobe) if self.ffprobe else "")
            dbg("ffprobe took %.2fs -> %r" % (time.time() - t0, info))
        if info is None:
            logging.error("%s: ffprobe failed", self.window_title)
            return
        width, height, fps, duration = info
        frame_bytes = width * height * 3
        if self.stop_flag.is_set():
            return

        # Start decoding FIRST, before the window is created, so frames are
        # already queued by the time the window is ready and the video
        # appears the moment the game's dummy playback starts. PyAV decodes
        # in-process (no external process to spawn or warm); ffmpeg spawns
        # its rawvideo pipe here. Either way the frame-reader thread keeps
        # the render loop decoupled from the decoder.
        if self.decoder == "pyav":
            reader = _AvFrameReader(self.mp4, width, height)
            self._reader = reader
            reader.start()
        else:
            try:
                self.video_proc = subprocess.Popen(
                    [self.ffmpeg, "-v", "error", "-i", self.mp4, "-map", "0:v:0",
                     "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW)
            except OSError as e:
                logging.error("%s: failed to start ffmpeg: %s", self.window_title, e)
                return
            if self.stop_flag.is_set():
                self._kill_video_proc()
                return
            reader = _FrameReader(self.video_proc, frame_bytes)
            self._reader = reader
            reader.start()

        # Decode the audio in the background BEFORE creating the window, so by
        # the time the first frame is shown the PCM is already in memory and
        # the audio stream opens instantly (no intro audio skipped). The
        # Decode audio in the background. The decoder must never delay the
        # first picture: the render loop's bounded AV gate releases on the
        # first frame (or its short timeout), while audio joins when ready.
        audio_ready = threading.Event()
        audio_go = threading.Event()
        self._audio_thread = threading.Thread(
            target=self._load_audio, args=(audio_ready, audio_go),
            name="audio-load", daemon=True)
        self._audio_thread.start()

        # Create/acquire the window immediately (before any audio work) so the
        # overlay appears the moment the video starts - no 1-2s blank delay.
        #
        # When the overlay pre-created the pygame display at startup (child
        # mode only; see precreate_display), the hidden window already exists,
        # so this session only RESIZES it (a cheap SDL_SetWindowSize) instead
        # of paying the cold display init + window creation again. The window
        # is shown later in _attach_to_game with SWP_NOACTIVATE|SWP_SHOWWINDOW,
        # so focus never leaves the game. (SDL's set_mode must not activate
        # the window: activation would steal keyboard focus from the game, and
        # the game only honors Alt+Enter when ITS thread owns the keyboard
        # focus - which is why Alt+Enter felt unresponsive for the first
        # seconds of playback.)
        #
        # Otherwise the display is owned by this session: in child mode it is
        # created HIDDEN exactly as above. OVERLAY mode creates the window
        # VISIBLE, exactly like the proven-working standalone overlay: a
        # window shown for the first time with SWP_SHOWWINDOW from a
        # background process (no foreground lock, SWP_NOACTIVATE) comes up
        # BEHIND the active foreground window - so if the user clicked/dragged
        # the game window before playback (making it the foreground window),
        # the overlay would draw behind the game no matter how often its
        # z-order was re-asserted afterwards. A window created visible is
        # placed above the game at creation time; WS_EX_NOACTIVATE is applied
        # in _attach_to_game immediately after, so focus still never leaves
        # the game. (That is why pre-creation is child-mode-only.)
        flags = pygame.NOFRAME
        if self.window_mode == "child":
            flags |= pygame.HIDDEN
        self._display_state = "none"
        try:
            if (SHARED_DISPLAY and self.window_mode == "child"
                    and not os.environ.get("OVERLAY_NO_PRECREATE")):
                # Reuse the overlay's pre-created display/window: resize only.
                t0 = time.time()
                self.screen = pygame.display.set_mode(
                    (self.initial_w, self.initial_h), flags)
                dbg("reused pre-created display; set_mode took %.2fs "
                    "(requested %dx%d)" % (time.time() - t0,
                                            self.initial_w, self.initial_h))
                self._display_state = "shared"
            else:
                # Own this display for the session (cold init + window create).
                t0 = time.time()
                pygame.display.init()
                dbg("pygame.display.init took %.2fs" % (time.time() - t0))
                t0 = time.time()
                self.screen = pygame.display.set_mode(
                    (self.initial_w, self.initial_h), flags)
                dbg("set_mode took %.2fs (requested %dx%d)"
                    % (time.time() - t0, self.initial_w, self.initial_h))
                self._display_state = "owned"
            dbg("hwnd=%s" % self._own_hwnd())
        except pygame.error as e:
            if self._display_state == "shared":
                # The pre-created display became unusable (e.g. a previous
                # session's fallback quit it): fall back to owning a fresh
                # one, exactly like the non-precreated path.
                SHARED_DISPLAY = False
                self._display_state = "none"
                try:
                    pygame.display.quit()
                except Exception:
                    pass
                try:
                    t0 = time.time()
                    pygame.display.init()
                    dbg("fallback: pygame.display.init took %.2fs"
                        % (time.time() - t0))
                    self.screen = pygame.display.set_mode(
                        (self.initial_w, self.initial_h), flags)
                    dbg("fallback: set_mode took %.2fs (requested %dx%d)"
                        % (time.time() - t0, self.initial_w, self.initial_h))
                    self._display_state = "owned"
                except pygame.error as e2:
                    logging.error("%s: pygame display failed: %s",
                                  self.window_title, e2)
                    self._cleanup()
                    return
            else:
                logging.error("%s: pygame display failed: %s",
                              self.window_title, e)
                self._cleanup()
                return
        self.hwnd = self._own_hwnd()
        pygame.display.set_caption(self.window_title)
        pygame.event.set_allowed([])  # we don't need any SDL events
        # The moment the window exists, park the mouse at the bottom-right
        # corner of the screen (the last pixel): the game hides the system
        # cursor during its dummy video and draws its own, so SDL's white
        # cursor would otherwise appear in the middle of the player window.
        self._park_cursor()
        # Overlay mode creates its first window visible so it is initially
        # placed above the active game. Hide it immediately while the backing
        # surface is positioned and cleared, then show it again at the correct
        # z-order.
        self._hide_window_now()
        # Keep a reused window hidden while it is positioned and cleared. This
        # prevents the previous video's last frame from being visible during
        # the attach/resize work or while the first frame is still decoding.
        self._attach_to_game(show=False)
        # Position/size the window now (before the first frame) so the
        # overlay never flashes at a wrong spot.
        self._sync_window(show=False)
        self._present_black()
        if self._attached:
            self._show_window_now()
        dbg("cleared stale frame before showing attached window")

        # A/V start gate (see _release_av): picture starts as soon as the
        # first frame is drawn; audio is prepared concurrently and joins at
        # the next safe clock point. The short gate prevents cold ffmpeg/audio
        # startup from making the first video appear late.
        # The deadline forces release even if one side never becomes ready.
        av_started = False
        av_deadline = time.time() + AV_START_TIMEOUT
        frame_idx = 0
        last_surface = None
        render_error_logged = False
        try:
            while not self.stop_flag.is_set():
                self._heartbeat = time.time()
                if not av_started and time.time() >= av_deadline:
                    av_started = True
                    self._release_av(audio_go, "timeout %.1fs"
                                     % AV_START_TIMEOUT)
                # Escape is intentionally not handled by the player. It stays
                # in the normal window/input path so the game decides whether
                # to skip the dummy video; the watcher stops the MP4 only when
                # the dummy is actually closed.
                try:
                    frame = reader.frames.get(timeout=0.05)
                except queue.Empty:
                    # No frame yet (still decoding, or an ffmpeg hiccup): keep
                    # the window alive, pumped and resize-aware instead of
                    # blocking on the pipe.
                    self._sync_window()
                    pygame.event.pump()
                    if reader.eof.is_set():
                        dbg("video EOF at frame_idx=%d" % frame_idx)
                        break
                    continue
                if len(frame) < frame_bytes:
                    continue
                if frame_idx % 30 == 0:
                    dbg("frame %d pts=%.2f clock=%.2f" % (frame_idx, frame_idx / fps, self._clock()))
                if frame_idx <= 3:
                    t0 = time.time()
                pts = frame_idx / fps
                clock = self._clock()
                if frame_idx <= 3:
                    dbg("loop frame %d got frame in %.3fs" % (frame_idx, time.time() - t0))

                # Way behind the audio clock: drop frames to catch up.
                if pts < clock - AV_DROP_BEHIND:
                    frame_idx += 1
                    continue

                # Ahead of the audio clock: wait. Events are pumped throughout
                # the wait so the window never goes "Not Responding", and the
                # game window is re-read so a resize reacts immediately.
                # The wait is also bounded by a wall-clock deadline: if the
                # audio clock ever stops advancing (the audio device can wedge
                # when the game grabs the audio device), the video must keep
                # playing on wall time instead of freezing forever.
                wait_deadline = time.time() + 1.0
                while (clock < pts - AV_LEAD
                       and not self.stop_flag.is_set()
                       and time.time() < wait_deadline):
                    self._heartbeat = time.time()
                    self._sync_window()
                    pygame.event.pump()
                    time.sleep(0.005)
                    clock = self._clock()
                if self.stop_flag.is_set():
                    break

                self._sync_window()
                try:
                    surf = pygame.image.frombuffer(frame, (width, height), "RGB")
                    self._render(surf)
                    last_surface = surf
                except pygame.error as e:
                    logging.error("%s: render error: %s", self.window_title, e)
                    break
                except Exception as e:
                    # A non-pygame error here (e.g. smoothscale on a huge
                    # surface) must never kill the thread: that would leave the
                    # window up with no message pump and it would freeze. Log
                    # once and keep playing silently.
                    if not render_error_logged:
                        logging.exception("%s: render loop error: %s", self.window_title, e)
                        dbg("render loop error: %r" % e)
                        render_error_logged = True
                if not av_started:
                    # First real frame is on screen and the window is ready
                    # to draw: mark our side of the gate. Release happens the
                    # moment the audio side is prepared too (or on timeout).
                    av_started = True
                    self._frame_drawn = True
                    dbg("first frame drawn; waiting for audio prepare")
                    self._release_av(audio_go)
                pygame.event.pump()
                frame_idx += 1
        finally:
            # Wake the audio worker's go.wait immediately on any teardown;
            # its own stop_flag checks decide whether a stream may open.
            audio_go.set()
            self._kill_video_proc()
            # If the video ends before background audio decode completes, give
            # it a short chance to publish its stream. Otherwise cancel it
            # before teardown so it cannot start audio after the window hides.
            if not self.stop_flag.is_set() and not audio_ready.wait(timeout=2.0):
                self.stop_flag.set()
                self._kill_audio_proc()
            if (not self.stop_flag.is_set()
                    and self._audio_thread is not None
                    and self._audio_thread.is_alive()):
                # Natural EOF may need a brief handoff for audio tail timing;
                # cancellation must never hold the shared display thread.
                self._audio_thread.join(timeout=0.1)
            # Cancellation closes stdout in _kill_video_proc(), so do not
            # hold the shared display thread waiting for a daemon reader. A
            # natural EOF still gets a bounded join for orderly teardown.
            if (self._reader is not None and self._reader.is_alive()
                    and not self.stop_flag.is_set()):
                self._reader.join(timeout=0.1)

        # Video stream ended: hold the last frame until the audio finishes.
        with self._wave_lock:
            wave = self.wave
        if wave is not None:
            deadline = time.time() + 2.0
            while (not wave.finished.is_set()
                   and not self.stop_flag.is_set()
                   and time.time() < deadline):
                self._heartbeat = time.time()
                self._sync_window()
                pygame.event.pump()
                if last_surface is not None:
                    try:
                        self._render(last_surface)
                    except Exception:
                        pass
                time.sleep(0.05)
        # INTERRUPTED (ESC / the game closed the dummy): the game stops its own
        # video and redraws the menu UNDER our window. Hiding immediately would
        # expose the game's not-yet-redrawn (black) video surface - the black
        # flash. Hold the last frame for a short grace so the hide reveals the
        # already-drawn menu instead. stop()/kill_fast() do not hide the window
        # synchronously on this path (stop() leaves it visible; kill_fast is
        # only used when this loop is stalled and cannot run this block), and
        # _sync_window() re-asserts SWP_SHOWWINDOW throughout, so the frame
        # stays on screen for the whole grace.
        if (self.stop_flag.is_set() and last_surface is not None
                and self.hwnd and _user32.IsWindow(self.hwnd)
                and _user32.IsWindowVisible(self.hwnd)):
            hold_until = time.time() + INTERRUPT_HOLD
            while time.time() < hold_until:
                self._heartbeat = time.time()
                self._sync_window()
                pygame.event.pump()
                if not _user32.IsWindowVisible(self.hwnd):
                    break
                try:
                    self._render(last_surface)
                except Exception:
                    pass
                time.sleep(0.01)
            dbg("held last frame %.0f ms after interrupt" % (INTERRUPT_HOLD * 1000))
        # Playback ended: restore the mouse to the game window's center and
        # hide the system cursor (the game draws its own cursor there).
        self._restore_cursor()
        self._cleanup()
        self._close_wave()
        if (not self.stop_flag.is_set()
                and self._audio_thread is not None
                and self._audio_thread.is_alive()):
            self._audio_thread.join(timeout=0.1)
        # Display teardown (quit if we own the display, hide the window if we
        # reused the overlay's pre-created one) runs in _run()'s finally.

    # -- window / geometry -------------------------------------------------

    def _own_hwnd(self):
        """Our window's HWND via pygame's WM info - reliable even when the
        window is a child (children do not appear in top-level EnumWindows)."""
        try:
            return pygame.display.get_wm_info().get("window")
        except (pygame.error, AttributeError):
            return None

    def _find_own_window(self):
        hwnd = self._own_hwnd()
        if hwnd:
            return hwnd
        return find_window_by_pid_and_title(os.getpid(), self.window_title)

    def _game_window(self):
        # A fullscreen/windowed transition can leave the old top-level HWND
        # valid but hidden while the game has already created its replacement.
        # Do not keep attaching/tracking against that stale HWND.
        if (self._game_win and _user32.IsWindow(self._game_win)
                and _user32.IsWindowVisible(self._game_win)):
            return self._game_win
        self._game_win = find_game_main_window(self.game_pid) if self.game_pid else None
        return self._game_win

    def _reassert_z_order(self, hwnd, show=True):
        """Keep the player above the game's video surface without touching the
        game's window or its message queue.

        The game may re-raise its own child window, or move itself within the
        topmost band, several times during a fullscreen transition. Geometry
        reads are intentionally throttled, so z-order recovery must not depend
        on a geometry change. A 50ms local assertion is inexpensive and avoids
        the synchronous cross-process SetWindowPos call that previously caused
        playback stalls when applied to the game HWND."""
        now = time.monotonic()
        if now - self._last_z_assert < Z_ORDER_ASSERT_INTERVAL:
            return
        self._last_z_assert = now
        insert_after = (HWND_TOPMOST if self.window_mode == "overlay"
                        else HWND_TOP)
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        if show:
            flags |= SWP_SHOWWINDOW
        _user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, flags)

    def _attach_to_game(self, show=True):
        """Position our window over the game window's client area.

        `show=False` is used during startup so a reused window can be cleared
        before its first visible frame.

        window_mode == "child":  make our window a CHILD of the game window
        (SetParent). A child window is clipped to the parent's client area,
        moves and resizes with it automatically, and never appears elsewhere
        on screen. The input forwarder relays keys/clicks to the game.

        window_mode == "overlay": keep our window a top-level WS_EX_TOPMOST
        layered window floating exactly over the game window's client area
        (re-tracked every frame by _sync_window). WS_EX_NOACTIVATE keeps
        keyboard focus on the game (ESC reaches it unchanged) and
        WS_EX_TRANSPARENT passes mouse clicks through to the game - no input
        forwarder needed because the overlay is never activated and the game
        keeps focus.

        Either way frames are composited with UpdateLayeredWindow at the
        window's live size."""
        gw = self._game_window()
        hwnd = self.hwnd or self._find_own_window()
        if hwnd and not _user32.IsWindow(hwnd):
            # Stale handle: the game window that parented our child was
            # destroyed and recreated (fullscreen toggle, or a transition
            # between cutscenes), taking our window with it. pygame never
            # sees the external destruction, so get_wm_info keeps returning
            # the dead handle - re-resolve it and, if it is really gone,
            # report failure so _sync_window recreates the window instead of
            # "attaching" to a dead one (which would render frames to
            # nothing: audio plays, no visuals - the second-video black
            # screen).
            hwnd = self._find_own_window()
            if hwnd and not _user32.IsWindow(hwnd):
                hwnd = None
        if not gw or not hwnd:
            return False
        self.hwnd = hwnd
        layered = not os.environ.get("OVERLAY_NO_LAYERED")
        if self.window_mode == "overlay":
            # Top-level popup - never WS_CHILD: a top-level window is
            # composited by DWM above the game's DirectDraw blits (no
            # parent/child repaint fight). WS_EX_TOPMOST keeps it above the
            # game window; NOACTIVATE + TRANSPARENT pass focus and clicks
            # through to the game.
            style = _user32.GetWindowLongW(hwnd, GWL_STYLE)
            _user32.SetWindowLongW(hwnd, GWL_STYLE, (style | WS_POPUP) & ~WS_CHILD)
            ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            _user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                   (ex | WS_EX_TOPMOST | WS_EX_NOACTIVATE |
                                    (WS_EX_LAYERED if layered else 0) | WS_EX_TRANSPARENT))
            # Position over the game's client area immediately - never leave
            # it at the SDL default position (a misplaced overlay flashes at
            # the wrong spot until the first sync).
            rect = _client_rect_on_screen(gw)
            x, y, cw, ch = rect if rect else (0, 0, self.initial_w, self.initial_h)
            flags = SWP_FRAMECHANGED | SWP_NOACTIVATE
            if show:
                flags |= SWP_SHOWWINDOW
            _user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, cw, ch, flags)
            self._target = (x, y)
            self._current_size = (cw, ch)
            self._attached = True
            dbg("attached overlay hwnd=%s over game %s at (%d,%d) %dx%d" %
                (hwnd, gw, x, y, cw, ch))
            _dbg_window_state(self, hwnd, "after overlay attach")
            self.attached_evt.set()
            return True
        # ---- child mode (default) ----
        # WS_CHILD + SetParent to the game window. Child coordinates are
        # relative to the parent's client origin, so position (0, 0).
        style = _user32.GetWindowLongW(hwnd, GWL_STYLE)
        _user32.SetWindowLongW(hwnd, GWL_STYLE, (style | WS_CHILD) & ~WS_POPUP)
        _user32.SetParent(hwnd, gw)
        # WS_EX_LAYERED keeps DWM compositing the per-frame bitmap above the
        # game's DirectDraw blits within the parent; NOACTIVATE + TRANSPARENT
        # pass focus and clicks through. No TOPMOST (children have no z-order
        # outside their parent).
        ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                               (ex | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT |
                                (WS_EX_LAYERED if layered else 0)) & ~WS_EX_TOPMOST)
        # Position over the game's client area immediately - never leave it at
        # the SDL default position (a misplaced child flashes at the wrong
        # spot until the first sync). Child coords: (0, 0).
        rect = _client_rect_on_screen(gw)
        cw, ch = (rect[2], rect[3]) if rect else (self.initial_w, self.initial_h)
        flags = SWP_FRAMECHANGED | SWP_NOACTIVATE
        if show:
            flags |= SWP_SHOWWINDOW
        _user32.SetWindowPos(hwnd, None, 0, 0, cw, ch, flags)
        # Belt and suspenders: SWP_SHOWWINDOW above is normally enough, but a
        # window hidden between sessions is re-shown with an explicit
        # ShowWindow so no environment quirk can leave it invisible.
        if show:
            _user32.ShowWindow(hwnd, SW_SHOW)
        self._target = (0, 0)
        self._current_size = (cw, ch)
        self._attached = True
        _dbg_window_state(self, hwnd, "after child attach")
        # The input forwarder relays keys and clicks to the game window
        # regardless of which window has keyboard focus, so Alt+Enter works
        # without clicking first. Deliberately NO SetFocus/SetForegroundWindow
        # here: those are synchronous cross-process calls that block until the
        # game's thread processes WM_SETFOCUS/WM_ACTIVATE - during the busy
        # intro the game does not pump promptly and our player thread wedges
        # (measured: watchdog had to force-stop the player). The overlay's
        # focus_game_window() (background thread, AttachThreadInput trick)
        # re-grabs foreground for the game once attached_evt is set.
        self._install_input_forwarder()
        dbg("attached child hwnd=%s to game %s %dx%d" % (hwnd, gw, cw, ch))
        self.attached_evt.set()
        return True

    def _install_input_forwarder(self):
        """Subclass our window so keyboard and mouse-button messages are
        forwarded to the game window (the parent). This is the safety net for
        Alt+Enter and clicks when focus is on the player window instead of the
        game: every input message is relayed to the game's WndProc, then the
        original WndProc (SDL's) still runs so SDL stays consistent.

        Messages are posted with PostMessage (asynchronous, never blocks): the
        WndProc runs on the render thread, so a synchronous SendMessage to the
        game would stall rendering for every keypress (measured: pressing
        Alt+Enter paused the video for several frames). The old subclass
        deadlocked by calling UpdateLayeredWindow inside BeginPaint/EndPaint -
        this subclass does NO GDI work at all, it only relays input."""
        hwnd = self.hwnd or self._find_own_window()
        gw = self._game_window()
        if not hwnd or not gw or self._orig_wndproc is not None:
            return
        self.hwnd = hwnd
        self._parent_hwnd = gw
        orig = [None]

        def wndproc(h, msg, wp, lp):
            # Alt+Enter must NEVER reach SDL: SDL2 toggles the window to
            # fullscreen on Alt+Enter by itself, and our window is a CHILD of
            # the game - a fullscreen child covers the screen (the game window
            # "disappears", the taskbar disappears). Only the game may toggle;
            # forward the keypress to the game and swallow it here.
            if (msg == WM_SYSKEYDOWN and wp == VK_RETURN
                    and (lp & KF_ALTDOWN)):
                if orig[0] is not None and self._parent_hwnd:
                    try:
                        _user32.PostMessageW(self._parent_hwnd, msg, wp, lp)
                    except Exception:
                        pass
                return 0
            if msg in FORWARD_MSGS and orig[0] is not None and self._parent_hwnd:
                try:
                    _user32.PostMessageW(self._parent_hwnd, msg, wp, lp)
                except Exception:
                    pass
            return _user32.CallWindowProcW(orig[0], h, msg, wp, lp)

        self._wndproc_obj = WNDPROC_T(wndproc)
        old = _user32.SetWindowLongPtrW(
            hwnd, GWLP_WNDPROC,
            ctypes.cast(self._wndproc_obj, ctypes.c_void_p).value)
        orig[0] = old
        self._orig_wndproc = old
        dbg("input forwarder installed hwnd=%s parent=%s" % (hwnd, gw))

    def _uninstall_input_forwarder(self):
        """Restore the window's original WndProc. The shared pre-created
        window outlives a single session, so a session's forwarder subclass
        must be removed when the session ends - otherwise the next session
        would chain a second subclass onto the first, and the first
        instance's callback object could be garbage-collected while still
        installed (a dangling WndProc). No-op when no forwarder is installed."""
        if self._orig_wndproc is None:
            return
        hwnd = self.hwnd or self._find_own_window()
        if hwnd and _user32.IsWindow(hwnd):
            try:
                _user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC,
                                          self._orig_wndproc)
            except Exception:
                pass
        self._orig_wndproc = None
        self._wndproc_obj = None

    def _teardown_display(self):
        """Release the pygame display at the end of a playback session.

        "owned" (this session created the display): quit it - the window
        disappears with it. "shared" (the overlay pre-created the display at
        startup): hide the window and uninstall the input forwarder so the
        NEXT session can reuse the same window; the display itself stays
        alive. "none" (no display was ever created): no-op."""
        state = self._display_state
        self._display_state = "none"
        if state == "owned":
            try:
                pygame.display.quit()
            except Exception:
                pass
            dbg("owned display quit (session ended)")
            return
        if state != "shared":
            return
        self._uninstall_input_forwarder()
        hwnd = self.hwnd or self._find_own_window()
        if hwnd and _user32.IsWindow(hwnd):
            # Hide first so clearing the backing surface is never visible to
            # the user. The next session clears again while hidden before it
            # is shown.
            try:
                _user32.ShowWindow(hwnd, SW_HIDE)
            except Exception:
                try:
                    _user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                                         SWP_NOMOVE | SWP_NOSIZE |
                                         SWP_NOACTIVATE | SWP_HIDEWINDOW)
                except Exception:
                    pass
            self._present_black()
            # Keep the window attached to the game window.
            # Deliberately NO detach/re-attach between sessions: re-parenting
            # the reused window made it fail to show again in the real game
            # (second video black screen - only a fresh window showed). The
            # game-exit deadlock that detaching used to prevent is instead
            # handled by the display thread's idle message pump (the window's
            # WndProc lives there; the pump services WM_DESTROY within ~0.1s,
            # so the game's DestroyWindow never blocks). The next session
            # re-shows the window in _attach_to_game.
            try:
                _user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
                                     SWP_HIDEWINDOW)
            except Exception:
                pass
        dbg("hid window, then cleared its backing frame (session ended)")
        _dbg_window_state(self, hwnd, "after teardown hide")
        dbg("shared display window hidden (session ended)")

    def _recreate_window(self):
        """Recreate our SDL window after it was destroyed. In child mode this
        happens when the game window is destroyed and recreated during a
        fullscreen/windowed toggle (our child window dies with the old game
        window); in overlay mode it is a fallback if our top-level window is
        ever lost. The render state (ffmpeg pipe, frame reader, audio clock)
        is untouched, so playback continues seamlessly - only the window is
        re-made and re-attached. Must run on the player thread (it owns the
        display)."""
        global SHARED_DISPLAY
        w, h = self._current_size or (self.initial_w, self.initial_h)
        flags = pygame.NOFRAME
        if self.window_mode == "child":
            flags |= pygame.HIDDEN
        # Same visibility policy as the initial window (_run_inner): child
        # mode creates HIDDEN (SDL's set_mode must not activate the recreated
        # window and steal keyboard focus from the game mid-toggle; it is
        # shown by re-attach with SWP_NOACTIVATE), while overlay mode creates
        # VISIBLE (a window shown at creation is placed above the game, unlike
        # one shown later with SWP_SHOWWINDOW, which can come up behind the
        # active game).
        was_shared = (self._display_state == "shared")
        try:
            pygame.display.quit()
        except Exception:
            pass
        try:
            self.screen = pygame.display.set_mode((max(1, w), max(1, h)),
                                                  flags)
        except pygame.error as e:
            logging.error("%s: window recreate failed: %s",
                          self.window_title, e)
            return None
        if was_shared:
            # The old window died externally (the game window that parented
            # it was recreated). pygame never sees the external destruction,
            # so its cached window handle is stale and get_wm_info keeps
            # returning the dead handle after a plain set_mode (a re-create
            # loop). Quitting the display - done above - clears pygame's
            # window state; the recreated display/window is now owned by this
            # session, and pre-creation is over for the process.
            SHARED_DISPLAY = False
            self._display_state = "owned"
        hwnd = self._own_hwnd()
        self.hwnd = hwnd
        # The old forwarder subclass and GDI caches died with the old window;
        # reset so the re-attach path re-installs them.
        self._orig_wndproc = None
        self._wndproc_obj = None
        self._parent_hwnd = None
        self._scratch = None
        self._destroy_dib()
        self._dib_size = None
        self._attached = False
        self._last_rect_read = 0.0
        self._last_z_assert = 0.0
        # A recreated window must re-park the mouse at the corner - the game
        # may still be mid-video after a fullscreen/windowed toggle. Unless
        # the game is windowed, where the cursor belongs at the window center.
        if self._game_is_windowed(self._game_window()) is not True:
            self._park_cursor()
        dbg("recreated window hwnd=%s" % hwnd)
        return hwnd

    def _sync_window(self, show=True):
        """Re-read the game window's client geometry and, if it changed, move
        and/or resize our window immediately. Runs on every rendered frame and
        during every wait, so window moves and fullscreen/windowed toggles
        feel instant and the overlay never stalls.

        window_mode == "child":  the window is a CHILD of the game (SetParent),
        so its position is always (0, 0) in the parent's client coordinates and
        it moves with the parent automatically; only the size must be tracked.
        Every sync also re-asserts our z-order at the TOP of the game's
        children: when the game toggles fullscreen/windowed it re-creates or
        re-raises its own video child window (the dummy AVI player) above ours,
        which would show the game's black dummy video instead of our MP4.
        Re-raising ours independently of the throttled geometry read keeps
        our video on top throughout every toggle. If our window was destroyed
        by the toggle (game window recreated), it is re-made and re-attached.

        window_mode == "overlay":  the window is a TOP-LEVEL overlay (never a
        child of the game), so BOTH the position (where the game's client area
        sits on screen) and the size must be tracked. The window is moved/
        resized with SetWindowPos only; frames are composited with
        UpdateLayeredWindow at the live window size, so the SDL surface size
        is irrelevant.

        Z-order in overlay mode: the overlay stays above the game because
        it is CREATED VISIBLE (see _run_inner) - a window shown for the first
        time with SWP_SHOWWINDOW from a background process comes up BEHIND
        the active foreground window, which is exactly what happened when the
        user clicked/dragged the game before playback (the game was the active
        window at show time). Creating the window visible places it above the
        game from the start, and _reassert_z_order keeps it in the topmost
        band throughout later resize/reorder operations. Deliberately NO
        SetWindowPos on the game window (raising ours AND demoting the game to
        HWND_NOTOPMOST): that is a synchronous CROSS-PROCESS call that blocks
        until the game's thread processes WM_WINDOWPOSCHANGING - during
        DirectDraw video blits the game pumps messages so rarely that playback
        stuttered to ~1 FPS. Only our own window is ever re-asserted (same
        process, same thread, processed instantly)."""
        hwnd = self.hwnd
        if hwnd and not _user32.IsWindow(hwnd):
            hwnd = self._find_own_window()
            # pygame.get_wm_info() can keep returning the CACHED handle after
            # the window was destroyed (its child died with the old game
            # window), so validate what it hands back.
            if hwnd and not _user32.IsWindow(hwnd):
                hwnd = None
            if not hwnd:
                # The game window was destroyed and recreated on a toggle,
                # taking our child with it. Recreate and re-attach (playback
                # state is preserved, so the video does not restart).
                hwnd = self._recreate_window()
            self.hwnd = hwnd
        if not hwnd:
            return
        gw = self._game_window()
        if gw is None:
            return
        if not self._attached:
            if not self._attach_to_game(show=False):
                return
            hwnd = self.hwnd
            self._last_rect_read = 0.0  # position/size the window immediately after attach
            self._present_black()
            if show:
                self._show_window_now()
        # Re-assert our own z-order before the geometry-read throttle. This is
        # deliberately independent of geometry: the game can reorder its black
        # video child without changing size, and a fullscreen transition can
        # repeatedly do so after our last geometry update.
        self._reassert_z_order(hwnd, show=show)
        # NOTE: no per-frame SetWindowPos on the GAME window. Doing that
        # (raising ours + demoting the game to HWND_NOTOPMOST every frame)
        # made the video stutter to ~1 FPS: SetWindowPos on the game's window
        # is a synchronous cross-process call that blocks until the game's
        # thread processes WM_WINDOWPOSCHANGING - and during DirectDraw video
        # blits the game barely pumps its message queue. The overlay is kept on
        # top by the local, 50ms _reassert_z_order assertion instead.
        # Reading the game window's client rect is a cross-process user32 call,
        # so it is throttled (resize-follow still reacts within ~200ms - plenty
        # for fullscreen/windowed toggles, which take a second anyway).
        now0 = time.time()
        if now0 - self._last_rect_read < 0.2:
            return
        self._last_rect_read = now0
        rect = _client_rect_on_screen(gw)
        if rect is None:
            return
        x, y, cw, ch = rect
        if self.window_mode == "overlay":
            # Top-level overlay: track BOTH the on-screen position of the
            # game's client area and its size.
            if (x, y) != self._target or (cw, ch) != self._current_size:
                self._target = (x, y)
                self._current_size = (cw, ch)
                flags = SWP_FRAMECHANGED | SWP_NOACTIVATE
                if show:
                    flags |= SWP_SHOWWINDOW
                _user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, cw, ch, flags)
            now = time.time()
            if now - self._last_topmost > 2.0:
                self._last_topmost = now
                # Self-heal: while the game owns the display (DirectDraw
                # fullscreen toggles) the OS can defer our SetWindowPos,
                # leaving the window at a stale position/size. Re-assert
                # against the game client.
                cr = w.RECT()
                if _user32.GetWindowRect(hwnd, ctypes.byref(cr)):
                    if ((cr.left, cr.top) != (x, y)
                            or (cr.right - cr.left, cr.bottom - cr.top) != (cw, ch)):
                        flags = SWP_FRAMECHANGED | SWP_NOACTIVATE
                        if show:
                            flags |= SWP_SHOWWINDOW
                        _user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, cw, ch, flags)
        elif (cw, ch) != self._current_size:
            self._current_size = (cw, ch)
            # Child coords: (0, 0) relative to the parent's client origin.
            # HWND_TOP keeps us above the game's own video child window.
            flags = SWP_FRAMECHANGED | SWP_NOACTIVATE
            if show:
                flags |= SWP_SHOWWINDOW
            _user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, cw, ch, flags)

        # Alt+Enter / fullscreen<->windowed toggle: once the window is
        # resized, fix up the mouse. Switching to WINDOWED shows the cursor
        # again at the game window's center (the user needs it to interact
        # with the window); switching back to fullscreen/borderless re-parks
        # it at the corner so it never floats over the video. The mode is
        # detected from the game window's style (caption == windowed), with a
        # screen-size fallback.
        windowed = self._game_is_windowed(gw)
        if windowed is not None and windowed != self._game_windowed:
            self._game_windowed = windowed
            if windowed:
                self._restore_cursor(show=True)
            else:
                self._park_cursor()

    def _game_is_windowed(self, gw):
        """True if the game window is in windowed mode (has a caption/title
        bar, or is smaller than the screen), False for fullscreen/borderless.
        Returns None if the mode cannot be determined."""
        if not gw:
            return None
        style = _user32.GetWindowLongW(gw, GWL_STYLE)
        if style:
            if style & WS_CAPTION:
                return True
            if style & WS_POPUP:
                return False
        rect = _client_rect_on_screen(gw)
        if rect:
            sw = _user32.GetSystemMetrics(SM_CXSCREEN)
            sh = _user32.GetSystemMetrics(SM_CYSCREEN)
            # Fullscreen/borderless covers the whole screen.
            return not (rect[2] >= sw - 1 and rect[3] >= sh - 1)
        return None

    def _park_cursor(self):
        """Park the mouse at the bottom-right corner of the screen (the last
        pixel), out of the way while the video plays. The game hides the
        system cursor during its dummy video and draws its own; SDL's white
        cursor would otherwise float over the player window. The cursor is
        restored to the game window's center when playback ends (see
        _restore_cursor)."""
        try:
            sw = _user32.GetSystemMetrics(SM_CXSCREEN) - 1
            sh = _user32.GetSystemMetrics(SM_CYSCREEN) - 1
            _user32.SetCursorPos(max(sw, 0), max(sh, 0))
        except Exception:
            pass

    def _restore_cursor(self, show=False):
        """Move the mouse back to the center of the game window and set the
        system cursor's visibility. Called at playback end (hide: the game
        draws its own cursor there, and a visible system cursor would float
        over it) and when switching to windowed mode (show: the user needs
        the cursor to interact with the window). Runs on the player thread
        while the display is still alive."""
        try:
            gw = self._game_window()
            rect = _client_rect_on_screen(gw) if gw else None
            if rect:
                _user32.SetCursorPos(rect[0] + rect[2] // 2,
                                     rect[1] + rect[3] // 2)
            try:
                pygame.mouse.set_visible(show)
            except Exception:
                pass
            _user32.ShowCursor(show)
        except Exception:
            pass

    def _render(self, surf):
        """Scale the frame into a scratch surface of the window's current size
        (letterboxed or stretched) and draw it into the window DC."""
        w, h = self._current_size or (0, 0)
        if w <= 0 or h <= 0:
            return
        if self._scratch is None or self._scratch.get_size() != (w, h):
            self._scratch = pygame.Surface((w, h))
        scratch = self._scratch
        if self.stretch:
            if (w, h) != surf.get_size():
                scaled = pygame.transform.smoothscale(surf, (w, h))
            else:
                scaled = surf
            scratch.blit(scaled, (0, 0))
        else:
            scratch.fill((0, 0, 0))
            aspect = surf.get_width() / float(surf.get_height())
            tw, th = w, int(w / aspect)
            if th > h:
                th = h
                tw = int(h * aspect)
            if (tw, th) != surf.get_size():
                scaled = pygame.transform.smoothscale(surf, (max(1, tw), max(1, th)))
            else:
                scaled = surf
            scratch.blit(scaled, ((w - tw) // 2, (h - th) // 2))
        self._present(scratch)

    def _present(self, scratch, hdc=None):
        """Composite the rendered frame into the layered overlay window via
        UpdateLayeredWindow, scaled to the window's ACTUAL size and position.

        The window is WS_EX_LAYERED, so DWM composites it on top of the game's
        DirectDraw primary-surface blits (kills the black flicker). Unlike
        SetLayeredWindowAttributes - whose redirection surface stays stuck at
        the creation size, so the video could not track window resizes -
        UpdateLayeredWindow takes the bitmap and the window rect explicitly
        every frame, so the composite always shows the frame at exactly the
        window's live size and position (even while a game resize is still
        being processed). The window rect is read live rather than cached,
        which also makes the fullscreen<->windowed toggles seamless.
        """
        hwnd = self.hwnd
        if not hwnd or not _user32.IsWindow(hwnd):
            return
        wr = w.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(wr)):
            return
        cw, ch = wr.right - wr.left, wr.bottom - wr.top
        if cw <= 0 or ch <= 0:
            return
        sw, sh = scratch.get_size()
        if sw <= 0 or sh <= 0:
            return
        if os.environ.get("OVERLAY_NO_LAYERED"):
            # Bisect / non-layered fallback: draw straight into the window DC
            # with GDI StretchDIBits (no DWM compositing).
            dc = hdc or _user32.GetDC(hwnd)
            if not dc:
                return
            try:
                proxy = scratch.get_buffer()
                buf = ctypes.c_char.from_buffer(proxy)
                sbmi = BITMAPINFOHEADER()
                sbmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                sbmi.biWidth = sw
                sbmi.biHeight = -sh
                sbmi.biPlanes = 1
                sbmi.biBitCount = 32
                sbmi.biCompression = BI_RGB
                _gdi32.StretchDIBits(dc, 0, 0, cw, ch, 0, 0, sw, sh,
                                     ctypes.addressof(buf), ctypes.byref(sbmi), 0,
                                     SRCCOPY)
            finally:
                if not hdc:
                    _user32.ReleaseDC(hwnd, dc)
            return
        if self._dib_mem_dc is None or self._dib_size != (cw, ch):
            self._destroy_dib()
            self._create_dib(cw, ch)
        if self._dib_mem_dc is None:
            return
        # Stretch the frame (already letterboxed/stretched by _render) into the
        # DIB section at the window's live size.
        proxy = scratch.get_buffer()
        buf = ctypes.c_char.from_buffer(proxy)
        sbmi = BITMAPINFOHEADER()
        sbmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        sbmi.biWidth = sw
        sbmi.biHeight = -sh
        sbmi.biPlanes = 1
        sbmi.biBitCount = 32
        sbmi.biCompression = BI_RGB
        _gdi32.StretchDIBits(self._dib_mem_dc, 0, 0, cw, ch, 0, 0, sw, sh,
                             ctypes.addressof(buf), ctypes.byref(sbmi), 0,
                             SRCCOPY)
        blend = BLENDFUNCTION()
        blend.BlendOp = AC_SRC_OVER
        blend.SourceConstantAlpha = 255
        blend.AlphaFormat = 0
        # Child window: pptDst is relative to the parent's client origin (our
        # position is always (0, 0)). Top-level overlay: pptDst is an absolute
        # screen position, and passing (0, 0) would drag the window to the
        # screen origin. Either way, pass NULL to keep the composite where
        # SetWindowPos put it. psize MUST be passed explicitly: verified
        # empirically that UpdateLayeredWindow with NULL psize leaves the
        # composite stale (black window), while NULL pptDst + explicit psize
        # composites correctly without moving the window.
        pt_src = w.POINT(0, 0)
        size = w.SIZE(cw, ch)
        _user32.UpdateLayeredWindow(hwnd, None, None, ctypes.byref(size),
                                    self._dib_mem_dc,
                                    ctypes.byref(pt_src), 0,
                                    ctypes.byref(blend), ULW_ALPHA)

    def _present_black(self):
        """Composite an all-black frame into the window. Used so the reused
        window never re-shows stale content from a previous video: without
        this, the next session's attach shows the OLD video's last frame for
        a moment (then a black frame, then the new video) - the pre-roll
        flash. Presenting black at teardown (before hiding) and right after
        each session's attach makes every show start from black, which is
        indistinguishable from the game's own black dummy playback."""
        hwnd = self.hwnd
        if not hwnd or not _user32.IsWindow(hwnd):
            return
        wr = w.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(wr)):
            return
        cw, ch = wr.right - wr.left, wr.bottom - wr.top
        if cw <= 0 or ch <= 0:
            return
        if self._scratch is None or self._scratch.get_size() != (cw, ch):
            self._scratch = pygame.Surface((cw, ch))
        self._scratch.fill((0, 0, 0))
        self._present(self._scratch)

    def _create_dib(self, cw, ch):
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = cw
        bmi.biHeight = -ch
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        hbmp = _gdi32.CreateDIBSection(None, ctypes.byref(bmi), BI_RGB,
                                       ctypes.byref(bits), None, 0)
        if not hbmp:
            return
        mem = _gdi32.CreateCompatibleDC(None)
        if not mem:
            _gdi32.DeleteObject(hbmp)
            return
        _gdi32.SelectObject(mem, hbmp)
        self._dib_mem_dc = mem
        self._dib_bmp = hbmp
        self._dib_size = (cw, ch)

    def _destroy_dib(self):
        if self._dib_mem_dc:
            _gdi32.DeleteDC(self._dib_mem_dc)
            self._dib_mem_dc = None
        if self._dib_bmp:
            _gdi32.DeleteObject(self._dib_bmp)
            self._dib_bmp = None
        self._dib_size = None
