# 🎮 Game Launcher · On-Demand Virtual File System

> 💾 **Save disk space without touching the game's behaviour.** Keep your game's MP3/OGG audio and video compressed on disk. The launcher mounts a virtual drive that decodes audio **in memory when the game opens it**, and plays the real, space-efficient MP4 video **inside the game window**, in place of a tiny placeholder file that the game believes is its original cutscene.

![Sample - Commandos 2 DP](<Sample - Commandos 2 DP.png>)

---

## Table of Contents

- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Project layout](#project-layout)
- [Quick start](#quick-start)
- [Preparing your files](#preparing-your-files)
- [Configuration](#configuration)
- [Python modules](#python-modules)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What it does

| Feature | Description |
| --- | --- |
| 🎵 **On-demand audio** | `.mp3` and `.ogg` files are presented to the game as `.wav` and decoded to PCM **in memory** when opened. Decoded audio is never written to disk. |
| 🧹 **Memory freed on close** | Decoded buffers are released as soon as the game closes the file. |
| 🎬 **Compressed video** | Videos are re-encoded to MP4, which is much smaller. The game still opens its original-format file, which is a tiny placeholder. |
| 🪞 **Mirrors your folders** | The `_vfs_data_` folder mirrors your game's folder tree, so every file is found where the game expects it. |
| 🧷 **Non-media passes through** | Any other file in `_vfs_data_` is served unchanged, with no decoding. |
| 🧽 **Clean exit** | The virtual drive, junctions and memory are cleaned up when the game exits. |

---

## How it works

```text
 🚀 Game Launcher.bat
   │  mounts virtual drive (e.g. X:), junctions audio folders, starts overlay + game
   ▼
 💾 WinFsp VFS (vfs_handler.py)  ── decodes MP3/OGG → PCM in memory on open
   │  serves files to the game
   ▼
 🎮 Game process ── opens placeholder video (tiny .avi)
   │  open / close detected by
   ▼
 👁️ video_overlay.py ── starts/stops the player
   ▼
 🎬 video_player.py ── plays the real MP4 + audio inside the game window
```

### Runtime sequence

1. **Dependency check.** The launcher finds Python, WinFsp and FFmpeg. Anything missing is installed automatically, and it installs the Python packages `fusepy`, `audioop-lts`, `miniaudio`, `pygame-ce` and `av`.
2. 💽 **Virtual drive.** It picks a free drive letter (preferring `X:`) and mounts `_vfs_data_` on it with the volume label `WaveAudio`.
3. 🔗 **Folder mapping.** Folders that contain audio are junctioned to the virtual drive. Folders with real files that are not audio are merged in with hard links.
4. 👁️ **Overlay.** The video overlay starts in the background. It watches the game process for opens and closes of the placeholder videos.
5. 🎮 **Game runs.** When the game opens a placeholder video, the matching real MP4 plays inside the game window, with its audio.
6. 🧹 **Cleanup.** When the game exits, the launcher unmounts the drive, removes the junctions and restores any folders it renamed.

---

## Requirements

| Requirement | Why | Install |
| --- | --- | --- |
| 🪟 **Windows** | The launcher is a `.bat` file that uses Win32 APIs | — |
| 🐍 **Python 3** | Runs the virtual drive and the video overlay | Installed by the launcher if missing ([python.org](https://www.python.org/downloads/)) |
| 💾 **WinFsp** | Provides the virtual file system driver | Installed by the launcher if missing ([winfsp.dev](https://winfsp.dev/)) |
| 🎞️ **FFmpeg** (`ffmpeg`, `ffprobe`, `ffplay`) | Re-encoding, probing and video decoding | Installed by the launcher if missing, via `winget` |

> 💡 The launcher checks these at startup and installs anything missing, so you do not need to install them yourself.

---

## Project layout

Place the launcher in the **game's root folder**, next to the game executable. The `_vfs_data_` folder mirrors the game's own folder tree, so every file sits at the same relative path the game expects. The folder names below are only examples; use whatever your game uses.

```text
Your Game\
├── 🚀 Game Launcher.bat        ← start the game with this
├── 🎮 game.exe                 ← your game (set GAME_EXE)
├── 📂 <game folders>\           ← the game's own files, unchanged
├── 📂 _vfs_data_\               ← compressed sources, same tree as the game
│   └── 📂 <same game folder>\
│       ├── 🎵 track01.ogg       ← served to the game as track01.wav
│       ├── 🎞️ intro.mp4         ← re-encoded video, played by the overlay
│       ├── 📝 intro.txt         ← sidecar: original format details
│       └── 🎞️ intro.avi         ← tiny placeholder the game opens
├── 📂 _vfs_scripts_\             ← launcher, Python modules and tools
└── 📂 _vfs_cache_\               ← runtime cache (created automatically)
```

## Quick start

1. Copy `Game Launcher.bat` and `_vfs_scripts_\` into the game's root folder.
2. Open `Game Launcher.bat` and set `GAME_EXE` to your game's executable name, for example `set "GAME_EXE=game.exe"`.
3. Put your converted files in `_vfs_data_\`, using the same relative paths the game uses ([see the layout](#project-layout)).
4. Double-click `Game Launcher.bat`. The launcher installs any missing [requirements](#requirements), mounts the virtual drive, starts the overlay and launches the game. When the game exits, it removes the junctions and unmounts the drive.

> The launcher maps each folder in `_vfs_data_` onto the same folder in the game directory. Where the game already has a real folder, the launcher keeps it and hard-links the non-audio and non-video files in from `_vfs_data_`, so both sets appear together.

---

## Preparing your files

### Audio: `_vfs_scripts_\tools\`

| Script | Result |
| --- | --- |
| `wav_to_ogg.bat` | Converts every `.wav` to `.ogg` (libvorbis, quality 5), then deletes the WAV |
| `wav_to_ogg_or_mp3.bat` | Lets you choose OGG (libvorbis) or MP3 (libmp3lame) and checks the sample rate and channels |
| `wav_to_mp3.bat` | Re-encodes WAV files to MP3 inside a WAV container, for games that expect `.wav` names |

### How to run the audio converters

Copy the converter batch file into the folder you want to convert, or into the game's working directory. The scripts convert **recursively**, so every sub-folder below it is converted too.

> ⚠️ Run these on copies first. They replace the originals.

Games with very large WAV collections benefit most. For example, Titan Fall ships about 34 GB of WAV files, and converting them to OGG takes a fraction of that space.

### Video: `video_re-encode_to_mp4.bat`

Run it inside a folder of videos (`.avi`, `.mp4`, `.mkv`, `.wmv`, `.mov` and more). For each video it:

1. 🔎 Reads the original codec, resolution, frame rate and audio format with `ffprobe`.
2. 🎞️ Re-encodes it to H.264 or H.265 MP4 with AAC audio.
3. 📝 Writes `<name>.txt`, a sidecar that records the original format details.
4. 🗑️ Deletes the original only after the MP4 is verified. If the encode fails, the original is kept.
5. 🪨 Creates the placeholder `<name>.<original extension>` that the game opens. It is a black, silent file with the same duration and frame rate and the smallest data it can be.

Videos that already have a `.txt` sidecar are skipped. Delete the sidecar to re-encode them.

> 💡 The placeholder keeps the game's timeline working, so the cutscene runs and can be skipped. The real MP4 is drawn inside the game window while the placeholder plays.

---

## Tested games

| Game | Status |
| --- | --- |
| Commandos: Behind Enemy Lines | Tested |
| Commandos: Beyond the Call of Duty | Tested |
| Commandos 2: Destination Paris (mod) | Tested |

Other games may work too, especially those with large WAV collections. Check the setup on a copy first.

---

## Configuration

All settings are at the top of `Game Launcher.bat`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `GAME_EXE` | *(empty)* | The game executable the launcher starts, e.g. `game.exe`. It is required. |
| `MAINTAIN_ASPECT_RATIO` | `true` | Video aspect ratio.<br>• `true`: letterbox the video to keep its aspect ratio.<br>• `false`: stretch the video to fill the game window.<br>If `ddraw.ini` (cnc-ddraw) sets `maintas` to false, that setting wins. |
| `VIDEO_WINDOW_TYPE` | `child` | How the video window attaches to the game.<br>• `child`: a true child window of the game. It moves and resizes with the game.<br>• `overlay`: a separate borderless window that floats on top and follows the game. Use it if a game refuses child windows, shows no video, or misbehaves. |
| `VIDEO_AUDIO_TYPE` | `sdl_mixer` | How video audio is played.<br>• `sdl_mixer`: SDL_Mixer (pygame.mixer), with lower CPU use. Recommended.<br>• `winmm`: the legacy WinMM waveOut API. Try it if audio stutters or is missing. |
| `VIDEO_PLAYER_TYPE` | `pyav` | Which decoder plays the video.<br>• `pyav`: decodes h264/h265 in-process with PyAV, so no `ffmpeg.exe` starts during playback. Recommended.<br>• `ffmpeg`: spawns `ffmpeg.exe` pipes for each video. Use it to compare, or if a codec fails with PyAV. |
| `OVERLAY_INTERRUPT_HOLD_MS` | `50` | How long, in milliseconds, the last video frame stays visible after a skip, so the game's menu redraw shows instead of a black flash.<br>• Raise it (e.g. `150`) if a flash remains.<br>• Lower it (e.g. `35`) if the video lingers after a skip. |
| `VIRTUAL_VIDEO_MODE` | `true` | How the placeholder videos are served and detected.<br>• `true`: the video folders are junctioned into the virtual drive, which hides the re-encoded sets and reports opens through a log file.<br>• `false`: the placeholder videos are hard-linked into the game folder, and opens are detected by polling the game process. |
| `VFS_VIDEO_DIAGNOSTICS` | `false` | Diagnostic logging.<br>• `false`: normal operation.<br>• `true`: serves the drive through `vfs_diagnostics.py`, which logs every file operation to `_vfs_cache_\vfs_diagnostics.log`, and also writes `_vfs_cache_\overlay_debug.log`. Use it only to debug playback, since it adds overhead. |

> 📝 `VFS_IDLE_TIMEOUT_SECONDS` is fixed at `300`. It limits how long startup can go without progress, not total startup time.

---

## Python modules

| Module | Role |
| --- | --- |
| 💾 `vfs_handler.py` | Virtual drive server (WinFsp via `fusepy`). Decodes MP3/OGG to PCM in memory on open |
| 🩺 `vfs_diagnostics.py` | Same as `vfs_handler.py`, plus a log of every file operation. Used when `VFS_VIDEO_DIAGNOSTICS=true` |
| 👁️ `video_overlay.py` | Watches the game process, detects placeholder opens and closes, and starts or stops the player |
| 🎬 `video_player.py` | Decodes and plays the real MP4 with audio, drawn in the game window |
| 🧾 `video_index.py` | Decides which files the video player handles. Shared by the VFS and the overlay |
| 🔍 `avi_scan_helper.py` | Scans the game's open file handles in a separate process, so a native crash cannot take down the overlay |
| 🔌 `ffmpeg_broker.py` | Runs FFmpeg decode jobs for the player |
| 🧪 `test_ffmpeg_broker.py` | Tests for `ffmpeg_broker.py` |
| 📦 `vfs_audio.py` | Earlier audio-only VFS. Not started by the launcher |

Troubleshooting helper: `_vfs_scripts_\capture_procmon.ps1` records a Process Monitor trace (run as Administrator) for debugging file access.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| 🚫 No video, or the game misbehaves | Set `VIDEO_WINDOW_TYPE` to `overlay` |
| ⬛ Black flash after a skip | Raise `OVERLAY_INTERRUPT_HOLD_MS` to `150` |
| ⏳ Video lingers after a skip | Lower `OVERLAY_INTERRUPT_HOLD_MS` to `35` |
| 🔇 Audio stutters or is silent | Set `VIDEO_AUDIO_TYPE` to `winmm` and compare |
| ⚠️ `VVS MERGE CONFLICT` | A real folder and its `.vfsbak` backup both exist. Keep one copy and run again |
| 🧱 Launcher reports the virtual drive did not mount | Check `_vfs_cache_\vfs_startup.status` for the last step reached |

---

## License

MIT license.

©️ Copyright 2026 - Thaer Razeq
