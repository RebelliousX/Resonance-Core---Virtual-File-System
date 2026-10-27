#!/usr/bin/env python3
"""Isolated AVI handle scanner (run as a subprocess by video_overlay.py).

Why a separate process: enumerating another process's handle table is native
code that can misbehave. NtQueryObject/GetFinalPathNameByHandleW can BLOCK for
tens of seconds on certain file handles, and GetFinalPathNameByHandleW can
even ACCESS-VIOLATE on some duplicated handles - an unhandled native crash in
any thread kills the whole process. If that happened inside the overlay, the
overlay (and its video player window) would die mid-playback, which is exactly
the reported bug: the first video played, then the overlay was gone and the
second video never appeared. Running the scan here isolates both failure
modes: a blocked scan only delays this process's output, and a native crash
kills only this process - the overlay notices we died and restarts us.

Protocol (line-based, one result per scan tick):
    argv: <game_pid> <file_object_type_index> [<video specs json>]
          specs json: list of [subdir, filename] pairs describing the dummy
          videos the overlay can play (e.g. [["VIDEO", "H_Intro.avi"]]).
          The game is watched for handles ending in <subdir>/<filename>
          (subdir may be "" for files at the scan root). When the specs are
          absent or empty, the legacy Commandos pattern VIDEO\\*.avi is used.
    stdout: "RESULT <json>" every SCAN_INTERVAL seconds
    stdin:  EOF means the overlay is gone -> exit (no orphaned scanner)
"""
import ctypes
import ctypes.wintypes as w
import json
import os
import re
import sys
import threading
import time

ntdll = ctypes.WinDLL("ntdll")
k32 = ctypes.WinDLL("kernel32")
user32 = ctypes.WinDLL("user32")

STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
STATUS_BUFFER_TOO_SMALL = 0xC0000023
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_DUP_HANDLE = 0x0040
DUPLICATE_SAME_ACCESS = 0x00000002
SystemExtendedHandleInformation = 64
PROCESS_HANDLE_INFORMATION = 51
ObjectNameInformation = 1

SCAN_INTERVAL = 1.0  # seconds between scans (handle churn is slow)


class SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX(ctypes.Structure):
    _fields_ = [("Object", ctypes.c_void_p),
                ("UniqueProcessId", ctypes.c_size_t),
                ("HandleValue", ctypes.c_void_p),
                ("GrantedAccess", ctypes.c_uint32),
                ("CreatorBackTraceIndex", ctypes.c_uint16),
                ("ObjectTypeIndex", ctypes.c_uint16),
                ("HandleAttributes", ctypes.c_uint32),
                ("Reserved", ctypes.c_uint32)]


class PROCESS_HANDLE_TABLE_ENTRY_INFO(ctypes.Structure):
    _fields_ = [("HandleValue", ctypes.c_void_p),
                ("HandleCount", ctypes.c_ulonglong),
                ("PointerCount", ctypes.c_ulonglong),
                ("GrantedAccess", ctypes.c_uint32),
                ("ObjectTypeIndex", ctypes.c_uint32),
                ("HandleAttributes", ctypes.c_uint32),
                ("Reserved", ctypes.c_uint32)]


ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong
ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p,
                                           ctypes.c_ulong, ctypes.c_void_p]
ntdll.NtQueryInformationProcess.restype = ctypes.c_ulong
ntdll.NtQueryInformationProcess.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.c_void_p]
ntdll.NtDuplicateObject.restype = ctypes.c_ulong
ntdll.NtDuplicateObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
k32.OpenProcess.restype = ctypes.c_void_p
k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.GetFinalPathNameByHandleW.restype = w.DWORD
k32.GetFinalPathNameByHandleW.argtypes = [ctypes.c_void_p, w.LPWSTR, w.DWORD, w.DWORD]


def process_handle_info(proc):
    """Return the process's own handle table via ProcessHandleInformation."""
    size = 65536
    while True:
        buf = ctypes.create_string_buffer(size)
        needed = w.DWORD()
        st = ntdll.NtQueryInformationProcess(proc, PROCESS_HANDLE_INFORMATION, buf, size,
                                             ctypes.byref(needed))
        if st == 0:
            break
        if st in (STATUS_INFO_LENGTH_MISMATCH, STATUS_BUFFER_TOO_SMALL):
            size = max(size * 2, int(needed.value) + 4096)
            continue
        raise OSError("NtQueryInformationProcess failed: 0x%08X" % st)
    n = ctypes.c_size_t.from_buffer(buf, 0).value
    arr = ctypes.cast(ctypes.addressof(buf) + 16,
                      ctypes.POINTER(PROCESS_HANDLE_TABLE_ENTRY_INFO))
    if n > (size - 16) // ctypes.sizeof(PROCESS_HANDLE_TABLE_ENTRY_INFO):
        n = (size - 16) // ctypes.sizeof(PROCESS_HANDLE_TABLE_ENTRY_INFO)
    return [arr[i] for i in range(n)]


def query_object_name(handle):
    """Full path of a FILE handle via GetFinalPathNameByHandleW.

    This call can block for tens of seconds and can access-violate on some
    duplicated handles - that is exactly why this whole scanner runs in a
    separate process. A crash here kills only this process.
    """
    size = 4096
    buf = ctypes.create_unicode_buffer(size)
    n = k32.GetFinalPathNameByHandleW(handle, buf, size, 0)
    if n == 0:
        return None
    if n > size:
        buf = ctypes.create_unicode_buffer(n)
        n = k32.GetFinalPathNameByHandleW(handle, buf, n, 0)
        if n == 0 or n > len(buf):
            return None
    return buf.value


