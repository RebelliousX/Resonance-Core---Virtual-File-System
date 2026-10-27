@echo off
setlocal enabledelayedexpansion
title Video Re-encoder to MP4 (H.264/H.265)
echo ================================================
echo  Video Re-encoder  -  output: MP4 / H.264-H.265
echo ================================================

rem ---- 1) Make sure ffmpeg is installed (before processing) ----
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo.
    echo [i] ffmpeg not found. Installing it via winget...
    winget install --id BtbN.FFmpeg.GPL.8.1 -e --accept-source-agreements --accept-package-agreements
    set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
    where ffmpeg >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [X] ffmpeg still not found after installation.
        echo     Close this window, reopen it, and run the script again.
        pause
        exit /b 1
    )
)
echo [i] ffmpeg is ready.

rem ---- 1a) Materialize the embedded copy of make_dummy.py next to this script ----
rem      This script is self-contained: the full source of make_dummy.py is
rem      stored READABLY at the end of this file, between the markers
rem      ":: MAKEDUMMY-BEGIN" and ":: MAKEDUMMY-END" (see the end of this
rem      file). Because it is embedded, this script can be copied to any
rem      folder and still generate the dummy videos the game needs. At
rem      startup we extract that block byte-for-byte with PowerShell into a
rem      real .py file next to this script; it is deleted again when done.
set "DUMMY_SRC=%~dp0make_dummy.py"
if exist "%DUMMY_SRC%" del "%DUMMY_SRC%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%~f0';$d='%~dp0make_dummy.py';$raw=[IO.File]::ReadAllText($p);$m=[regex]::Match($raw,'(?ms)^:: MAKEDUMMY-BEGIN\r?\n(.*?)^:: MAKEDUMMY-END\r?\n');if($m.Success){[IO.File]::WriteAllText($d,[regex]::Replace($m.Groups[1].Value,'\r\n',[string][char]10),[Text.UTF8Encoding]::new($false));Write-Host '    [i] make_dummy.py materialized from embedded readable copy.'}else{Write-Host '    [i] ERROR: embedded make_dummy.py markers not found.';exit 1}"
if not exist "%DUMMY_SRC%" (
    echo.
    echo [X] ERROR: make_dummy.py could not be materialized from the embedded
    echo     copy in this script. The re-encoder needs it to generate the
    echo     dummy videos the game plays, so the encoding will NOT run.
    echo     Aborting - no files were modified.
    echo.
    pause
    exit /b 1
)

rem ---- 1a1) Dummy-video parallelism ----
rem      Each dummy video is re-encoded in the ORIGINAL codec (e.g. Cinepak),
rem      which is single-threaded, so several dummies are generated at once.
rem      3 concurrent encodes is a safe default (8 froze this machine); you can
rem      raise it if your system copes. Passed to make_dummy.py as --jobs
rem      (used by the parallel pass in section 4).
set "DUMMY_JOBS=3"

