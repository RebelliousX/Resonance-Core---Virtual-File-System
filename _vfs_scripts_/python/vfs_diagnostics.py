"""VFS diagnostics tracer - SEPARATE tool, not used by the normal VFS.

Drop-in replacement for vfs_handler.py: same command line, same mount. It
wraps EVERY FUSE operation with millisecond-timestamped logging so we can see
exactly what the game does with the dummy video files - the open flags, the
read pattern (offset/length/result), when reads stop, and when the handle is
released. This is how we find out why the game closes the dummy AVI as soon
as it opens it (it plays fine when hard-linked on a real disk).

v2: additionally traces every RARE operation through a generic __call__
hook (access, opendir, xattrs, ioctl, create, ...), implements byte-range
locking (fusepy leaves fuse_operations.lock NULL -> winfsp-fuse answers
LockFile/UnlockFile with ENOSYS; NTFS hard links always succeed - prime
suspect for the silent playback abort after index parsing) and can emit
virtual-video open/close events exactly like vfs_handler.py (--video-events).

Usage (swap it into the launcher, or run by hand):
    python vfs_diagnostics.py --source "G:\\Commandos BEL\\_vfs_data_" --mount X: --cache "...\\vfs_index_cache.json"

Log: written to <cache_dir>\\vfs_diagnostics.log AND mirrored to the console.
"""

import os
import sys
import time
import errno
import ctypes
import struct
import stat
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vfs_handler  # noqa: E402  (reuse the real VFS; nothing instrumented in it)

LOG = None


def open_log(path):
    global LOG
    LOG = open(path, "w", encoding="utf-8", buffering=1)


