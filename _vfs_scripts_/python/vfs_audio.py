import os
import sys
import errno
import stat
import time
import struct
import argparse
import json
import shutil
import subprocess
import threading
import queue
import concurrent.futures
from fuse import FUSE, Operations
import miniaudio

try:
    # The launcher switches cmd.exe to UTF-8. Explicitly matching that encoding
    # keeps the colored Unicode progress bar reliable in child Python processes.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, ValueError):
    sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

GREEN = "\033[92m"
BLUE = "\033[94m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
WHITE = "\033[97m"
BLINKING = "\033[1;5m"
YELLOW_BLUE = "\033[93;44m"
BLUE_YELLOW = "\033[94;103m"
WHITE_GREEN = "\033[97;42m"    # white on green   - [VFS PASSTHROUGH]
RESET = "\033[0m"

def _enable_ansi():
    if os.name != "nt":
        return True
    try:
        if not sys.stdout.isatty():
            return False
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False

if not _enable_ansi():        GREEN = BLUE = CYAN = YELLOW = RED = BOLD = YELLOW_BLUE = BLUE_YELLOW = WHITE_GREEN = BLINKING = WHITE = RESET = ""


_LOG_QUEUE = queue.Queue()
_LOG_TOGGLE = 0
_LAST_REGULAR_VFS_TOGGLE = None


def _log_worker():
    progress_active = False
    while True:
        item = _LOG_QUEUE.get()
        try:
            if isinstance(item, tuple):
                line, is_progress, is_done = item
                sys.stdout.write("\r" + line)
                if is_done:
                    sys.stdout.write("\n")
                    progress_active = False
                else:
                    progress_active = True
            else:
                if progress_active:
                    sys.stdout.write("\n")
                    progress_active = False
                sys.stdout.write(item)
            sys.stdout.flush()
        except Exception:
            # Never let console rendering kill the VFS logger thread.
            progress_active = False


threading.Thread(target=_log_worker, name="VFSLogger", daemon=True).start()


def log(msg, progress=False, done=False):
    # Alternate the [VFS] family tag color per emitted line
    # (yellow/blue then blue/yellow) so consecutive status lines
    # visibly alternate; other tags keep their own colors.
    global _LOG_TOGGLE, _LAST_REGULAR_VFS_TOGGLE
    for tag in ("[VFS]", "[VFS DECODE]", "[VFS MEMORY]"):
        for col in (YELLOW_BLUE, BLUE_YELLOW):
            pre = col + tag + RESET
            if msg.startswith(pre):
                if progress and done and _LAST_REGULAR_VFS_TOGGLE is not None:
                    # The completed bar must be the next alternating color
                    # after the last normal VFS row, independent of how many
                    # redraws occurred during probing.
                    _LOG_TOGGLE = 0 if _LAST_REGULAR_VFS_TOGGLE else 1
                else:
                    _LOG_TOGGLE ^= 1
                msg = (YELLOW_BLUE if _LOG_TOGGLE else BLUE_YELLOW) + tag + RESET + msg[len(pre):]
                if not progress:
                    _LAST_REGULAR_VFS_TOGGLE = _LOG_TOGGLE
                break
        else:
            continue
        break
    if progress:
        _LOG_QUEUE.put((msg, True, done))
    else:
        _LOG_QUEUE.put(msg + "\n")


def _write_status(path, state, stage, sequence, completed=0, total=0, message=""):
    """Atomically publish launcher-readable VFS startup state."""
    if not path:
        return
    try:
        status_dir = os.path.dirname(os.path.abspath(path))
        os.makedirs(status_dir, exist_ok=True)
        safe_message = " ".join(str(message).replace("=", ":").splitlines())
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("state=%s\n" % state)
            f.write("stage=%s\n" % stage)
            f.write("sequence=%d\n" % sequence)
            f.write("completed=%d\n" % int(completed or 0))
            f.write("total=%d\n" % int(total or 0))
            f.write("updated=%d\n" % int(time.time()))
            f.write("message=%s\n" % safe_message)
        os.replace(tmp_path, path)
    except OSError:
        # Status reporting must never prevent the VFS itself from starting.
        pass