rem ---- 1b) Choose output pixel format ----
rem      yuv420p is standard 4:2:0 (max compatibility). gbrp is RGB 4:4:4
rem      (HEVC "Rext" profile - what the original encoder produced) and keeps
rem      full color detail, but it needs a Rext-capable encoder.
set "PIXFMT=yuv420p"
echo.
call :SetESC
chcp 65001 >nul
echo ╔══════════════════╦═════════════════════╦═════════════════════╗
echo ║ %ESC%[1;97mMetric          %ESC%[0m ║ %ESC%[1;93mgbrp (RExt Profile)%ESC%[0m ║ %ESC%[1;92myuv420p (Main      %ESC%[0m ║
echo ║                  ║                     ║ %ESC%[1;92mProfile)           %ESC%[0m ║
echo ╠══════════════════╬═════════════════════╬═════════════════════╣
echo ║ %ESC%[97mColor Space     %ESC%[0m ║ %ESC%[93mNative RGB (Green, %ESC%[0m ║ %ESC%[92mYCbCr (Luminance + %ESC%[0m ║
echo ║                  ║ %ESC%[93mBlue, Red Planar)  %ESC%[0m ║ %ESC%[92mSubsampled Chroma) %ESC%[0m ║
echo ║                  ║                     ║                     ║
echo ║ %ESC%[97mChroma Sampling %ESC%[0m ║ %ESC%[93m4:4:4 (No color    %ESC%[0m ║ %ESC%[92m4:2:0 (Color       %ESC%[0m ║
echo ║                  ║ %ESC%[93mcompression)       %ESC%[0m ║ %ESC%[92mresolution cut by  %ESC%[0m ║
echo ║                  ║                     ║ %ESC%[92m75%%)               %ESC%[0m ║
echo ║                  ║                     ║                     ║
echo ║ %ESC%[97mVisual Quality  %ESC%[0m ║ %ESC%[93mMaximum (Perfect   %ESC%[0m ║ %ESC%[92mStandard (Good for %ESC%[0m ║
echo ║                  ║ %ESC%[93mfor text, UI, and  %ESC%[0m ║ %ESC%[92mmovies, poor for   %ESC%[0m ║
echo ║                  ║ %ESC%[93mgrading)           %ESC%[0m ║ %ESC%[92msmall text)        %ESC%[0m ║
echo ║                  ║                     ║                     ║
echo ║ %ESC%[97mFile Size       %ESC%[0m ║ %ESC%[93mSignificantly      %ESC%[0m ║ %ESC%[92mExtremely Compact  %ESC%[0m ║
echo ║                  ║ %ESC%[93mLarger (Up to 2x-3x%ESC%[0m ║ %ESC%[92m(Highly efficient  %ESC%[0m ║
echo ║                  ║ %ESC%[93mlarger)            %ESC%[0m ║ %ESC%[92mcompression)       %ESC%[0m ║
echo ║                  ║                     ║                     ║
echo ║ %ESC%[97mCompatibility   %ESC%[0m ║ %ESC%[93mVery Poor (Fails on%ESC%[0m ║ %ESC%[92mUniversal (Plays on%ESC%[0m ║
echo ║                  ║ %ESC%[93mmost TVs, browsers,%ESC%[0m ║ %ESC%[92malmost every       %ESC%[0m ║
echo ║                  ║ %ESC%[93mand HW players)    %ESC%[0m ║ %ESC%[92mscreen/device)     %ESC%[0m ║
echo ╚══════════════════╩═════════════════════╩═════════════════════╝
echo.
echo Select output pixel format:
echo   [1] yuv420p - standard 4:2:0, max compatibility
echo   [2] gbrp    - RGB 4:4:4 / HEVC "Rext" profile, original format
choice /C 12 /N /M "Pixel format [1=yuv420p 2=gbrp]: "
if errorlevel 2 set "PIXFMT=gbrp"
echo [i] Output pixel format: !PIXFMT! - audio stays at the source AVI rate.

rem ---- 1c) Choose video codec (H.265 or H.264) ----
rem      CRF_VAL is the software-encoder quality target (constant quality, in
rem      x265/x264 CRF units: lower = better quality / bigger file). 28 was
rem      measured to look the same as the old NVENC output on these cutscenes;
rem      x264 needs a slightly lower CRF (27) to match that look, but produces
rem      ~30% bigger files than x265 at equal quality.
set "VCODEC=hevc"
set "VCODEC_DISP=H.265"
set "SOFT_ENC=libx265"
set "SOFT_NAME=libx265 (software)"
set "CODEC_TAG=-tag:v hvc1"
set "CRF_VAL=28"
echo.
echo Select video codec:
echo   [1] H.265 / HEVC - better compression, smaller files
echo   [2] H.264 / AVC  - wider compatibility, faster encode
choice /C 12 /N /M "Codec [1=H.265 2=H.264]: "
if errorlevel 2 (
    set "VCODEC=h264"
    set "VCODEC_DISP=H.264"
    set "SOFT_ENC=libx264"
    set "SOFT_NAME=libx264 (software)"
    set "CODEC_TAG=-tag:v avc1"
    set "CRF_VAL=27"
)
echo [i] Codec: !VCODEC_DISP! (software encoder: !SOFT_ENC!, crf !CRF_VAL!)

rem ---- 1d) Choose encoder mode: hardware (speed) or software (size) ----
rem      Hardware encoders (NVENC/QSV/AMF) encode very fast but produce larger
rem      files for the same visual quality. The software encoder (preset slow,
rem      crf !CRF_VAL!) is slower but makes noticeably smaller files at the
rem      same look. If "hardware" is chosen but no hardware encoder is
rem      available, the software encoder is used instead.
echo.
echo Select encoder mode:
echo   [1] Hardware - fast encoding (NVENC / QSV / AMF auto-detected)
echo   [2] Software - smaller files (!SOFT_ENC!, preset slow, crf !CRF_VAL!)
choice /C 12 /N /M "Encoder [1=hardware/fast 2=software/smaller]: "
set "ENC_MODE=hardware"
if errorlevel 2 set "ENC_MODE=software"
echo [i] Encoder mode: !ENC_MODE!.

rem ---- 2) Pick the encoder: hardware if chosen and available, else software ----
rem      The software settings below are the default; hardware detection only
rem      runs in "hardware" mode and each check does a tiny real encode (not
rem      just "is it compiled in"), so an encoder is only used if the actual
rem      GPU/hardware can initialize it.
set "VCODEC_ARGS=-c:v !SOFT_ENC! -preset slow -crf !CRF_VAL!"
set "ENC_NAME=!SOFT_ENC! (software, preset slow, crf !CRF_VAL!)"
set "ENC_TEST=%TEMP%\cncdd_enc_test.mp4"
set "ENC_PROBE=%TEMP%\cncdd_enc_probe.txt"
if "!ENC_MODE!"=="software" goto encoder_done

echo [i] Detecting a hardware encoder...

rem NVIDIA NVENC
call :TryEncoder !VCODEC!_nvenc "-preset p7 -rc vbr -cq 20 -b:v 0" "NVIDIA NVENC !VCODEC!_nvenc"
if "!ENC_FOUND!"=="1" goto encoder_done

rem Intel Quick Sync Video
call :TryEncoder !VCODEC!_qsv "-preset veryslow -global_quality 20" "Intel Quick Sync !VCODEC!_qsv"
if "!ENC_FOUND!"=="1" goto encoder_done

rem AMD AMF
call :TryEncoder !VCODEC!_amf "-quality quality -rc cqp -qp_i 20 -qp_p 22" "AMD AMF !VCODEC!_amf"
if "!ENC_FOUND!"=="1" goto encoder_done

:encoder_done
echo [i] Using !ENC_NAME!.

rem ---- 3) Re-encode every video file in this folder ----
for %%F in (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts *.mpg *.mpeg *.3gp *.vob *.ogv) do (
    if exist "%%~dpnF.txt" (
        echo [i] Skipping %%~nxF - already has %%~dpnF.txt. Delete the .txt to re-encode.
    ) else (
        echo.
        echo [*] Processing: %%~nxF
        set "vcodec="
        set "acodec="
        set "vwidth="
        set "vheight="
        set "vfps="
        set "asamplerate="
        set "achannels="

        for /f "delims=" %%C in ('ffprobe -v error -select_streams v:0 -show_entries stream^=codec_name -of default^=noprint_wrappers^=1:nokey^=1 "%%~fF"') do set "vcodec=%%C"
        for /f "delims=" %%C in ('ffprobe -v error -select_streams a:0 -show_entries stream^=codec_name -of default^=noprint_wrappers^=1:nokey^=1 "%%~fF"') do set "acodec=%%C"
        for /f "delims=" %%C in ('ffprobe -v error -select_streams v:0 -show_entries stream^=width -of default^=noprint_wrappers^=1:nokey^=1 "%%~fF"') do set "vwidth=%%C"
        for /f "delims=" %%C in ('ffprobe -v error -select_streams v:0 -show_entries stream^=height -of default^=noprint_wrappers^=1:nokey^=1 "%%~fF"') do set "vheight=%%C"
        for /f "delims=" %%C in ('ffprobe -v error -select_streams v:0 -show_entries stream^=r_frame_rate -of default^=noprint_wrappers^=1:nokey^=1 "%%~fF"') do set "vfps=%%C"
        for /f "delims=" %%C in ('ffprobe -v error -select_streams a:0 -show_entries stream^=sample_rate -of default^=noprint_wrappers^=1:nokey^=1 "%%~fF"') do set "asamplerate=%%C"
        for /f "delims=" %%C in ('ffprobe -v error -select_streams a:0 -show_entries stream^=channels -of default^=noprint_wrappers^=1:nokey^=1 "%%~fF"') do set "achannels=%%C"

        if not defined vcodec set "vcodec=none"
        if not defined acodec set "acodec=none"
        if not defined vwidth set "vwidth=0"
        if not defined vheight set "vheight=0"
        if not defined vfps set "vfps=0"
        if not defined asamplerate set "asamplerate=0"
        if not defined achannels set "achannels=0"
        set "vfps=!vfps:/1=!"

        echo     original video codec: !vcodec!
        echo     original audio codec: !acodec!
        echo     original extension:   %%~xF
        echo     original resolution:  !vwidth!x!vheight!
        echo     original frame rate:  !vfps!
        echo     original audio:       !asamplerate! Hz, !achannels! ch
        echo     encoding to MP4 - !VCODEC_DISP! with !ENC_NAME!...

        REM -vf format=!PIXFMT! converts the RGB cinepak frames to the chosen
        REM pixel format before the encoder. Audio rate/channels are left at the
        REM source AVI's values.
        if exist "%%~dpnF.reenc_tmp" del "%%~dpnF.reenc_tmp"
        ffmpeg -y -hide_banner -loglevel error -stats -i "%%~fF" -vf "format=!PIXFMT!" !VCODEC_ARGS! -c:a aac -b:a 192k -movflags +faststart !CODEC_TAG! -f mp4 "%%~dpnF.reenc_tmp"

        REM Verify the output really has a video stream before touching the
        REM original. ffmpeg can exit 0 even when the encoder failed to open.
        call :IsValidVideo "%%~dpnF.reenc_tmp"
        if "!ENC_FOUND!"=="1" (
            (
                echo Original video codec: !vcodec!
                echo Original audio codec: !acodec!
                echo Original file extension: %%~xF
                echo Resolution: !vwidth!x!vheight!
                echo Frames per second: !vfps!
                echo Audio sample rate: !asamplerate!
                echo Audio channels: !achannels!
            ) > "%%~dpnF.txt"
            del "%%~fF"
            ren "%%~dpnF.reenc_tmp" "%%~nF.mp4"
            echo     [OK] Finished %%~nF.mp4 - original deleted.
            REM The dummy video for this file is generated later, in the
            REM parallel pass below (section 4): dummies re-encode in the
            REM original codec (e.g. Cinepak), so several run at once.
        ) else (
            echo     [X] Encode FAILED for %%~nxF - original file KEPT.
            if exist "%%~dpnF.reenc_tmp" del "%%~dpnF.reenc_tmp"
        )
    )
)

rem ---- 4) Generate dummy videos for every re-encoded MP4 (in parallel) ----
rem      Dummies are made after all re-encodes, in one parallel pass: each
rem      dummy re-encodes in the ORIGINAL codec (e.g. Cinepak), which is
rem      single-threaded, so !DUMMY_JOBS! of them run at once. make_dummy.py
rem      skips MP4s that already have a dummy, so this pass also fills in any
rem      dummies missing from earlier runs.
if exist "%DUMMY_SRC%" (
    set "PYBIN="
    where python >nul 2>nul && set "PYBIN=python"
    if not defined PYBIN where py >nul 2>nul && set "PYBIN=py"
    if defined PYBIN (
        echo.
        echo [i] Generating dummy videos - up to !DUMMY_JOBS! in parallel...
        !PYBIN! "%DUMMY_SRC%" --dir . --jobs !DUMMY_JOBS!
    ) else (
        echo.
        echo [i] python not found - dummy videos not generated.
    )
) else (
    echo.
    echo [i] make_dummy.py not generated - dummy videos not created.
)

rem Delete the materialized make_dummy.py copy - it is only needed while this
rem script is running (the dummy videos it produced stay behind).
if exist "%DUMMY_SRC%" del "%DUMMY_SRC%" >nul 2>&1

echo.
echo ============================================
echo  All done.
echo ============================================
pause
exit /b 0

rem ====================== subroutines ======================

:SetESC
rem Generates the ANSI escape character and stores it in ESC.
for /F "tokens=1,2 delims=#" %%a in ('"prompt #$H#$E# & echo on & for %%b in (1) do rem"') do (
    set ESC=%%b
    exit /B 0
)
exit /B 0

:TryEncoder
rem arg1 = encoder name, arg2 = encoder options, arg3 = display name
set "ENC_FOUND=0"
if exist "%ENC_TEST%" del "%ENC_TEST%" 2>nul
ffmpeg -hide_banner -loglevel error -f lavfi -i color=black:s=320x240:d=0.5 -vf "format=!PIXFMT!" -c:v %~1 %~2 -f mp4 "%ENC_TEST%" 2>nul
call :IsValidVideo "%ENC_TEST%"
if "!ENC_FOUND!"=="1" (
    set "VCODEC_ARGS=-c:v %~1 %~2"
    set "ENC_NAME=%~3"
)
if exist "%ENC_TEST%" del "%ENC_TEST%" 2>nul
exit /b 0

:IsValidVideo
rem arg1 = file to probe; sets ENC_FOUND=1 if it has a video stream
set "ENC_FOUND=0"
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of default=noprint_wrappers=1:nokey=1 "%~1" > "%ENC_PROBE%" 2>nul
set "PROBE_VCODEC="
for /f "usebackq delims=" %%P in ("%ENC_PROBE%") do set "PROBE_VCODEC=%%P"
if defined PROBE_VCODEC set "ENC_FOUND=1"
exit /b 0


:: ============================================================================
:: EMBEDDED make_dummy.py - READABLE copy (extracted at startup, see section 1a)
:: ----------------------------------------------------------------------------
:: The Python source below is the complete make_dummy.py, stored verbatim so
:: this script is self-contained - this embedded copy is the only copy.
:: ============================================================================
:: MAKEDUMMY-BEGIN
#!/usr/bin/env python3
"""
make_dummy.py - generate the smallest possible dummy video for a re-encoded
video file. The dummy is a real container file, in the ORIGINAL container
type of the source (AVI, WMV, MOV, MP4, MKV, ... taken from the sidecar
.txt extension), that plays for the exact same duration as the re-encoded
MP4 but contains as little data as possible, so a game/player that only
cares about the timeline runs normally while the real (heavier) MP4 is
shown separately.

The original format is described by the companion <name>.txt written by
reencode_mp4.bat, e.g.:

    Original video codec: cinepak
    Original audio codec: pcm_s16le
    Original file extension: .avi
    Resolution: 320x240
    Frames per second: 12
    Audio sample rate: 22050
    Audio channels: 2

Key parsing is case-insensitive and missing keys fall back to safe defaults,
so any of the above lines may be absent.

The dummy is a black video at the SOURCE frame rate (the original file's
"Frames per second" from the sidecar .txt, or probed from the MP4) with a
single keyframe (the frame at t=0) and ~1 s of silence (--audio minimal),
spanning the exact duration of the re-encoded MP4.

Validated strategy (Commandos BCD, 2026): old games drive the cutscene
timeline from the VIDEO stream and only require an audio stream to EXIST
(its length does not matter). The game's video renderer also gates its
ESC/skip handling on the frame rate - 1-2 fps dummies decode but the game
ignores ESC for ~20 s, while dummies at the source frame rate (12 fps)
quit instantly. The keyframe interval does not matter for that behaviour,
so the dummy uses a single keyframe (fewest bytes). At 640x240 cinepak,
every delta frame costs ~1.2 KB regardless of content (a per-block
selector), which sets the size floor (~15 KB/s at 12 fps) - the price of
full-rate playback. If no frame rate can be determined, the file is skipped
instead of falling back to 1 fps (a 1 fps dummy makes the game ignore
ESC/skip for ~20 s).

Use --fps to override the frame rate, --gop N for a denser keyframe
interval (--gop 1 = every frame a keyframe; only if a game proves to be
keyframe-driven), and --audio full if a game's timeline is audio-driven
(audio must then span the full duration).

Usage:
    make_dummy.py --file path/to/name.mp4
    make_dummy.py --dir  path/to/folder
    make_dummy.py --file name.mp4 --gop 1   # override: keyframe every frame
    make_dummy.py --file name.mp4 --fps 2 --audio full --force
    make_dummy.py --dir path/to/folder --jobs 4   # 4 dummies at once
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_FFMPEG = r"C:\Users\Thaer\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
DEFAULT_FFPROBE = r"C:\Users\Thaer\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"

MIN_AUDIO_SECONDS = 1
MIN_AUDIO_RATE = 8000
AUDIO_CHANNELS = 1

MAX_GOP = 100000  # -g for a single keyframe (t=0 only) on black dummies

# Parallel dummy generation prints from several worker threads at once, so
# route all progress output through this lock to keep lines from interleaving.
_PRINT_LOCK = threading.Lock()


def log(*args):
    with _PRINT_LOCK:
        print(*args, flush=True)


# --- Universal container support -------------------------------------------
# The dummy is muxed into the ORIGINAL container (from the sidecar .txt
# extension) so any game's decoder can demux it - not just AVI.
MUXERS = {
    "avi": "avi", "wmv": "asf", "mov": "mov", "mp4": "mp4", "m4v": "mp4",
    "mkv": "matroska", "webm": "webm", "ogv": "ogg", "ogg": "ogg",
    "flv": "flv", "3gp": "3gp", "mpg": "mpeg", "mpeg": "mpeg",
    "ts": "mpegts", "vob": "vob",
}
# Some containers cannot carry raw pcm_u8, so pick a tiny audio codec per
# muxer (codec, sample rate). 22050 is the lowest MPEG-2 Layer II supports.
AUDIO_PRESETS = {
    "avi": ("pcm_u8", MIN_AUDIO_RATE),
    "asf": ("wmav2", MIN_AUDIO_RATE),
    "mov": ("aac", MIN_AUDIO_RATE),
    "mp4": ("aac", MIN_AUDIO_RATE),
    "matroska": ("aac", MIN_AUDIO_RATE),
    "webm": ("libopus", MIN_AUDIO_RATE),
    "ogg": ("libvorbis", MIN_AUDIO_RATE),
    "flv": ("mp3", 11025),  # FLV rejects 8000 Hz; 11025 is its lowest rate
    "3gp": ("aac", MIN_AUDIO_RATE),
    "mpeg": ("mp2", 22050),
    "mpegts": ("mp2", 22050),
    "vob": ("mp2", 22050),
}


def muxer_for(ext):
    """ffmpeg muxer name for a filename extension (lowercase, no dot)."""
    return MUXERS.get(ext, "avi")


def find_exe(name, fallback):
    p = shutil.which(name)
    if p:
        return p
    if fallback and os.path.isfile(fallback):
        return fallback
    return None


def parse_spec(txt):
    spec = {}
    with open(txt, encoding="utf-8-sig", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or ":" not in line:
                continue
            k, v = [x.strip() for x in line.split(":", 1)]
            spec[k.lower()] = v
    return spec


def get(spec, *aliases):
    for a in aliases:
        a = a.lower()
        if a in spec:
            return spec[a]
    return None


def parse_resolution(val, default=(320, 240)):
    if not val:
        return default
    nums = re.findall(r"\d+", val)
    if len(nums) >= 2:
        try:
            return int(nums[0]), int(nums[1])
        except ValueError:
            pass
    return default


def probe_json(ffprobe, path, extra):
    cmd = [ffprobe, "-v", "error", "-of", "json"] + extra + [path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def probe_duration(ffprobe, path):
    info = probe_json(ffprobe, path, ["-show_entries", "format=duration"])
    if not info or "format" not in info:
        return None
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None


def source_fps(spec, ffprobe, mp4):
    """Source frame rate: sidecar .txt first, else probed from the mp4, else None."""
    v = get(spec, "frames per second", "fps", "frame rate")
    if v:
        try:
            f = float(v)
            if f > 0:
                return f
        except ValueError:
            pass
    info = probe_json(ffprobe, mp4, [
        "-select_streams", "v",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate",
    ])
    if info and "streams" in info and info["streams"]:
        for key in ("avg_frame_rate", "r_frame_rate"):
            rate = info["streams"][0].get(key)
            if rate and "/" in rate:
                try:
                    num, den = rate.split("/")
                    f = float(num) / float(den) if float(den) else 0.0
                    if f > 0:
                        return f
                except (ValueError, ZeroDivisionError):
                    pass
    return None


def cinepak_extra(codec):
    if codec and codec.lower() in ("cinepak", "cvid"):
        return ["-pix_fmt", "rgb24",
                "-min_strips", "1", "-max_strips", "1",
                "-max_extra_cb_iterations", "0", "-skip_empty_cb", "true"]
    return []


def encode(ffmpeg, spec, dur, out, fps=None, audio="none", res=None, gop=None):
    codec = get(spec, "original video codec")
    if not codec:
        return None, "txt has no video codec"
    if not fps or fps <= 0:
        # No 1 fps fallback: a 1 fps dummy makes the game ignore ESC/skip for
        # ~20 s, so refuse to build one without a real frame rate.
        return None, "no usable frame rate (add 'Frames per second' to the .txt or use --fps)"
    w, h = res if res else parse_resolution(get(spec, "resolution", "resolution width"), (320, 240))
    rate = str(int(fps)) if float(fps).is_integer() else str(fps)
    rate_val = float(fps)

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    cmd += ["-f", "lavfi", "-i", "color=c=black:s=%dx%d:r=25" % (w, h)]
    if audio in ("minimal", "full"):
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=%d:cl=%s" % (MIN_AUDIO_RATE, "mono")]
    # -t: span the full mp4 duration without an extra second of black tail.
    # Frames land every 1/rate s, so the smallest end time that still has a
    # frame at/after dur is ceil(dur*rate)/rate (+ a hair so ffmpeg emits it).
    # The old +1.0 s made every dummy ~1 s longer than the original.
    t_end = math.ceil(dur * rate_val) / rate_val + 0.001
    cmd += ["-t", "%.6f" % t_end]
    opts = ["-bf", "0"]  # no B-frames: their reorder delay shifts timestamps in
                      # some muxers (flv/mpegts) on these tiny 1 fps clips
    if gop is not None:
        # Explicit keyframe interval (frames). gop 1 = every frame a keyframe
        # (a game that drives its input/ESC handling from decoder keyframes
        # becomes responsive within 1 s), at the cost of a larger file.
        opts += ["-g", str(int(gop))]
    elif codec and codec.lower() == "theora":
        opts += ["-g", "1"]  # libtheora drops identical frames unless all-keyframe
    else:
        # Single keyframe (t=0): the game plays forward only and the keyframe
        # interval does not affect its input/ESC handling (validated at 12 fps),
        # so one keyframe keeps the black dummy smallest.
        opts += ["-g", str(MAX_GOP)]
    cmd += ["-r", rate, "-c:v", codec] + cinepak_extra(codec) + opts
    if audio == "minimal":
        cmd += ["-af", "atrim=0:%d,asetpts=PTS-STARTPTS" % MIN_AUDIO_SECONDS]
    muxer = muxer_for(os.path.splitext(out)[1].lstrip(".").lower())
    a_codec, a_rate = AUDIO_PRESETS.get(muxer, ("pcm_u8", MIN_AUDIO_RATE))
    if audio in ("minimal", "full"):
        cmd += ["-c:a", a_codec, "-ar", str(a_rate), "-ac", str(AUDIO_CHANNELS)]
    cmd += ["-f", muxer, out]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None, "ffmpeg failed:\n%s%s" % (r.stdout, r.stderr)
    return True, None


def verify(ffprobe, out, expected_dur, want_audio, fps):
    info = probe_json(ffprobe, out, [
        "-show_entries", "stream=codec_type,codec_name",
        "-show_entries", "format=duration",
    ])
    if not info or "streams" not in info:
        return None, "cannot read dummy with ffprobe"
    video = None
    audio = None
    for s in info["streams"]:
        if s.get("codec_type") == "video":
            video = s
        elif s.get("codec_type") == "audio":
            audio = s
    if video is None:
        return None, "dummy has no video stream"
    if want_audio and audio is None:
        return None, "dummy missing audio stream"
    if not want_audio and audio is not None:
        return None, "dummy has unexpected audio stream"
    # The game's timeline is driven by the video stream, so verify it spans the
    # mp4 duration. Some muxers (mpeg/flv/mpegts) store odd absolute timestamps
    # or unreliable container durations on tiny clips, so we check the SPAN of
    # the video frames (last - first) instead of absolute values.
    frames = probe_json(ffprobe, out, [
        "-select_streams", "v", "-show_entries", "frame=best_effort_timestamp_time",
    ])
    times = []
    if frames and "frames" in frames:
        for f in frames["frames"]:
            t = f.get("best_effort_timestamp_time")
            if t is not None:
                try:
                    times.append(float(t))
                except (TypeError, ValueError):
                    pass
    if times:
        span = max(times) - min(times)
        if abs(span - expected_dur) > 1.5:
            return None, "dummy video spans %.3fs != mp4 %.3fs" % (span, expected_dur)
        if max(times) < expected_dur - 1.0:
            return None, "dummy last frame at %.3fs ends before mp4 %.3fs" % (max(times), expected_dur)
        return {"span": span, "frames": len(times), "codec": video.get("codec_name"),
                "has_audio": audio is not None}, None
    # Frame timestamps unreadable (e.g. some MPEG-PS muxers): fall back to a
    # frame-count check - the framing lands ceil(duration*fps) + 1 frames.
    nframes = probe_json(ffprobe, out, [
        "-select_streams", "v", "-count_frames", "-show_entries", "stream=nb_read_frames",
    ])
    got = None
    if nframes and "streams" in nframes and nframes["streams"]:
        got = nframes["streams"][0].get("nb_read_frames")
    if got is None:
        return None, "cannot read dummy video frame timestamps"
    want = int(math.ceil(expected_dur * fps)) + 1
    if int(got) != want:
        return None, "dummy has %s frames != expected %s" % (got, want)
    return {"span": None, "frames": int(got), "codec": video.get("codec_name"),
            "has_audio": audio is not None}, None


def make_dummy(ffmpeg, ffprobe, mp4, txt, audio, fps, res, force, gop=None):
    ext = get(spec := parse_spec(txt), "original file extension", "file extension", "extension") or "avi"
    if fps is None:
        # Default to the source frame rate (txt spec, else probed from the mp4).
        # There is no 1 fps fallback: 1 fps dummies decode but the game ignores
        # ESC for ~20 s, while the source rate (12 fps) quits instantly.
        fps = source_fps(spec, ffprobe, mp4)
    if fps is None:
        log("  [warn] %s: cannot determine a frame rate (missing .txt fps and "
            "no video rate in the mp4) - skipped" % os.path.basename(mp4))
        return 0
    ext = ext.strip().lstrip(".")
    out = os.path.join(os.path.dirname(mp4), os.path.basename(mp4)[:-4] + "." + ext)
    if os.path.exists(out) and not force:
        log("  skip %s (exists; use --force)" % os.path.basename(out))
        return 0

    dur = probe_duration(ffprobe, mp4)
    if dur is None or dur <= 0:
        log("  [warn] cannot probe duration of %s" % os.path.basename(mp4))
        return 0

    t0 = time.time()
    ok, err = encode(ffmpeg, spec, dur, out, fps=fps, audio=audio, res=res, gop=gop)
    if not ok:
        log("  [error] %s: %s" % (os.path.basename(mp4), err))
        return 2

    res_info, verr = verify(ffprobe, out, dur, audio != "none", fps)
    if verr:
        log("  [error] %s: %s" % (os.path.basename(mp4), verr))
        return 2

    sz = os.path.getsize(out)
    log("  %-14s %.3fs -> %s (%.2f KB, %d frame(s), audio=%s, %.1fs)"
        % (os.path.basename(out), dur, "OK", sz / 1024.0,
           res_info.get("frames", "?"), res_info["has_audio"], time.time() - t0))
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="folder to scan for *.mp4 with matching *.txt")
    ap.add_argument("--file", help="single mp4 to process")
    ap.add_argument("--fps", type=float, default=None,
                    help="explicit video frame rate (default: the source file's frame rate "
                         "from the .txt spec, probed from the mp4 if absent)")
    ap.add_argument("--audio", choices=["none", "minimal", "full"], default="minimal",
                    help="audio strategy (default minimal: ~1 s silence; use full if the "
                         "game's timeline is audio-driven, none if it accepts video-only)")
    ap.add_argument("--res", default=None, help="override resolution, e.g. 320x240")
    ap.add_argument("--gop", type=int, default=None,
                    help="keyframe interval in frames (default: encoder default, "
                         "12 for cinepak = a keyframe every 12 s on a 1 fps dummy; "
                         "use 1 or 2 to make a game's input/ESC handling responsive "
                         "if it is keyframe-driven - the dummy gets larger)")
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--ffprobe", default=None)
    ap.add_argument("--force", action="store_true", help="regenerate even if the dummy exists")
    ap.add_argument("--jobs", type=int, default=None,
                    help="how many dummy encodes to run at once (default: number "
                         "of CPU cores, capped at 3). Dummy encodes are "
                         "single-threaded, so this speeds up --dir runs.")
    args = ap.parse_args()

    ffmpeg = args.ffmpeg or find_exe("ffmpeg", DEFAULT_FFMPEG)
    ffprobe = args.ffprobe or find_exe("ffprobe", DEFAULT_FFPROBE)
    if not ffmpeg:
        print("[error] ffmpeg not found", flush=True)
        return 1
    if not ffprobe:
        print("[error] ffprobe not found", flush=True)
        return 1

    res = None
    if args.res:
        res = parse_resolution(args.res)

    mp4s = []
    if args.file:
        mp4s.append(os.path.abspath(args.file))
    elif args.dir:
        for n in sorted(os.listdir(args.dir)):
            if n.lower().endswith(".mp4"):
                mp4s.append(os.path.join(args.dir, n))
    else:
        ap.error("provide --file or --dir")

    jobs = args.jobs or min(os.cpu_count() or 2, 3)

    def run_one(mp4):
        txt = mp4[:-4] + ".txt"
        if not os.path.isfile(txt):
            log("  [warn] no spec txt next to %s - skipped" % os.path.basename(mp4))
            return 0
        return make_dummy(ffmpeg, ffprobe, mp4, txt, args.audio, args.fps, res,
                          args.force, gop=args.gop)

    if jobs > 1 and len(mp4s) > 1:
        log("Using %d parallel dummy job(s) (%d mp4 file(s))..." % (jobs, len(mp4s)))
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            results = list(ex.map(run_one, mp4s))
    else:
        results = [run_one(m) for m in mp4s]

    failed = sum(1 for rc in results if rc >= 2)
    made = sum(1 for rc in results if rc == 1)
    log("Done: %d dummy(s), %d skipped/failed" % (made, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
:: MAKEDUMMY-END