def log_line(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S.") + "%03d" % (int(time.time() * 1000) % 1000), msg)
    if LOG is not None:
        try:
            LOG.write(line + "\n")
            LOG.flush()
        except OSError:
            pass
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


_FLAG_NAMES = [
    ("O_WRONLY", 0o1), ("O_RDWR", 0o2), ("O_CREAT", 0o100), ("O_EXCL", 0o200),
    ("O_TRUNC", 0o1000), ("O_APPEND", 0o2000), ("O_NONBLOCK", 0o4000),
    ("O_SYNC", 0o4010000), ("O_DIRECTORY", 0o200000), ("O_NOFOLLOW", 0o400000),
]


def decode_flags(flags):
    names = []
    if flags & 0o3 == 0:
        names.append("O_RDONLY")
    for name, bit in _FLAG_NAMES:
        if flags & bit:
            names.append(name)
    return "|".join(names) if names else hex(flags)


# --- byte-range locking (struct flock, MinGW/x64 layout) -------------------
class _Flock(ctypes.Structure):
    _fields_ = [("l_type", ctypes.c_int16),      # F_RDLCK/F_WRLCK/F_UNLCK
                ("l_whence", ctypes.c_int16),
                ("l_start", ctypes.c_int64),
                ("l_len", ctypes.c_int64),
                ("l_pid", ctypes.c_int32)]


_F_TYPES = {0: "F_RDLCK", 1: "F_WRLCK", 2: "F_UNLCK"}
_F_CMDS = {5: "F_GETLK", 6: "F_SETLK", 7: "F_SETLKW"}

# Ops that already have detailed named overrides below. The generic __call__
# hook stays quiet for these (video paths are logged by the overrides; audio
# paths would drown the log).
_HANDLED_VERBOSE = {"getattr", "readdir", "open", "read", "write", "truncate",
                    "flush", "fsync", "release", "statfs", "setattr"}


class TracerVFS(vfs_handler.UniversalAudioVFS):
    """Same VFS, every operation wrapped with logging. Only /VIDEO/ files are
    traced at full detail (open flags, every read); other files log open/close
    so the audio path stays comparable without drowning the log."""

    def _is_video(self, path):
        p = path.lower()
        return "/video/" in p or p.endswith((".avi", ".mp4", ".txt"))

    # ------------------------------------------------------------------
    # Generic catch-all: every op fusepy dispatches passes through here
    # first (self.operations(name, *args) -> __call__), including lock,
    # bmap, ioctl, access, opendir, xattrs... Rare ops get logged always;
    # high-volume ops stay with their named overrides below.
    # ------------------------------------------------------------------
    def __call__(self, op, *args):
        if op in _HANDLED_VERBOSE:
            return super().__call__(op, *args)
        t0 = time.time()
        try:
            r = super().__call__(op, *args)
        except OSError as e:
            code = errno.errorcode.get(e.errno, str(e.errno))
            log_line("%-9s %-42s -> %s (%.2fms)"
                     % (op.upper(), str(args[0])[:40] if args else "?", code,
                        (time.time() - t0) * 1000))
            raise
        extra = ""
        if r:
            extra = " -> %r" % (r,)
        log_line("%-9s %-42s (%.2fms)%s"
                 % (op.upper(), str(args[0])[:40] if args else "?",
                    (time.time() - t0) * 1000, extra))
        return r

    # Byte-range locking: NTFS answers LockFile/UnlockFile natively; a NULL
    # fuse handler makes winfsp-fuse return ENOSYS instead. We answer "OK"
    # (and for F_GETLK report no conflicting lock), logging every request -
    # dummies are read-only mirrors so pretending all locks succeed is safe.
    def lock(self, path, fh, cmd, lk):
        info = ""
        try:
            fl = ctypes.cast(lk, ctypes.POINTER(_Flock)).contents
            info = "%s type=%s whence=%d start=%d len=%d pid=%d" % (
                _F_CMDS.get(cmd, cmd), _F_TYPES.get(fl.l_type, fl.l_type),
                fl.l_whence, fl.l_start, fl.l_len, fl.l_pid)
            if cmd == 5:                       # F_GETLK: no conflict exists
                fl.l_type = 2                  # F_UNLCK
        except Exception as e:
            info = "(flock parse failed: %s)" % e
        log_line("LOCK     %-40s fh=%r %s" % (path, fh, info))
        return 0

    # Block-to-device mapping (FIBMAP ioctl): never meaningful over a
    # virtual FS; wired only so an attempt becomes VISIBLE in the trace.
    def bmap(self, path, blocksize, idx):
        log_line("BMAP     %-40s blocksize=%d" % (path, blocksize))
        raise OSError(errno.ENOSYS)

    def getattr(self, path, fh=None):
        t0 = time.time()
        try:
            r = super().getattr(path, fh)
            if self._is_video(path):
                log_line("getattr  %-40s mode=%s size=%s (%.2fms)"
                         % (path, oct(r["st_mode"]), r["st_size"], (time.time() - t0) * 1000))
            return r
        except OSError as e:
            if self._is_video(path):
                log_line("getattr  %-40s -> ENOENT" % path)
            raise

    def readdir(self, path, fh):
        t0 = time.time()
        r = super().readdir(path, fh)
        if self._is_video(path):
            log_line("readdir  %-40s %d entries (%.2fms)"
                     % (path, len(r), (time.time() - t0) * 1000))
        return r

    def open(self, path, flags):
        t0 = time.time()
        r = super().open(path, flags)
        if self._is_video(path):
            log_line("open     %-40s flags=%s -> %r (%.2fms)"
                     % (path, decode_flags(flags), r, (time.time() - t0) * 1000))
        return r

    def read(self, path, length, offset, fh):
        t0 = time.time()
        data = super().read(path, length, offset, fh)
        if self._is_video(path):
            got = len(data) if isinstance(data, (bytes, bytearray)) else data
            log_line("read     %-40s off=%d len=%d -> got=%s (%.2fms)"
                     % (path, offset, length, got, (time.time() - t0) * 1000))
        return data

    def write(self, path, data, offset, fh):
        t0 = time.time()
        r = super().write(path, data, offset, fh)
        if self._is_video(path):
            log_line("write    %-40s off=%d len=%d -> %r (%.2fms)"
                     % (path, offset, len(data), r, (time.time() - t0) * 1000))
        return r

    def truncate(self, path, length, fh=None):
        t0 = time.time()
        r = super().truncate(path, length, fh)
        if self._is_video(path):
            log_line("truncate %-40s len=%d -> %r (%.2fms)"
                     % (path, length, r, (time.time() - t0) * 1000))
        return r

    def flush(self, path, fh):
        t0 = time.time()
        try:
            r = super().flush(path, fh)
        except Exception as e:  # fusepy: undefined flush -> ENOSYS
            log_line("flush    %-40s -> ENOSYS (%s)" % (path, e))
            return -errno.ENOSYS
        if self._is_video(path):
            log_line("flush    %-40s -> %r (%.2fms)" % (path, r, (time.time() - t0) * 1000))
        return r

    def fsync(self, path, datasync, fh):
        t0 = time.time()
        try:
            r = super().fsync(path, datasync, fh)
        except Exception as e:
            log_line("fsync    %-40s -> ENOSYS (%s)" % (path, e))
            return -errno.ENOSYS
        if self._is_video(path):
            log_line("fsync    %-40s -> %r (%.2fms)" % (path, r, (time.time() - t0) * 1000))
        return r

    def release(self, path, fh):
        t0 = time.time()
        r = super().release(path, fh)
        if self._is_video(path):
            log_line("release  %-40s (%.2fms)" % (path, (time.time() - t0) * 1000))
        return r

    def statfs(self, path):
        t0 = time.time()
        try:
            r = super().statfs(path)
            # Volume-level capacity checks (GetDiskFreeSpaceEx) can arrive
            # for "/" or a directory, so log every statfs regardless of type.
            bsize = r.get("f_bsize", 0) if isinstance(r, dict) else 0
            blocks = r.get("f_blocks", 0) if isinstance(r, dict) else 0
            free = r.get("f_bavail", 0) if isinstance(r, dict) else 0
            log_line("statfs   %-40s bsize=%s blocks=%s free=%s (%.2fms)"
                     % (path, bsize, blocks, free, (time.time() - t0) * 1000))
            return r
        except Exception as e:
            code = getattr(e, "errno", None)
            log_line("statfs   %-40s -> %s (%s)" % (path,
                     errno.errorcode.get(code, code), e))
            return -errno.ENOSYS

    def setattr(self, path, stat_info, fh=None):
        t0 = time.time()
        try:
            r = super().setattr(path, stat_info, fh)
        except Exception as e:
            log_line("setattr  %-40s -> ENOSYS (%s)" % (path, e))
            return -errno.ENOSYS
        if self._is_video(path):
            log_line("setattr  %-40s -> %r (%.2fms)" % (path, r, (time.time() - t0) * 1000))
        return r


def main():
    parser = argparse.ArgumentParser(description="VFS diagnostics tracer (drop-in for vfs_handler.py)")
    parser.add_argument("--source", required=True)
    parser.add_argument("--mount", default="X:")
    parser.add_argument("--label", default="AudioVFS")
    parser.add_argument("--cache", default=None)
    parser.add_argument("--status", default=None)
    parser.add_argument("--video-events", default=None, dest="video_events",
                        help="Optional path to the virtual-video event log "
                             "(video_events.jsonl). When set, behaves exactly "
                             "like vfs_handler.py in virtual-video mode: matched "
                             ".mp4/.txt sets are hidden and open/close events "
                             "are emitted for the overlay.")
    parser.add_argument("--backing", action="append", default=None, dest="backing",
                        help="Backing-store merge, same as vfs_handler.py: "
                             "repeatable \"REL=ABS\" pairs whose real folder "
                             "contents are served inside the virtual one")
    args = parser.parse_args()

    if args.cache:
        log_path = os.path.join(os.path.dirname(os.path.abspath(args.cache)),
                                "vfs_diagnostics.log")
    else:
        log_path = os.path.join(os.path.dirname(os.path.abspath(args.source)),
                                "vfs_diagnostics.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    open_log(log_path)
    log_line("=== VFS diagnostics start: source=%s mount=%s ===" % (args.source, args.mount))
    log_line("=== Virtual-video events: %s ==="
             % (args.video_events if args.video_events else "disabled"))
    log_line("=== Play the game, trigger a cutscene, then exit the game. ===")
    log_line("=== Send this file: %s ===" % log_path)

    # Same mount as the real VFS (uid=-1/gid=-1 so files are owned by the user).
    try:
        vfs = TracerVFS(args.source, cache_path=args.cache, status_path=args.status,
                        video_events_path=args.video_events, backing_map=args.backing)
        vfs._status_update("mounting", "mount", message="Mounting virtual drive", force=True)
        vfs_handler.FUSE(
            vfs, args.mount,
            foreground=True, nothreads=False,
            volname=args.label, FileinfoTimeout=-1, allow_other=True,
            uid=-1, gid=-1)
        vfs._status_update("stopped", "mount", message="Virtual drive stopped", force=True)
    except Exception as err:
        vfs_handler._write_status(args.status, "failed", "startup", 0,
                                message="%s: %s" % (type(err).__name__, err))
        raise


if __name__ == "__main__":
    main()