def _display_path(path, base_dir=None):
    """Return a readable path relative to the launcher's current directory."""
    base_dir = os.path.abspath(base_dir or os.getcwd())
    absolute = os.path.abspath(path)
    relative = os.path.relpath(absolute, base_dir)
    if relative == ".":
        return "."
    parent_prefix = ".." + os.sep
    if relative.startswith(parent_prefix) or os.path.isabs(relative):
        return relative
    return "." + os.sep + relative


def original_ext_of(mp4):
    """Return the ORIGINAL video file extension (lowercase, without the dot)
    recorded in the sidecar <basename>.txt written by the re-encode script.
    This is the extension of the dummy file the game actually opens (e.g.
    .avi) - whatever the source video was. Defaults to "avi" when the sidecar
    is missing or has no such line. Mirrors video_player.original_ext_of()."""
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


class UniversalAudioVFS(Operations):
    def __init__(self, root, cache_path=None, status_path=None):
        self.root = os.path.abspath(root)
        self._display_base = os.path.abspath(os.getcwd())
        self._status_path = status_path
        self._status_sequence = 0
        self._status_last_write = 0.0
        self._progress_log_keys = {}
        self._status_update("indexing", "starting", message="Preparing VFS index", force=True)
        log(f"ℹ️ Current directory: {CYAN}{self._display_base}{RESET}")
        log(f"ℹ️ {BLINKING}{WHITE}NOTE:{RESET} paths shown as \"{CYAN}.\\{RESET}\" refer to this folder")
        self.wav_size_cache = {}    # {path_lower: file_size_bytes}
        self.pcm_buffer_cache = {}  # Global Cache: {path_lower: complete_wav_bytes}
        self.audio_meta_cache = {}  # File Properties Cache: {path_lower: (sample_rate, channels, format)}
        self.fixed_time = int(time.time())
        self.lock = threading.Lock() # Thread-safety for multi-threaded game audio engines

        self.virtual_to_real = {}
        self.virtual_dirs = set(['/'])
        self.case_map = {}          # CASE PRESERVATION MATRIX: {parent_path_lower: set(Original_Case_Nodes)}
        self.case_map['/'] = set()

        # Optional persistent metadata cache: keyed by real relative path, holding the
        # results of the expensive miniaudio.get_file_info() probe plus a
        # (size, mtime_ns) fingerprint. Entries are reused ONLY when the fingerprint
        # matches, so a stale cache can never hide or invent files - the directory
        # walk in _build_index() is always the source of truth for what exists.
        self._cache_path = cache_path
        self._index_cache = None
        self._probe_workers = min(8, max(1, os.cpu_count() or 1))
        if cache_path:
            self._index_cache = self._load_cache(cache_path)

        # Video overlay support: index every re-encoded mp4 (dummy file the
        # game opens -> real mp4, via the sidecar .txt) and pre-probe its
        # metadata with ffprobe DURING indexing, so playback never runs
        # ffprobe mid-game. The overlay detects dummy open/close itself by
        # polling the real files (NtQueryInformationFile), so no event
        # channel is needed here.
        self._ffprobe_exe = shutil.which("ffprobe")
        self._video_map = {}          # dummy rel path (lower) -> {"mp4", "name", "rel"}
        self._video_meta = {}         # dummy rel path -> probe entry (persisted)
        self._video_meta_path = None  # video_meta.json next to the index cache

        log(f"{YELLOW_BLUE}[VFS]{RESET} Indexing layout for source folder: {CYAN}{_display_path(self.root, self._display_base)}{RESET}")
        self._build_index()
        self._build_video_index()
        self._status_update("mounting", "complete", message="Indexing complete; mounting virtual drive", force=True)

    def _status_update(self, state, stage, completed=0, total=0, message="", force=False):
        if not self._status_path:
            return
        now = time.monotonic()
        # Avoid turning very large directory walks into a stream of tiny disk writes,
        # while still guaranteeing regular activity updates for the launcher.
        if not force and now - self._status_last_write < 0.5:
            return
        self._status_sequence += 1
        self._status_last_write = now
        _write_status(self._status_path, state, stage, self._status_sequence,
                      completed, total, message)
        if total and stage in ("audio", "video"):
            progress_key = (completed, total)
            if self._progress_log_keys.get(stage) != progress_key:
                self._progress_log_keys[stage] = progress_key
                percent = min(100, max(0, int(completed * 100 / total)))
                filled = int(percent * 30 / 100)
                bar = (GREEN + "█" * filled + BLUE + "░" * (30 - filled) + RESET)
                log(f"{YELLOW_BLUE}[VFS]{RESET} {stage} {bar} {percent:3d}% ({completed}/{total})",
                    progress=True, done=(percent >= 100))

    def _generate_wav_header(self, pcm_data_len, sample_rate, num_channels, bits_per_sample=16):
        """Generates a strict, standard canonical 44-byte PCM WAV header matching audio targets"""
        byte_rate = (sample_rate * num_channels * bits_per_sample) // 8
        block_align = (num_channels * bits_per_sample) // 8
        header = struct.pack('<4sI4s4sIHHIIHH4sI',
            b'RIFF', pcm_data_len + 36, b'WAVE', b'fmt ',
            16, 1, num_channels, sample_rate, byte_rate, block_align, bits_per_sample,
            b'data', pcm_data_len
        )
        return header

    def _build_index(self):
        # The directory walk is ALWAYS the source of truth for which files exist:
        # it is cheap (milliseconds for this tree) and detects new and deleted
        # files on every run. The index cache only stores the expensive per-file
        # metadata probe results, reused only when a file's size+mtime fingerprint
        # is unchanged, so mismatches are reconciled safely on every run.
        pending_probes = []  # (v_path_lower, real_file, rel_key, filename, size, mtime_ns)
        seen_rel_keys = set()
        cache_hits = 0
        total_files = 0
        audio_files = 0

        self._status_update("indexing", "layout", message="Scanning source tree", force=True)
        for dirpath, _, filenames in os.walk(self.root):
            for filename in filenames:
                total_files += 1
                filename_lower = filename.lower()
                real_file = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(real_file, self.root).replace("\\", "/")
                # Cache keys keep the EXACT on-disk case (Windows is case-insensitive,
                # so the walk always yields the real spelling; a case-renamed file
                # simply misses and gets re-probed once).
                rel_key = rel_path
                seen_rel_keys.add(rel_key)
                raw_parts = rel_path.split('/')
                parts = []

                # Check if it's an audio file that needs virtual translation
                is_audio = filename_lower.endswith('.mp3') or filename_lower.endswith('.ogg')
                if is_audio:
                    audio_files += 1

                for idx, item in enumerate(raw_parts):
                    if idx == len(raw_parts) - 1 and is_audio:
                        # Convert virtual extension to .wav for the game engine
                        parts.append(item[:-4] + ".wav")
                    else:
                        parts.append(item)

                v_path = "/" + "/".join(parts)
                v_path_lower = v_path.lower()

                self.virtual_to_real[v_path_lower] = real_file

                # Track exact structural parent tree case structures
                current_parent = '/'
                for idx, item in enumerate(parts):
                    if current_parent not in self.case_map:
                        self.case_map[current_parent] = set()
                    self.case_map[current_parent].add(item)

                    if idx < len(parts) - 1:
                        next_parent = current_parent if current_parent == '/' else current_parent + '/'
                        current_parent = (next_parent + item).lower()

                st = os.lstat(real_file)

                if is_audio:
                    cached = None
                    if self._index_cache is not None:
                        cached = self._index_cache.get(rel_key)
                    if (cached is not None
                            and cached.get("size") == st.st_size
                            and cached.get("mtime_ns") == st.st_mtime_ns):
                        try:
                            self.audio_meta_cache[v_path_lower] = {
                                "sample_rate": cached["sample_rate"],
                                "channels": cached["channels"],
                                "format": miniaudio.SampleFormat[cached["format"]],
                                "bits_per_sample": cached["bits_per_sample"],
                                "is_audio": True
                            }
                            pcm_bytes = int(round(cached["duration"] * cached["sample_rate"])) * cached["channels"] * (cached["bits_per_sample"] // 8)
                            self.wav_size_cache[v_path_lower] = 44 + pcm_bytes
                            cache_hits += 1
                        except Exception:
                            # Malformed or incompatible cache entry: re-probe it
                            pending_probes.append((v_path_lower, real_file, rel_key, filename, st.st_size, st.st_mtime_ns))
                    else:
                        pending_probes.append((v_path_lower, real_file, rel_key, filename, st.st_size, st.st_mtime_ns))
                else:
                    # Non-audio file tracking (like .MSB files)
                    self.audio_meta_cache[v_path_lower] = {"is_audio": False}
                    self.wav_size_cache[v_path_lower] = st.st_size

                for idx in range(1, len(parts)):
                    sub_dir = "/" + "/".join(parts[:idx])
                    self.virtual_dirs.add(sub_dir.lower())
                self._status_update("indexing", "layout", completed=total_files,
                                    message="Scanning source tree")

        # Phase 2: probe only the files that are new or changed (cache misses).
        #          Probing is read-only per file, so cache misses run in parallel.
        if pending_probes:
            log(f"{YELLOW_BLUE}[VFS]{RESET} Probing metadata for {len(pending_probes)} audio file(s) (new or changed)...")
            self._status_update("indexing", "audio", completed=0,
                                total=len(pending_probes), message="Probing audio metadata", force=True)
            results = []
            if self._probe_workers > 1 and len(pending_probes) > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=self._probe_workers) as pool:
                    futures = {pool.submit(self._probe_file, item): item for item in pending_probes}
                    for completed, future in enumerate(concurrent.futures.as_completed(futures), 1):
                        results.append((futures[future], future.result()))
                        self._status_update("indexing", "audio", completed=completed,
                                            total=len(pending_probes), message="Probing audio metadata")
            else:
                for completed, item in enumerate(pending_probes, 1):
                    results.append((item, self._probe_file(item)))
                    self._status_update("indexing", "audio", completed=completed,
                                        total=len(pending_probes), message="Probing audio metadata")

            for item, result in results:
                v_path_lower, real_file, rel_key, filename, size, mtime_ns = item
                if result[0] == "fallback":
                    _, fname, err = result
                    log(f"{YELLOW}[VFS Warning]{RESET} Failed reading metadata for {fname}: {err}. Falling back to standard profiles.")
                    st = os.lstat(real_file)
                    self.audio_meta_cache[v_path_lower] = {
                        "sample_rate": 22050,
                        "channels": 2,
                        "format": miniaudio.SampleFormat.SIGNED16,
                        "bits_per_sample": 16,
                        "is_audio": True
                    }
                    self.wav_size_cache[v_path_lower] = st.st_size * 11
                    # Failed probes are NOT cached so they are retried on the next run
                    continue
                meta, wav_size, duration = result
                self.audio_meta_cache[v_path_lower] = meta
                self.wav_size_cache[v_path_lower] = wav_size
                if self._index_cache is not None:
                    self._index_cache[rel_key] = {
                        "size": size,
                        "mtime_ns": mtime_ns,
                        "sample_rate": meta["sample_rate"],
                        "channels": meta["channels"],
                        "format": meta["format"].name,
                        "bits_per_sample": meta["bits_per_sample"],
                        "duration": duration,
                    }

        if cache_hits:
            log(f"{BLUE_YELLOW}[VFS]{RESET} Reused cached metadata for {YELLOW}{cache_hits}{RESET} unchanged file(s) - skipped probing.")
        self._status_update("indexing", "audio", completed=len(pending_probes),
                            total=len(pending_probes), message="Audio index complete", force=True)

        # Phase 3: drop cache entries for files that no longer exist, then save.
        #          The cache file is only rewritten when something actually changed
        #          (new/changed probes or removed files); otherwise it is left
        #          untouched and the reuse is logged above.
        if self._index_cache is not None:
            dropped = 0
            for k in [k for k in self._index_cache if k not in seen_rel_keys]:
                del self._index_cache[k]
                dropped += 1
            if pending_probes or dropped:
                self._save_cache()

        log(f"{BLUE_YELLOW}[VFS]{RESET} Index complete. Mapped {YELLOW}{total_files}{RESET} files total ({YELLOW}{audio_files}{RESET} audio + {YELLOW}{total_files - audio_files}{RESET} non-audio).")

    def _probe_file(self, item):
        """Probe one audio file and return the metadata used by the virtual WAV.

        This is intentionally separate from decoding: indexing should inspect the
        file header/container only, while open() decodes audio on demand. The
        current audio sources (MP3/OGG) are decoded as signed 16-bit PCM, which is
        also the format supported by the WAV header generator and miniaudio's
        decode_file() path below.
        """
        _v_path_lower, real_file, _rel_key, filename, _size, _mtime_ns = item
        try:
            info = miniaudio.get_file_info(real_file)
            sample_rate = int(info.sample_rate)
            channels = int(info.nchannels)
            num_frames = int(info.num_frames)
            if sample_rate <= 0 or channels <= 0 or num_frames < 0:
                raise ValueError("invalid audio metadata")

            # MP3 and Vorbis are reported as SIGNED16 by miniaudio. Normalize
            # unsupported source formats to SIGNED16 so the decode and WAV
            # header paths always agree if another audio type is added later.
            sample_format = miniaudio.SampleFormat.SIGNED16
            bits_per_sample = 16
            duration = float(info.duration)
            pcm_bytes = num_frames * channels * (bits_per_sample // 8)
            meta = {
                "sample_rate": sample_rate,
                "channels": channels,
                "format": sample_format,
                "bits_per_sample": bits_per_sample,
                "is_audio": True,
            }
            return meta, 44 + pcm_bytes, duration
        except Exception as err:
            return "fallback", filename, err

    def _build_video_index(self):
        """Index every re-encoded mp4 under the source root: map the dummy
        file the game opens (basename + ORIGINAL extension from the sidecar
        .txt, e.g. H_Intro.avi) to the real mp4, and pre-probe (width, height,
        fps, duration) with ffprobe so the overlay never runs ffprobe at
        gameplay. Results persist to video_meta.json next to the index cache,
        fingerprinted so only new/changed mp4s are re-probed."""
        self._video_map = {}
        if self._cache_path:
            self._video_meta_path = os.path.join(
                os.path.dirname(os.path.abspath(self._cache_path)), "video_meta.json")
        mp4s = []
        self._status_update("indexing", "video-layout", message="Scanning video files", force=True)
        for root, _dirs, files in os.walk(self.root):
            for fn in files:
                if not fn.lower().endswith(".mp4"):
                    continue
                mp4 = os.path.join(root, fn)
                stem = fn[:-4]
                dummy = stem + "." + original_ext_of(mp4)
                subdir = os.path.relpath(os.path.dirname(mp4), self.root).replace("\\", "/")
                dummy_rel = (subdir + "/" + dummy) if subdir != "." else dummy
                self._video_map[dummy_rel.lower()] = {
                    "mp4": mp4, "name": dummy, "rel": dummy_rel}
                mp4s.append(mp4)
                self._status_update("indexing", "video-layout", completed=len(mp4s),
                                    message="Scanning video files")
        if not mp4s:
            self._video_meta = {}
            # Persist the empty map so deleted videos do not remain in the
            # video metadata cache forever.
            if self._video_meta_path:
                self._save_video_meta()
            self._status_update("indexing", "video", message="No video files require probing", force=True)
            return
        old = self._load_video_meta()
        entries = {}
        pending = []
        for dummy_rel, info in self._video_map.items():
            try:
                st = os.lstat(info["mp4"])
            except OSError:
                continue
            cached = old.get(dummy_rel)
            if (cached is not None
                    and cached.get("size") == st.st_size
                    and cached.get("mtime_ns") == st.st_mtime_ns):
                entries[dummy_rel] = cached
            else:
                pending.append((dummy_rel, info["mp4"], st.st_size, st.st_mtime_ns))
        if pending:
            if self._ffprobe_exe:
                log(f"{YELLOW_BLUE}[VFS]{RESET} Probing metadata for {len(pending)} video file(s) with ffprobe...")
                self._status_update("indexing", "video", completed=0, total=len(pending),
                                    message="Preparing video metadata probes", force=True)
                workers = min(4, max(1, os.cpu_count() or 1))
                results = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(self._ffprobe_video, item[1]): item for item in pending}
                    for completed, future in enumerate(concurrent.futures.as_completed(futures), 1):
                        results.append((futures[future], future.result()))
                        self._status_update("indexing", "video", completed=completed,
                                            total=len(pending), message="Probing video metadata")
                for (dummy_rel, _mp4, size, mtime_ns), res in results:
                    if res:
                        entries[dummy_rel] = {"size": size, "mtime_ns": mtime_ns,
                                              "width": res[0], "height": res[1],
                                              "fps": res[2], "duration": res[3]}
            else:
                log(f"{YELLOW}[VFS Warning]{RESET} ffprobe not found - video metadata will fall back to sidecar .txt files at play time.")
                self._status_update("indexing", "video", completed=len(pending),
                                    total=len(pending), message="ffprobe unavailable; using sidecars", force=True)
        else:
            self._status_update("indexing", "video", completed=0, total=0,
                                message="All video metadata reused from cache", force=True)
        self._video_meta = entries
        changed = bool(pending) or entries != old
        if self._video_meta_path and changed:
            self._save_video_meta()
        self._status_update("indexing", "video", completed=len(pending), total=len(pending),
                            message="Video index complete", force=True)
        if changed:
            log(f"{BLUE_YELLOW}[VFS]{RESET} Saved video metadata cache ({len(entries)} file(s)) to {CYAN}{_display_path(self._video_meta_path, self._display_base)}{RESET}")
        else:
            log(f"{BLUE_YELLOW}[VFS]{RESET} Reused cached metadata for {YELLOW}{len(entries)}{RESET} unchanged file(s) - skipped probing.")

    def _ffprobe_video(self, mp4):
        """Return (width, height, fps, duration) or None. Same probe command
        and parsing as video_player._ffprobe_video so both sides agree."""
        try:
            out = subprocess.run(
                [self._ffprobe_exe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,r_frame_rate",
                 "-show_entries", "format=duration",
                 "-of", "csv=p=0", mp4],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ).stdout.decode("utf-8", "replace")
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

    def _load_video_meta(self):
        if not self._video_meta_path:
            return {}
        try:
            with open(self._video_meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_video_meta(self):
        try:
            tmp = self._video_meta_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._video_meta, f, indent=1)
            os.replace(tmp, self._video_meta_path)
        except OSError as e:
            log(f"{YELLOW}[VFS Warning]{RESET} Could not save video metadata: {e}")

    def _load_cache(self, cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries")
            if data.get("version") != 1 or not isinstance(entries, dict):
                return {}
            log(f"{BLUE_YELLOW}[VFS]{RESET} Loaded index cache: {YELLOW}{len(entries)}{RESET} file(s) from {CYAN}{_display_path(cache_path, self._display_base)}{RESET}")
            return entries
        except FileNotFoundError:
            return {}
        except Exception as e:
            log(f"{YELLOW}[VFS Warning]{RESET} Index cache unreadable ({e}), starting fresh.")
            return {}

    def _save_cache(self):
        try:
            cache_dir = os.path.dirname(self._cache_path)
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
            tmp_path = self._cache_path + ".tmp"
            # Write in a human-readable form: one file entry per line, sorted by
            # relative path so a specific file is easy to find in a text editor.
            # (json.load handles this format identically to the compact one.)
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write("{\r\n  \"version\": 1,\r\n  \"entries\": {\r\n")
                items = sorted(self._index_cache.items(), key=lambda kv: kv[0].lower())
                for i, (key, entry) in enumerate(items):
                    comma = "," if i < len(items) - 1 else ""
                    f.write("    " + json.dumps(key, ensure_ascii=False) + ": "
                            + json.dumps(entry, ensure_ascii=False, separators=(",", ": ")) + comma + "\r\n")
                f.write("  }\r\n}\r\n")
            os.replace(tmp_path, self._cache_path)
            log(f"{BLUE_YELLOW}[VFS]{RESET} Saved audio index cache ({len(self._index_cache)} file(s)) to {CYAN}{_display_path(self._cache_path, self._display_base)}{RESET}")
        except Exception as e:
            log(f"{YELLOW}[VFS Warning]{RESET} Could not save index cache: {e}")

    def _stat_attrs(self, path_lower, actual_size, mode):
        """Build a stat dict for a served file.

        NON-audio files (dummy videos, sidecar .txt, game data) are served from
        disk and must NOT appear read-only: the game's AVI reader opens them
        with GENERIC_READ|GENERIC_WRITE (standard for that era), and WinFsp
        refuses a write-access open on a 0o444 file - the game then can't play
        the dummy and skips the cutscene. 0o666 mirrors a real on-disk file
        (the hard-link era, which played fine).

        Metadata mirrors the REAL source file (timestamps, birth/creation
        time, inode, allocation) instead of a fixed fake time: the game reads
        file dates/sizes via FindFirstFileA (it has no GetFileSize/
        GetFileInformationByHandle imports), and a creation time of 1970-01-01
        (what WinFsp reports when st_birthtime is missing) made the file look
        alien compared to the hard-link era when the same videos played fine.
        """
        try:
            st = os.stat(self.virtual_to_real[path_lower])
        except (OSError, KeyError):
            st = None
        if st is not None:
            birth = getattr(st, 'st_birthtime', None) or st.st_mtime
            # Windows' os.stat has no st_blocks/st_blksize - synthesize them
            # (they only feed WinFsp's allocation-size reporting).
            st_blocks = getattr(st, 'st_blocks', None) or max(1, (actual_size + 511) // 512)
            st_blksize = getattr(st, 'st_blksize', None) or 4096
            return {
                'st_mode': mode,
                'st_nlink': st.st_nlink or 1,
                'st_size': actual_size,
                'st_ino': st.st_ino,
                'st_atime': st.st_atime,
                'st_mtime': st.st_mtime,
                'st_ctime': st.st_ctime,
                'st_birthtime': birth,
                'st_blocks': st_blocks,
                'st_blksize': st_blksize,
                'st_gid': 0, 'st_uid': 0,
            }
        return {
            'st_mode': mode,
            'st_nlink': 1,
            'st_size': actual_size,
            'st_atime': self.fixed_time, 'st_mtime': self.fixed_time, 'st_ctime': self.fixed_time,
            'st_birthtime': self.fixed_time,
            'st_ino': 0,
            'st_blocks': max(1, (actual_size + 511) // 512),
            'st_blksize': 4096,
            'st_gid': 0, 'st_uid': 0,
        }

    def getattr(self, path, fh=None):
        path_lower = path.lower().rstrip('/')
        if not path_lower:
            path_lower = '/'

        if path_lower in self.virtual_to_real:
            with self.lock:
                if path_lower in self.pcm_buffer_cache:
                    actual_size = len(self.pcm_buffer_cache[path_lower])
                else:
                    actual_size = self.wav_size_cache[path_lower]
            meta = self.audio_meta_cache.get(path_lower, {})
            # Audio files are decoded in memory and only ever read - keep them
            # read-only.
            mode = stat.S_IFREG | (0o444 if meta.get("is_audio") else 0o666)
            return self._stat_attrs(path_lower, actual_size, mode)

        if path_lower in self.virtual_dirs:
            return {'st_mode': stat.S_IFDIR | 0o555, 'st_nlink': 2, 'st_size': 4096}

        raise OSError(errno.ENOENT, os.strerror(errno.ENOENT))

    def readdir(self, path, fh):
        path_lower = path.lower().rstrip('/')
        if not path_lower:
            path_lower = '/'

        dirents = ['.', '..']
        if path_lower in self.case_map:
            dirents.extend(list(self.case_map[path_lower]))
        return dirents

    def open(self, path, flags):
        path_lower = path.lower()
        if path_lower in self.virtual_to_real:
            meta = self.audio_meta_cache[path_lower]

            # Non-audio files do not need dynamic decode pipeline buffers
            if not meta["is_audio"]:
                log(f"{WHITE_GREEN}[VFS PASSTHROUGH]{RESET} Non-audio file accessed - no decoding: {CYAN}{path}{RESET}")
                return 0

            with self.lock:
                if path_lower not in self.pcm_buffer_cache:
                    log(f"{YELLOW_BLUE}[VFS DECODE]{RESET} On-demand dynamic decode memory pipeline: {CYAN}{path}{RESET}")
                    audio_path = self.virtual_to_real[path_lower]
                    try:
                        decoded_file = miniaudio.decode_file(
                            audio_path,
                            sample_rate=meta["sample_rate"],
                            nchannels=meta["channels"],
                            output_format=meta["format"]
                        )
                        raw_pcm_bytes = decoded_file.samples.tobytes()

                        header = self._generate_wav_header(
                            len(raw_pcm_bytes),
                            sample_rate=meta["sample_rate"],
                            num_channels=meta["channels"],
                            bits_per_sample=meta["bits_per_sample"]
                        )
                        self.pcm_buffer_cache[path_lower] = header + raw_pcm_bytes
                    except Exception as e:
                        log(f"{RED}[VFS Error]{RESET} Failed decoding {path}: {e}")
                        return -errno.EIO
            return 0
        raise OSError(errno.ENOENT, os.strerror(errno.ENOENT))

    def read(self, path, length, offset, fh):
        path_lower = path.lower()
        if path_lower in self.virtual_to_real:
            meta = self.audio_meta_cache[path_lower]
            if not meta["is_audio"]:
                # Direct structural pass-through read for non-audio data files
                with open(self.virtual_to_real[path_lower], 'rb') as f:
                    f.seek(offset)
                    return f.read(length)

            with self.lock:
                if path_lower in self.pcm_buffer_cache:
                    return self.pcm_buffer_cache[path_lower][offset:offset + length]
        return b""

    def write(self, path, data, offset, fh):
        """Passthrough write for non-audio files: the game may open the dummy
        video (and other data files) read-write and occasionally write to it.
        Forward the bytes to the real file - exactly what a hard link did in
        the pre-VFS setup - so the write never fails and playback proceeds."""
        path_lower = path.lower()
        if path_lower in self.virtual_to_real:
            meta = self.audio_meta_cache.get(path_lower, {})
            if not meta.get("is_audio"):
                try:
                    with open(self.virtual_to_real[path_lower], 'r+b') as f:
                        f.seek(offset)
                        f.write(data)
                    return len(data)
                except OSError:
                    return 0
        return 0

    def truncate(self, path, length, fh=None):
        """Passthrough truncate for non-audio files (games may SetEndOfFile on
        a video handle); forwards to the real file."""
        path_lower = path.lower()
        if path_lower in self.virtual_to_real:
            meta = self.audio_meta_cache.get(path_lower, {})
            if not meta.get("is_audio"):
                try:
                    with open(self.virtual_to_real[path_lower], 'r+b') as f:
                        f.truncate(length)
                except OSError:
                    pass
        return 0

    def chmod(self, path, mode):
        """Accept attribute/ACL changes instead of failing with EROFS.

        A 1998-era game may call SetFileAttributesA on a video file before
        playing it (e.g. clear READONLY/ARCHIVE). On a real disk (the hard-link
        era, which played fine) that always succeeds; fusepy's default chmod
        raises EROFS, which WinFsp turns into an error the game could treat as
        "file unplayable". Attributes on this virtual volume are cosmetic, so
        accept and ignore the change."""
        return 0

    def release(self, path, fh):
        path_lower = path.lower()
        if path_lower in self.audio_meta_cache:
            if self.audio_meta_cache[path_lower]["is_audio"]:
                with self.lock:
                    if path_lower in self.pcm_buffer_cache:
                        log(f"{BLUE_YELLOW}[VFS MEMORY]{RESET} Releasing buffer allocation for closed track: {CYAN}{path}{RESET}")
                        del self.pcm_buffer_cache[path_lower]
        return 0

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Universal Game Audio VFS Translation Layer")
    parser.add_argument("--source", required=True, help="Absolute path containing the source audio files")
    parser.add_argument("--mount", default="X:", help="Virtual drive letter destination (e.g. X:)")
    parser.add_argument("--label", default="AudioVFS", help="Windows Volume Label visualization string")
    parser.add_argument("--cache", default=None, help="Optional path to the index metadata cache (JSON) to speed up repeated launches")
    parser.add_argument("--status", default=None, help="Optional startup status file for the launcher")
    args = parser.parse_args()
    # uid=-1/gid=-1: present every file as owned by the user who launched the
    # VFS. Without this, WinFsp maps uid/gid 0 to S-1-5-0 (SYSTEM), files end
    # up owned by SYSTEM, and Windows denies GENERIC_WRITE opens to the game
    # even when the mode is 0666 - the game then can't open the dummy video
    # read-write (standard for its AVI reader) and skips the cutscene.
    try:
        vfs = UniversalAudioVFS(args.source, cache_path=args.cache, status_path=args.status)
        vfs._status_update("mounting", "mount", message="Mounting virtual drive", force=True)
        FUSE(vfs, args.mount,
             foreground=True, nothreads=False,
             volname=args.label, FileinfoTimeout=-1, allow_other=True,
             uid=-1, gid=-1)
        vfs._status_update("stopped", "mount", message="Virtual drive stopped", force=True)
    except Exception as err:
        _write_status(args.status, "failed", "startup", 0,
                      message="%s: %s" % (type(err).__name__, err))
        raise