# Handles whose name query was abandoned (blocked past the per-query
# timeout). Such queries are never the AVI (those finish instantly), so
# skipping them briefly is safe. The set is persisted to a sidecar file so a
# crash-restarted helper does not re-query the same pathological handle and
# crash again in a loop.
_slow_handles = {}            # handle value -> time it was last abandoned
_SLOW_SKIP_SECONDS = 30.0
_SLOW_FILE = None             # sidecar path (set in main)


def _load_slow_handles():
    global _slow_handles
    try:
        with open(_SLOW_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _slow_handles = {int(k): float(v) for k, v in data.items()}
    except Exception:
        _slow_handles = {}


def _save_slow_handles():
    try:
        with open(_SLOW_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _slow_handles.items()}, f)
    except Exception:
        pass


def query_name_bounded(game_handle, dup_handle, timeout=0.15):
    """query_object_name() that never blocks the caller: runs the (possibly
    pathological) native call in a daemon worker thread and gives up after
    `timeout` seconds, returning None for that handle. The worker OWNS
    `dup_handle` and closes it when it finishes (possibly long after we gave
    up), so the caller must NOT close it after a spawn. When the handle is
    skipped (known slow), the dup handle is closed here.

    The skip set is keyed by the GAME's handle value (stable across scans),
    NOT the duplicated handle value - dup values are reused by the OS for
    different files between scans, so keying on them would randomly skip the
    AVI (measured: the AVI was detected on scan 1 and vanished on scan 2).
    """
    now = time.time()
    last = _slow_handles.get(game_handle)
    if last is not None and now - last < _SLOW_SKIP_SECONDS:
        try:
            k32.CloseHandle(dup_handle)
        except Exception:
            pass
        return None
    if last is not None:
        del _slow_handles[game_handle]
    result = {}

    def worker():
        try:
            result["name"] = query_object_name(dup_handle)
        except Exception:
            result["name"] = None
        finally:
            try:
                k32.CloseHandle(dup_handle)
            except Exception:
                pass

    t = threading.Thread(target=worker, name="name-query", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        _slow_handles[game_handle] = now
        _save_slow_handles()
        return None
    return result.get("name")


def build_patterns(specs):
    """Compile a regex per [subdir, filename] spec: matches a handle path that
    ends with <subdir>/<filename> (subdir "" -> just the filename). Falls back
    to the legacy Commandos VIDEO\\*.avi pattern when specs is empty."""
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


def game_avi_handles(game_pid, file_idx, specs=None):
    """Return {video_filename: handle_count} for the videos open in the game.
    Each filename is matched against the compiled spec patterns (see
    build_patterns); the result keys are the filenames the overlay indexes."""
    out = {}
    pats = build_patterns(specs)
    proc = k32.OpenProcess(PROCESS_DUP_HANDLE | PROCESS_QUERY_INFORMATION, False, game_pid)
    if not proc:
        return out
    try:
        entries = process_handle_info(proc)
        for e in entries:
            if file_idx is not None and e.ObjectTypeIndex != file_idx:
                continue
            dup = ctypes.c_void_p()
            st = ntdll.NtDuplicateObject(proc, e.HandleValue, ctypes.c_void_p(-1),
                                         ctypes.byref(dup), 0, 0, DUPLICATE_SAME_ACCESS)
            if st != 0 or not dup.value:
                continue
            # NB: query_name_bounded OWNS dup.value now - the worker thread
            # closes it when the query completes (possibly long after we gave
            # up on it), so we must NOT CloseHandle it here (except in the
            # skip path, which closes it itself).
            name = query_name_bounded(e.HandleValue, dup.value)
            if name:
                for pat in pats:
                    m = pat.search(name)
                    if m:
                        fname = m.group(1) if m.groups() else None
                        if fname is None:
                            fname = os.path.basename(name)
                        out[fname] = out.get(fname, 0) + 1
                        break
    finally:
        k32.CloseHandle(proc)
    return out


def main():
    if len(sys.argv) < 3:
        return 1
    game_pid = int(sys.argv[1])
    file_idx = int(sys.argv[2]) if sys.argv[2] != "None" else None
    specs = None
    if len(sys.argv) >= 4 and sys.argv[3]:
        try:
            raw = json.loads(sys.argv[3])
            specs = [tuple(s) for s in raw] if isinstance(raw, list) else None
        except (ValueError, TypeError):
            specs = None
    _SLOW_FILE = os.path.join(os.environ.get("TEMP", r"C:\Users\Thaer\AppData\Local\Temp"),
                              "avi_scan_slow_%d.json" % game_pid)
    _load_slow_handles()

    # Exit when the overlay goes away (its stdin pipe closes on EOF).
    def watch_stdin():
        try:
            sys.stdin.read()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=watch_stdin, daemon=True).start()

    while True:
        t0 = time.time()
        try:
            res = game_avi_handles(game_pid, file_idx, specs)
        except Exception:
            res = {}
        try:
            sys.stdout.write("RESULT " + json.dumps(res) + "\n")
            sys.stdout.flush()
        except Exception:
            return 1  # overlay is gone
        elapsed = time.time() - t0
        if elapsed < SCAN_INTERVAL:
            time.sleep(SCAN_INTERVAL - elapsed)


if __name__ == "__main__":
    sys.exit(main())
