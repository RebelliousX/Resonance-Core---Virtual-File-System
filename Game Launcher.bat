@echo off
title Resonance Core: VFS
REM Make variables local to this script only. Enable delayed variable expansion
setlocal EnableDelayedExpansion

:: =====================================================================
:: Game executable that this script will launch
:: =====================================================================
set "GAME_EXE="

:: =====================================================================
:: Video overlay: maintain the videos' playback aspect ratio
::   true  = letterbox videos, keep their original aspect ratio
::   false = stretch videos to fill the game window
:: NOTE: if ddraw.ini (cnc-ddraw) is present and its 'maintas' is false,
::       ddraw.ini takes precedence and videos are stretched even when
::       this flag is true, so the overlay matches the stretched game.
:: =====================================================================
set "MAINTAIN_ASPECT_RATIO=true"

:: =====================================================================
:: Video overlay: video player window type
::   child   = the video plays in a true CHILD window of the game window
::             (SetParent). The video is clipped to the game's client area
::             and moves/resizes with it automatically, so it feels like it
::             is part of the game window - the most seamless experience.
::             Caveat: a few games refuse to accept an external child
::             window (the video may not appear, or the game may misbehave
::             or freeze). If you see no video or the game acts up, switch
::             this flag to "overlay".
::   overlay = the video plays in a separate borderless window that FLOATS
::             on top of the game window (WS_EX_TOPMOST). It keeps
::             monitoring the game window's position and size and follows
::             it seamlessly when the game is moved or resized. It is a
::             little less "attached" than a child window (it is its own
::             top-level window), but works with games that refuse child
::             windows.
:: =====================================================================
set "VIDEO_WINDOW_TYPE=child"

:: =====================================================================
:: Video overlay: video audio output type
::   sdl_mixer = the video's audio is played through SDL_Mixer - the audio
::               library bundled inside pygame-ce, used via pygame.mixer.
::               SDL_Mixer streams the audio to the sound card from its own
::               thread with its own buffering, so there is no polling, CPU
::               usage is lower, and it generally sounds tighter and more
::               reliable than the legacy API below. This is the recommended
::               setting and the default. If SDL_Mixer is ever unavailable
::               (e.g. no audio device), the player automatically falls back
::               to WinMM waveOut.
::   winmm     = the video's audio is played through the legacy Windows
::               WinMM waveOut API - the old, low-level sound output the
::               player originally used. It is kept as an option for the
::               rare machine/driver combination where SDL_Mixer misbehaves
::               (stutter, no sound, etc.). If you ever suspect the audio
::               path is the problem, switch this flag to "winmm" and
::               compare the two.
:: =====================================================================
set "VIDEO_AUDIO_TYPE=sdl_mixer"

:: =====================================================================
:: Video overlay: video DECODER type
::   pyav   = h264/h265 cutscene videos are decoded IN-PROCESS by the Python
::            PyAV library (it bundles its own FFmpeg libraries and loads them
::            inside the overlay process itself). No external ffmpeg.exe has
::            to be spawned or pre-warmed when a video starts, eliminating
::            first-video playback delays caused by ffmpeg still loading.
::            This is the default and recommended setting. If PyAV is not
::            installed, playback automatically falls back to ffmpeg.
::   ffmpeg = classic behavior: every playback spawns ffmpeg.exe decode
::            pipes, and one tiny ffmpeg job is started asynchronously at
::            startup purely to warm up Windows' exe/DLL cache. Choose this
::            only if you want the exact previous pipeline (e.g. to compare
::            against PyAV, or if a codec fails to decode with PyAV).
:: =====================================================================
set "VIDEO_PLAYER_TYPE=pyav"

:: =====================================================================
:: Video overlay: interrupt hold (milliseconds) - fixes the black flash when
:: the video is skipped (like ESC). When the game closes the dummy video
:: it stops its own playback and redraws the menu UNDER the overlay window;
:: hiding the overlay window immediately would expose the game's
:: momentarily-black video surface (a quick black flash before the menu).
:: The player keeps the last frame visible for this many milliseconds so
:: the hide reveals the already-drawn menu instead.
::   50  = default, covers the game's menu redraw on this machine
::   raise it (e.g. 150) if a brief black flash still appears after skip
::   lower it (e.g. 35) if the video lingers too long after skip
:: =====================================================================
set "OVERLAY_INTERRUPT_HOLD_MS=50"

:: =====================================================================
:: VIRTUAL-VIDEO MODE - WinFsp/FUSE video serving
::   true  = the game's video folders are JUNCTIONED into the mounted
::           virtual drive, exactly like audio folders. The VFS hides
::           every matched .mp4/.txt re-encode set from listings, serves
::           the visible dummy videos itself, and reports dummy
::           open/close to the overlay watcher through
::           "_vfs_cache_\video_events.jsonl". Detection runs fully in
::           the VFS callbacks; no game handle-table polling happens at
::           all.
::   false = dummy videos are hard-linked as REAL files into the game
::           dir and the overlay detects opens by polling the game
::           process's handle table (DummyWatch).
:: =====================================================================
set "VIRTUAL_VIDEO_MODE=true"

:: =====================================================================
:: VFS diagnostics mode (troubleshooting tool)
::   false = normal operation (default): the virtual drive is served by
::           vfs_handler.py, which decodes audio on demand into memory.
::           Video handling follows VIRTUAL_VIDEO_MODE above - junctioned
::           dummy files served through the drive, or real hard links.
::   true  = diagnostic mode: the virtual drive is served by
::           vfs_diagnostics.py instead, which behaves identically but
::           additionally logs every file operation the game makes
::           (opens, reads, seeks, closes) to
::           _vfs_cache_\vfs_diagnostics.log. Use this ONLY when debugging
::           playback issues - it adds overhead, so keep it false for
::           normal play.
:: =====================================================================
set "VFS_VIDEO_DIAGNOSTICS=false"


:: =====================================================================
:: Change terminal code page to allow UTF-8 and setup colors
:: =====================================================================

REM Change Code Page to UTF8 (Windows), to properly display box drawing.
chcp 65001 > nul

REM Create ANSI Escape Sequence characters and associate them with %ESC% variable
call :SetESC
REM Create color variables
call :SetColors

echo %WHITE_MAGENTA%╔════════════════════════════════════════════════════════════════╗%ENDSEQ%
echo %WHITE_MAGENTA%║              Resonance Core: Virtual File System               ║%ENDSEQ%
echo %WHITE_MAGENTA%╠════════════════════════════════════════════════════════════════╣%ENDSEQ%
echo %WHITE_MAGENTA%║ This launcher automates the virtual file system setup for the  ║%ENDSEQ%
echo %WHITE_MAGENTA%║ game and performs the following tasks:                         ║%ENDSEQ%
echo %WHITE_MAGENTA%║                                                                ║%ENDSEQ%
echo %WHITE_MAGENTA%║ - Verifies all required dependencies before launching.         ║%ENDSEQ%
echo %WHITE_MAGENTA%║ - Mounts a virtual drive for "_vfs_data_" contents.            ║%ENDSEQ%
echo %WHITE_MAGENTA%║ - Indexes the contents of "_vfs_data_".                        ║%ENDSEQ%
echo %WHITE_MAGENTA%║ - Mirror "_vfs_data_" contents to the game directory.          ║%ENDSEQ%
echo %WHITE_MAGENTA%║   Mixing them if needed. We use symbolic links / Junctions.    ║%ENDSEQ%
echo %WHITE_MAGENTA%║ - Decodes .mp3 and .ogg files on demand into wave format in    ║%ENDSEQ%
echo %WHITE_MAGENTA%║   memory, so decoded audio is never written to storage.        ║%ENDSEQ%
echo %WHITE_MAGENTA%║ - Frees cached audio buffers as soon as files are closed.      ║%ENDSEQ%
echo %WHITE_MAGENTA%║ - Cleans up the mounted drive, junctions, and memory when the  ║%ENDSEQ%
echo %WHITE_MAGENTA%║   game exits.                                                  ║%ENDSEQ%
echo %WHITE_MAGENTA%║                                                                ║%ENDSEQ%
echo %WHITE_MAGENTA%║ NOTE: "_vfs_data_" mirrors the game directory's folder         ║%ENDSEQ%
echo %WHITE_MAGENTA%║ layout. Though intended primarily for audio and video. Any     ║%ENDSEQ%
echo %WHITE_MAGENTA%║ other data it contains is mirrored to the virtual drive as     ║%ENDSEQ%
echo %WHITE_MAGENTA%║ well. They will simply passthrough without decoding.           ║%ENDSEQ%
echo %WHITE_MAGENTA%║                                                                ║%ENDSEQ%
echo %WHITE_MAGENTA%║ NOTE: This launcher requires Python, WinFSP and ffmpeg. And    ║%ENDSEQ%
echo %WHITE_MAGENTA%║ will prompt to install them if they are not already present.   ║%ENDSEQ%
echo %WHITE_MAGENTA%║                                                                ║%ENDSEQ%
echo %WHITE_MAGENTA%║ FFmpeg is needed for encoding audio and video files to         ║%ENDSEQ%
echo %WHITE_MAGENTA%║ compress them using tool scripts. Also, to probe video files   ║%ENDSEQ%
echo %WHITE_MAGENTA%║ during runtime to get proper inormation for video playback     ║%ENDSEQ%
echo %WHITE_MAGENTA%║ using ffprobe.                                                 ║%ENDSEQ%
echo %WHITE_MAGENTA%╚════════════════════════════════════════════════════════════════╝%ENDSEQ%

:: =====================================================================
:: CONTEXT PRESERVATION (Fixes working directory when Running as Admin)
:: =====================================================================
cd /d "%~dp0"
set "GAME_DIR=%~dp0"
if "%GAME_DIR:~-1%"=="\" set "GAME_DIR=%GAME_DIR:~0,-1%"

:: =====================================================================
:: SELECT A FREE VIRTUAL DRIVE LETTER (prefer X:, else first unused)
:: A drive is considered used only if it actually exists (if not exist).
:: The registry (MountedDevices) is NOT used because it keeps stale
:: entries for drives that are no longer present.
:: =====================================================================
set "VFS_DRIVE="
for %%D in (X Y Z W V U T S R Q P O N M L K J I H G F E D) do (
    if not exist "%%D:\" if not defined VFS_DRIVE set "VFS_DRIVE=%%D:"
)
if not defined VFS_DRIVE (
    echo.
    echo %RED%%BOLD%[ERROR]%ENDSEQ% %RED%No free drive letter could be found to mount the virtual audio drive.%ENDSEQ%
    echo %RED%Free a drive letter, close any programs using virtual drives, and re-run.%ENDSEQ%
    echo.
    pause
    exit /b 1
)

:: =====================================================================
echo %YELLOW%➡️ Selected virtual drive letter: %GREEN%%VFS_DRIVE%%ENDSEQ%
:: =====================================================================
set "VFS_VOLUME_LABEL=WaveAudio"
if /i "%VFS_VIDEO_DIAGNOSTICS%"=="true" (
    set "PYTHON_VFS_SCRIPT=%GAME_DIR%\_vfs_scripts_\python\vfs_diagnostics.py"
    echo %YELLOW%➡️ VFS diagnostics mode ACTIVE - logging file operations to %CYAN%_vfs_cache_\vfs_diagnostics.log%ENDSEQ%
) else (
    set "PYTHON_VFS_SCRIPT=%GAME_DIR%\_vfs_scripts_\python\vfs_handler.py"
)
set "VFS_DATA_DIR=%GAME_DIR%\_vfs_data_"
set "VFS_CACHE_DIR=%GAME_DIR%\_vfs_cache_"
set "VFS_STATUS_FILE=%VFS_CACHE_DIR%\vfs_startup.status"
:: The timeout applies only when no indexing status update is received.
:: Total startup time is intentionally unlimited while progress continues.
set /a VFS_IDLE_TIMEOUT_SECONDS=300

:: =====================================================================
echo %YELLOW%➡️ Locating system Python...%ENDSEQ%
:: =====================================================================
set "PYTHON_PATH="
for /f "delims=" %%A in ('where python 2^>nul') do (
    set "CURRENT_PATH=%%A"
    echo !CURRENT_PATH! | findstr /I "WindowsApps" >nul
    if errorlevel 1 (
        if not defined PYTHON_PATH set "PYTHON_PATH=!CURRENT_PATH!"
    )
)
if defined PYTHON_PATH (
    "%PYTHON_PATH%" --version >nul 2>&1 || set "PYTHON_PATH="
)

if not defined PYTHON_PATH (
    for /f "delims=" %%A in ('py -3 -c "import sys;sys.stdout.write(sys.executable)" 2^>nul') do set "PYTHON_PATH=%%A"
)
if defined PYTHON_PATH (
    echo !PYTHON_PATH! | findstr /I "WindowsApps" >nul && set "PYTHON_PATH="
)

if not defined PYTHON_PATH call :InstallPython

if not defined PYTHON_PATH (
    echo.
    echo %RED%%BOLD%[ERROR]%ENDSEQ% %RED%A working Python was not found.%ENDSEQ%
    echo %RED%This launcher requires Python to mount the virtual audio drive.%ENDSEQ%
    echo.
    pause
    exit /b 1
)

:: =====================================================================
echo %YELLOW%➡️ Verifying virtual drive dependencies...%ENDSEQ%
:: =====================================================================
"%PYTHON_PATH%" -m pip install fusepy audioop-lts miniaudio pygame-ce --quiet
:: pygame-ce is the maintained pygame fork; the video overlay player needs it.
if errorlevel 1 (
    echo %YELLOW%Warning: pygame-ce install failed - video overlay player will be disabled.%ENDSEQ%
)

:: PyAV decoder (VIDEO_PLAYER_TYPE=pyav): in-process h264/h265 decoding for
:: the video overlay - no ffmpeg.exe spawn or warm-up on a video's critical
:: path. Only installed when that mode is selected; otherwise nothing changes.
if /i "%VIDEO_PLAYER_TYPE%"=="pyav" (
    "%PYTHON_PATH%" -m pip install av --quiet
    if errorlevel 1 (
        echo %YELLOW%Warning: PyAV install failed - videos will fall back to ffmpeg decoding.%ENDSEQ%
    )
)

:: =====================================================================
echo %YELLOW%➡️ Verifying WinFSP is installed...%ENDSEQ%
:: =====================================================================
set "WINFSP_INSTALLED=0"
set "WINFSP_DIR="
for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\WinFsp" /v InstallDir 2^>nul') do if not "%%B"=="" set "WINFSP_DIR=%%B"
if not defined WINFSP_DIR for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\WinFsp" /v InstallDir 2^>nul') do if not "%%B"=="" set "WINFSP_DIR=%%B"
if not defined WINFSP_DIR (
    if exist "C:\Program Files (x86)\WinFsp\bin\WinFSP-x64.dll" set "WINFSP_DIR=C:\Program Files (x86)\WinFsp\"
    if exist "C:\Program Files\WinFsp\bin\WinFSP-x64.dll" set "WINFSP_DIR=C:\Program Files\WinFsp\"
)
if defined WINFSP_DIR if exist "%WINFSP_DIR%bin\WinFSP-x64.dll" set "WINFSP_INSTALLED=1"
if not "%WINFSP_INSTALLED%"=="1" call :InstallWinFsp

if not "%WINFSP_INSTALLED%"=="1" (
    echo.
    echo %RED%%BOLD%[ERROR]%ENDSEQ% %RED%WinFSP is required to mount the virtual audio drive.%ENDSEQ%
    echo %RED%Please install it from the "%GAME_DIR%\VFS_Dependencies" folder and re-run.%ENDSEQ%
    echo.
    pause
    exit /b 1
)

:: =====================================================================
:: VERIFY FFMPEG TOOLS (ffmpeg / ffprobe / ffplay) - used by the video
:: overlay and by the re-encode/convert tools.
:: =====================================================================
echo %YELLOW%➡️ Verifying FFmpeg tools: ffmpeg, ffprobe, ffplay...%ENDSEQ%
:: =====================================================================
set "WINGET_LINKS=%LOCALAPPDATA%\Microsoft\WinGet\Links"
set "FFMPEG_OK=0"
where ffmpeg  >nul 2>&1 && where ffprobe >nul 2>&1 && where ffplay >nul 2>&1 && set "FFMPEG_OK=1"
if not "%FFMPEG_OK%"=="1" if exist "%WINGET_LINKS%\ffmpeg.exe" if exist "%WINGET_LINKS%\ffprobe.exe" if exist "%WINGET_LINKS%\ffplay.exe" set "FFMPEG_OK=1"
if not "%FFMPEG_OK%"=="1" (
    echo %RED%%BOLD%[MISSING DEPENDENCY]%ENDSEQ% %YELLOW%FFmpeg tools are not installed: ffmpeg, ffprobe, ffplay.%ENDSEQ%
    choice /C YN /N /M "%YELLOW%Install FFmpeg now via winget (Y/N)? %ENDSEQ%"
    if not errorlevel 2 (
        echo %CYAN%Installing FFmpeg via winget, please wait...%ENDSEQ%
        winget install --id BtbN.FFmpeg.GPL.8.1 -e --accept-source-agreements --accept-package-agreements
        if not errorlevel 1 set "FFMPEG_JUST_INSTALLED=1"
    ) else (
        echo %YELLOW%Skipping FFmpeg installation.%ENDSEQ%
    )
)

:: A freshly installed FFmpeg is usable right away through the WinGet Links
:: shims (%LOCALAPPDATA%\Microsoft\WinGet\Links), so no session restart or
:: PATH changes are needed - continue with the launcher normally.
if defined FFMPEG_JUST_INSTALLED echo %WHITE_GREEN%%BOLD%✅ FFmpeg installed. Continuing...%ENDSEQ%

:: =====================================================================
echo %YELLOW%➡️ Force cleaning old lingering processes and stale folder junctions...%ENDSEQ%
:: =====================================================================
taskkill /f /im python.exe >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1

:: Clean up old runtime tracking junctions safely before starting
if exist "%VFS_DATA_DIR%" (
    cd /d "%VFS_DATA_DIR%"
    for /r %%D in (.) do (
        set "FULL_PATH=%%~fD"
        set "REL_PATH=!FULL_PATH:%VFS_DATA_DIR%\=!"
        if not "!REL_PATH!"=="." if not "!REL_PATH!"=="" (
            if exist "%GAME_DIR%\!REL_PATH!" (
                set "REPARSE_ATTR="
                for %%A in ("%GAME_DIR%\!REL_PATH!") do set "REPARSE_ATTR=%%~aA"
                if not "!REPARSE_ATTR:l=!"=="!REPARSE_ATTR!" rmdir "%GAME_DIR%\!REL_PATH!" >nul 2>&1
            )
        )
    )
)

:: =====================================================================
:: Unmount old virtual drive letter cleanly
:: =====================================================================
cd /d "%GAME_DIR%"
mountvol %VFS_DRIVE%\ /d >nul 2>&1

if not exist "%VFS_CACHE_DIR%" mkdir "%VFS_CACHE_DIR%" >nul 2>&1
if exist "%VFS_STATUS_FILE%" del /f /q "%VFS_STATUS_FILE%" >nul 2>&1
REM Virtual-video mode: remove the previous run's dummy open/close event log
REM so the overlay's tailer only ever reads events from THIS run.
if exist "%VFS_CACHE_DIR%\video_events.jsonl" del /f /q "%VFS_CACHE_DIR%\video_events.jsonl" >nul 2>&1
>"%VFS_STATUS_FILE%" (
    echo state=starting
    echo stage=launcher
    echo sequence=0
    echo completed=0
    echo total=0
    echo updated=0
    echo message=Starting VFS service
)

echo.
echo %WHITE_GREEN%╔════════════════════════════════════════════════════════════════╗%ENDSEQ%
echo %WHITE_GREEN%║ MOUNTING VIRTUAL TRANSLATION INTERFACE                         ║%ENDSEQ%
echo %WHITE_GREEN%╚════════════════════════════════════════════════════════════════╝%ENDSEQ%
REM Virtual-video mode feeds open/close events (and mp4/txt hiding) to the
REM VFS server itself; the same flag is given to the overlay further below.
set "VFS_VVS_ARGS="
if "!VIRTUAL_VIDEO_MODE!"=="true" set "VFS_VVS_ARGS=--video-events "%VFS_CACHE_DIR%\video_events.jsonl""

:: ---------------------------------------------------------------------
:: BACKING-STORE PRE-PASS (virtual-video mode)
:: A video folder whose REAL counterpart already exists in the game dir
:: cannot be junctioned over directly - a junction replaces the whole
:: folder view. To keep the original game data safe AND still serve the
:: dummies through the drive: rename the real folder aside to
:: "<name>.vfsbak" (same volume, nothing is deleted), let the VFS merge
:: its contents in behind the dummies (--backing), and put the junction
:: on top. On exit the junction is removed and the original name is
:: restored. If BOTH a real folder and a .vfsbak exist, something went
:: wrong earlier - abort and ask for manual resolution instead of
:: guessing which copy is current.
:: ---------------------------------------------------------------------
set "VFS_BACKING_ARGS="
if "!VIRTUAL_VIDEO_MODE!"=="true" (
    cd /d "%VFS_DATA_DIR%"
    for /d %%D in ("*") do (
        set "FULL_PATH=%%~fD"
        set "REL_PATH=!FULL_PATH:%VFS_DATA_DIR%\=!"
        if exist "%%~fD\*.mp4" (
            if exist "%GAME_DIR%\!REL_PATH!" (
                REM Reparse-point test via attributes ("l" flag). Listing a
                REM junction path shows THROUGH it, so dir+find would never
                REM see the <JUNCTION> tag; %%~aA is exact and block-safe.
                set "REPARSE_ATTR="
                for %%A in ("%GAME_DIR%\!REL_PATH!") do set "REPARSE_ATTR=%%~aA"
                if not "!REPARSE_ATTR:l=!"=="!REPARSE_ATTR!" (
                    REM Junction survived from an interrupted run - if a backup
                    REM exists it holds the originals and must stay merged.
                    if exist "%GAME_DIR%\!REL_PATH!.vfsbak" (
                        set VFS_BACKING_ARGS=!VFS_BACKING_ARGS! "--backing" "!REL_PATH!=%GAME_DIR%\!REL_PATH!.vfsbak"
                    )
                ) else (
                    if exist "%GAME_DIR%\!REL_PATH!.vfsbak" (
                        echo %RED%%BOLD%[VVS MERGE CONFLICT]:%ENDSEQ% %RED%Both "%GAME_DIR%\!REL_PATH!" and its .vfsbak backup exist.%ENDSEQ%
                        echo %RED%Resolve this manually - keep one copy - then relaunch. Nothing was modified now.%ENDSEQ%
                        pause
                        exit /b 1
                    )
                    move "%GAME_DIR%\!REL_PATH!" "%GAME_DIR%\!REL_PATH!.vfsbak" >nul
                    echo %YELLOW_BLUE%%BOLD%[VVS MERGE]:%ENDSEQ% %YELLOW%Original folder kept safe as "!REL_PATH!.vfsbak" - VFS will merge it behind the dummies.%ENDSEQ%
                    set VFS_BACKING_ARGS=!VFS_BACKING_ARGS! "--backing" "!REL_PATH!=%GAME_DIR%\!REL_PATH!.vfsbak"
                )
            ) else (
                REM Folder missing but a backup remains: previous run was
                REM interrupted before restore - re-arm the merge.
                if exist "%GAME_DIR%\!REL_PATH!.vfsbak" (
                    set VFS_BACKING_ARGS=!VFS_BACKING_ARGS! "--backing" "!REL_PATH!=%GAME_DIR%\!REL_PATH!.vfsbak"
                )
            )
        )
    )
)

:: Latency/content-sync tracing: the VFS, the overlay and the player all
:: append epoch-stamped lines here (VFS inherits this env var), correlating
:: 1:1 with video_events.jsonl "time" values. Only written on video activity
:: when VFS diagnostics mode is enabled.
set "PLAYER_DEBUG="
if /i "%VFS_VIDEO_DIAGNOSTICS%"=="true" set "PLAYER_DEBUG=%VFS_CACHE_DIR%\overlay_debug.log"
if exist "%VFS_CACHE_DIR%\overlay_debug.log" del /f /q "%VFS_CACHE_DIR%\overlay_debug.log" >nul 2>&1
start /b "PythonVFS" "%PYTHON_PATH%" "%PYTHON_VFS_SCRIPT%" --source "%VFS_DATA_DIR%" --mount "%VFS_DRIVE%" --label "%VFS_VOLUME_LABEL%" --cache "%VFS_CACHE_DIR%\vfs_index_cache.json" --status "%VFS_STATUS_FILE%" !VFS_VVS_ARGS!!VFS_BACKING_ARGS! 2>nul

:: =====================================================================
echo %YELLOW%%BLINKING%➡️ The service python has been started.%ENDSEQ%
echo %YELLOW%➡️ Waiting for virtual drive %GREEN%%VFS_DRIVE%%ENDSEQ%%YELLOW% to initialize...%ENDSEQ%
:: =====================================================================
set /a idle_count=0
set /a VFS_IDLE_TIMEOUT_TICKS=(VFS_IDLE_TIMEOUT_SECONDS+1)/2
set "last_status_sequence=0"
set "last_status_stage="
set "vfs_status_color_toggle=0"

:: =====================================================================
:CHECK_VFS
:: =====================================================================
timeout /t 2 /nobreak > nul
set "current_status_sequence="
for /f "tokens=2 delims==" %%A in ('findstr /b "sequence=" "%VFS_STATUS_FILE%" 2^>nul') do set "current_status_sequence=%%A"

if defined current_status_sequence if not "!current_status_sequence!"=="!last_status_sequence!" (
    set /a idle_count=0
    set "last_status_sequence=!current_status_sequence!"
    set "current_status_stage="
    set "current_status_completed="
    set "current_status_total="
    set "current_status_message="
    for /f "tokens=2 delims==" %%A in ('findstr /b "stage=" "%VFS_STATUS_FILE%" 2^>nul') do set "current_status_stage=%%A"
    for /f "tokens=2 delims==" %%A in ('findstr /b "completed=" "%VFS_STATUS_FILE%" 2^>nul') do set "current_status_completed=%%A"
    for /f "tokens=2 delims==" %%A in ('findstr /b "total=" "%VFS_STATUS_FILE%" 2^>nul') do set "current_status_total=%%A"
    for /f "tokens=2,* delims==" %%A in ('findstr /b "message=" "%VFS_STATUS_FILE%" 2^>nul') do set "current_status_message=%%B"
    rem The Python VFS owns the live progress line. The launcher only uses
    rem this status sequence to reset the inactivity timeout.
) else (
    set /a idle_count+=1
)

set "current_vfs_state="
for /f "tokens=2 delims==" %%A in ('findstr /b "state=" "%VFS_STATUS_FILE%" 2^>nul') do set "current_vfs_state=%%A"
if /i "!current_vfs_state!"=="failed" (
    set "current_vfs_message="
    for /f "tokens=2,* delims==" %%A in ('findstr /b "message=" "%VFS_STATUS_FILE%" 2^>nul') do set "current_vfs_message=%%B"
    echo %RED%%BOLD%[CRITICAL ERROR]%ENDSEQ% %RED%VFS startup failed: !current_vfs_message!%ENDSEQ%
    echo %RED%See "%VFS_STATUS_FILE%" and the Python console/log for details.%ENDSEQ%
    pause
    exit /b 1
)

if exist "%VFS_DRIVE%\" (
    goto MOUNT_SUCCESS
)

if !idle_count! geq !VFS_IDLE_TIMEOUT_TICKS! (
    echo %RED%%BOLD%[CRITICAL ERROR]%ENDSEQ% %RED%The VFS made no progress for %VFS_IDLE_TIMEOUT_SECONDS% seconds.%ENDSEQ%
    echo %RED%It may be stuck on a file or unable to mount drive %VFS_DRIVE%.%ENDSEQ%
    echo %RED%Status: "%VFS_STATUS_FILE%"%ENDSEQ%
    pause
    exit /b 1
)
goto CHECK_VFS

:: =====================================================================
:MOUNT_SUCCESS
:: =====================================================================
echo %WHITE_GREEN%%BOLD%[SUCCESS]%ENDSEQ% %WHITE%Virtual drive %GREEN%%VFS_DRIVE%%ENDSEQ%%WHITE% mounted successfully.%ENDSEQ%

echo.
echo %YELLOW%➡️ Dynamically mapping folder junctions from %CYAN%".\_vfs_data_"%ENDSEQ%%YELLOW%...%ENDSEQ%
cd /d "%VFS_DATA_DIR%"

:: Maps all directories sequentially to guarantee mid-level files mirror correctly
for /r %%D in (.) do (
    set "FULL_PATH=%%~fD"
    set "REL_PATH=!FULL_PATH:%VFS_DATA_DIR%\=!"

    if not "!REL_PATH!"=="." if not "!REL_PATH!"=="" (
        set "HAS_AUDIO=0"
        if exist "%%~fD\*.mp3" set "HAS_AUDIO=1"
        if exist "%%~fD\*.ogg" set "HAS_AUDIO=1"
        set "HAS_VIDEO=0"
        if exist "%%~fD\*.mp4" set "HAS_VIDEO=1"

        REM Process folders that contain audio tracks natively via virtual drive links
        if "!HAS_AUDIO!"=="1" (
            echo %BLUE_YELLOW%Target Audio Folder Found:%ENDSEQ% %CYAN%.\_vfs_data_\!REL_PATH!%ENDSEQ%

            if exist "%GAME_DIR%\!REL_PATH!" (
                set "REPARSE_ATTR="
                for %%A in ("%GAME_DIR%\!REL_PATH!") do set "REPARSE_ATTR=%%~aA"
                if not "!REPARSE_ATTR:l=!"=="!REPARSE_ATTR!" (
                    rmdir "%GAME_DIR%\!REL_PATH!" >nul 2>&1
                    echo %WHITE%%BOLD%➡️ Junction created for %CYAN%.\!REL_PATH!%ENDSEQ%%WHITE%%BOLD% %BLINKING%◀════▶%ENDSEQ%%WHITE%%BOLD% %CYAN%%VFS_DRIVE%\!REL_PATH!%ENDSEQ%
                    mklink /J "%GAME_DIR%\!REL_PATH!" "%VFS_DRIVE%\!REL_PATH!" >nul
                ) else (
                    echo %YELLOW_BLUE%%BOLD%[MIXING SYSTEM]:%ENDSEQ% %YELLOW%Real folder items exist. Merging files instead...%ENDSEQ%
                    for %%F in ("%%~fD\*") do (
                        set "FILE_NAME=%%~nxF"
                        if not "%%~xF"==".mp3" if not "%%~xF"==".ogg" (
                            if not exist "%GAME_DIR%\!REL_PATH!\!FILE_NAME!" (
                                mklink /H "%GAME_DIR%\!REL_PATH!\!FILE_NAME!" "%%~fF" >nul 2>&1
                            )
                        )
                    )
                )
            ) else (
                echo %WHITE%%BOLD%➡️ Junction created for %CYAN%.\!REL_PATH!%ENDSEQ%%WHITE%%BOLD% %BLINKING%◀════▶%ENDSEQ%%WHITE%%BOLD% %CYAN%%VFS_DRIVE%\!REL_PATH!%ENDSEQ%
                mklink /J "%GAME_DIR%\!REL_PATH!" "%VFS_DRIVE%\!REL_PATH!" >nul
            )
        ) else if "!HAS_VIDEO!"=="1" (
            if "!VIRTUAL_VIDEO_MODE!"=="true" (
                REM ---------------------------------------------------------
                REM VIRTUAL-VIDEO MODE: the entire video folder becomes a
                REM junction into the mounted virtual drive - exactly like
                REM audio folders. The VFS hides every matched .mp4/.txt
                REM re-encode set and serves each visible dummy video itself;
                REM open/close events reach the overlay through
                REM _vfs_cache_\video_events.jsonl - no handle polling.
                REM ---------------------------------------------------------
                echo %BLUE_YELLOW%Target Video Folder Found:%ENDSEQ% %CYAN%.\_vfs_data_\!REL_PATH!%ENDSEQ%
                echo %WHITE%%BOLD%- Junctioning video folder to virtual drive %CYAN%%VFS_DRIVE%\!REL_PATH!%ENDSEQ%
                if exist "%GAME_DIR%\!REL_PATH!" (
                    set "REPARSE_ATTR="
                    for %%A in ("%GAME_DIR%\!REL_PATH!") do set "REPARSE_ATTR=%%~aA"
                    if not "!REPARSE_ATTR:l=!"=="!REPARSE_ATTR!" (
                        rmdir "%GAME_DIR%\!REL_PATH!" >nul 2>&1
                        mklink /J "%GAME_DIR%\!REL_PATH!" "%VFS_DRIVE%\!REL_PATH!" >nul
                    ) else (
                        echo %YELLOW_BLUE%%BOLD%[VVS FALLBACK]:%ENDSEQ% %YELLOW%Real folder already exists in game dir - hard-linking dummies instead.%ENDSEQ%
                        for %%F in ("%%~fD\*") do (
                            set "FILE_NAME=%%~nxF"
                            if not "%%~xF"==".mp4" if not "%%~xF"==".txt" (
                                if not exist "%GAME_DIR%\!REL_PATH!\!FILE_NAME!" (
                                    mklink /H "%GAME_DIR%\!REL_PATH!\!FILE_NAME!" "%%~fF" >nul 2>&1
                                )
                            )
                        )
                    )
                ) else (
                    mklink /J "%GAME_DIR%\!REL_PATH!" "%VFS_DRIVE%\!REL_PATH!" >nul
                )
            ) else (
            REM Folders that contain re-encoded videos: the dummy video files
            REM the game opens are hard-linked as REAL files into the game dir,
            REM so playback never depends on files served through the drive.
            REM The overlay detects the game opening them by polling who holds
            REM a handle to each file. EVERY file in the folder is linked
            REM as-is EXCEPT the re-encoded .mp4 and the sidecar .txt, so
            REM dummies of ANY original format (.avi, .bik, .mve, .wmv, ...)
            REM work; the .mp4 and .txt stay in %VFS_DATA_DIR% and are never
            REM exposed to the game.
            echo %BLUE_YELLOW%Target Video Folder Found:%ENDSEQ% %CYAN%.\_vfs_data_\!REL_PATH!%ENDSEQ%
            echo %WHITE%%BOLD%➡️ Dummy videos hard-linked into %CYAN%.\!REL_PATH!%ENDSEQ%%WHITE%%BOLD% - the .mp4/.txt stay virtual on %CYAN%%VFS_DRIVE%\!REL_PATH!%ENDSEQ%

            if exist "%GAME_DIR%\!REL_PATH!" (
                set "REPARSE_ATTR="
                for %%A in ("%GAME_DIR%\!REL_PATH!") do set "REPARSE_ATTR=%%~aA"
                if not "!REPARSE_ATTR:l=!"=="!REPARSE_ATTR!" (
                    rmdir "%GAME_DIR%\!REL_PATH!" >nul 2>&1
                    mkdir "%GAME_DIR%\!REL_PATH!" >nul 2>&1
                )
            ) else (
                mkdir "%GAME_DIR%\!REL_PATH!" >nul 2>&1
            )
            for %%F in ("%%~fD\*") do (
                set "FILE_NAME=%%~nxF"
                if not "%%~xF"==".mp4" if not "%%~xF"==".txt" (
                    if not exist "%GAME_DIR%\!REL_PATH!\!FILE_NAME!" (
                        mklink /H "%GAME_DIR%\!REL_PATH!\!FILE_NAME!" "%%~fF" >nul 2>&1
                    )
                )
            )
            )
        ) else (
            REM Handle non-audio placeholder directories to safely mirror files like .MSB
            if exist "%GAME_DIR%\!REL_PATH!" (
                for %%F in ("%%~fD\*") do (
                    set "FILE_NAME=%%~nxF"
                    if not exist "%GAME_DIR%\!REL_PATH!\!FILE_NAME!" (
                        mklink /H "%GAME_DIR%\!REL_PATH!\!FILE_NAME!" "%%~fF" >nul 2>&1
                    )
                )
            ) else (
                REM If the path completely missing from game tree, create physical anchor framework first
                mkdir "%GAME_DIR%\!REL_PATH!" >nul 2>&1
                for %%F in ("%%~fD\*") do (
                    set "FILE_NAME=%%~nxF"
                    mklink /H "%GAME_DIR%\!REL_PATH!\!FILE_NAME!" "%%~fF" >nul 2>&1
                )
            )
        )
    )
)

echo.
if not exist "%GAME_DIR%\%GAME_EXE%" (
    echo %RED%%BOLD%[ERROR]%ENDSEQ% %RED%Game executable not found:%ENDSEQ%
    echo %RED%  "%GAME_DIR%\%GAME_EXE%"%ENDSEQ%
    echo.
    pause
    exit /b 1
)
echo %YELLOW_RED%%BOLD%%BLINKING%╔════════════════════════════════════════════════════════════════╗%ENDSEQ%
echo %YELLOW_RED%%BOLD%%BLINKING%║ Launching Game Environment Natively...                         ║%ENDSEQ%
echo %YELLOW_RED%%BOLD%%BLINKING%╚════════════════════════════════════════════════════════════════╝%ENDSEQ%

:: =====================================================================
:: Start the video overlay watcher (pythonw so it survives python.exe cleanup).
:: It plays the real MP4 on top of the dummy video the game renders.
:: --source is the WHOLE _vfs_data_ mirror root: the overlay scans it
::   recursively and pairs each MP4 with the dummy the game opens (same
::   basename, original extension from the sidecar .txt), so no game-specific
::   "VIDEO" folder name or ".avi" extension is hardcoded anywhere.
:: --process tells the watcher which game process to watch: the exe this
::   launcher starts (e.g. comandos_w10.exe), instead of a hardcoded name.
:: =====================================================================
set "OVERLAY_STARTED=0"
if exist "%GAME_DIR%\_vfs_scripts_\python\video_overlay.py" (
    set "PYTHONW_PATH="
    if defined PYTHON_PATH for %%F in ("%PYTHON_PATH%") do set "PYTHONW_PATH=%%~dpFpythonw.exe"
    if defined PYTHONW_PATH if exist "!PYTHONW_PATH!" (
        if exist "%VFS_CACHE_DIR%\video_overlay.ready" del /f /q "%VFS_CACHE_DIR%\video_overlay.ready" >nul 2>&1
        echo %YELLOW%➡️ Starting video overlay watcher...%ENDSEQ%
        REM Virtual-video mode passes the VFS's JSONL event log so the overlay
        REM detects dummy open/close from the VFS callbacks (VirtualVideoWatch)
        REM instead of polling the game's handle table (DummyWatch).
        if "!VIRTUAL_VIDEO_MODE!"=="true" (
            start /b "VideoOverlay" "!PYTHONW_PATH!" "%GAME_DIR%\_vfs_scripts_\python\video_overlay.py" --source "%VFS_DATA_DIR%" --game "%GAME_DIR%" --maintain-aspect "%MAINTAIN_ASPECT_RATIO%" --window-type "%VIDEO_WINDOW_TYPE%" --audio-type "%VIDEO_AUDIO_TYPE%" --player-type "%VIDEO_PLAYER_TYPE%" --process "%GAME_EXE%" --cache "%GAME_DIR%\_vfs_cache_\vfs_index_cache.json" --video-events "%VFS_CACHE_DIR%\video_events.jsonl" 2>nul
        ) else (
            start /b "VideoOverlay" "!PYTHONW_PATH!" "%GAME_DIR%\_vfs_scripts_\python\video_overlay.py" --source "%VFS_DATA_DIR%" --game "%GAME_DIR%" --maintain-aspect "%MAINTAIN_ASPECT_RATIO%" --window-type "%VIDEO_WINDOW_TYPE%" --audio-type "%VIDEO_AUDIO_TYPE%" --player-type "%VIDEO_PLAYER_TYPE%" --process "%GAME_EXE%" --cache "%GAME_DIR%\_vfs_cache_\vfs_index_cache.json" 2>nul
        )
        set "OVERLAY_STARTED=1"
    ) else (
        echo %YELLOW%➡️ pythonw not found - video overlay disabled.%ENDSEQ%
    )
)

:: =====================================================================
:: Wait for the video overlay to signal it is ready (poll loop up, display
:: pre-created) before launching the game, so the first dummy video is never
:: delayed by overlay startup - pythonw + pygame import + index build + the
:: pre-created window can take several seconds on a cold start. Best-effort:
:: if it never signals (e.g. pythonw failed to start), give up after 120s
:: and launch the game anyway - the videos are then just delayed, not missing.
:: =====================================================================
if not "%OVERLAY_STARTED%"=="1" goto SKIP_OVERLAY_WAIT
echo %YELLOW%➡️ Waiting for the video overlay to be ready...%ENDSEQ%
set /a OVERLAY_WAIT_TICKS=0
:WAIT_OVERLAY
if exist "%VFS_CACHE_DIR%\video_overlay.ready" goto OVERLAY_READY
timeout /t 1 /nobreak > nul
set /a OVERLAY_WAIT_TICKS+=1
if !OVERLAY_WAIT_TICKS! geq 120 (
    echo %RED%%BOLD%[WARNING]%ENDSEQ% %YELLOW%Video overlay did not signal ready in 120s - starting the game anyway, videos may be delayed.%ENDSEQ%
    goto OVERLAY_READY
)
goto WAIT_OVERLAY
:OVERLAY_READY
echo %BLINKING%%WHITE%✅ Video overlay ready.%ENDSEQ%
echo.
:SKIP_OVERLAY_WAIT

:: =====================================================================
cd /d "%GAME_DIR%"
start "" /wait "%GAME_DIR%\%GAME_EXE%"
:: =====================================================================

echo.
echo %YELLOW%➡️ Game closed. Detaching folder junctions and clearing temporary runtime files...%ENDSEQ%
if exist "%VFS_DATA_DIR%" (
    cd /d "%VFS_DATA_DIR%"
    for /r %%D in (.) do (
        set "FULL_PATH=%%~fD"
        set "REL_PATH=!FULL_PATH:%VFS_DATA_DIR%\=!"
        if not "!REL_PATH!"=="." if not "!REL_PATH!"=="" (
            if exist "%GAME_DIR%\!REL_PATH!" (
                REM Reparse-point test via attributes ("l" flag) - see the
                REM BACKING-STORE PRE-PASS note: dir+find cannot detect a
                REM junction by listing it, %%~aA can.
                set "REPARSE_ATTR="
                for %%A in ("%GAME_DIR%\!REL_PATH!") do set "REPARSE_ATTR=%%~aA"
                if not "!REPARSE_ATTR:l=!"=="!REPARSE_ATTR!" (
                    rmdir "%GAME_DIR%\!REL_PATH!" >nul 2>&1
                ) else (
                    REM Safely remove temporary hard links for non-audio tracking files
                    for %%F in ("%%~fD\*") do (
                        set "FILE_NAME=%%~nxF"
                        if not "%%~xF"==".mp3" if not "%%~xF"==".ogg" (
                            if exist "%GAME_DIR%\!REL_PATH!\!FILE_NAME!" (
                                del /f /q "%GAME_DIR%\!REL_PATH!\!FILE_NAME!" >nul 2>&1
                            )
                        )
                    )
                )
            )
        )
    )
)

:: ---------------------------------------------------------------------
:: BACKING-STORE RESTORE PASS
:: Junctions are gone now; give every merged folder its original name
:: back. Only restores when the junction slot is actually free - if a
:: real folder somehow reappeared under the original name, leave both
:: copies in place and tell the user instead of overwriting anything.
:: ---------------------------------------------------------------------
if exist "%VFS_DATA_DIR%" (
    cd /d "%VFS_DATA_DIR%"
    for /d %%D in ("*") do (
        set "FULL_PATH=%%~fD"
        set "REL_PATH=!FULL_PATH:%VFS_DATA_DIR%\=!"
        if exist "%GAME_DIR%\!REL_PATH!.vfsbak" (
            if exist "%GAME_DIR%\!REL_PATH!" (
                echo %YELLOW%%BOLD%[VVS MERGE NOTICE]:%ENDSEQ% %YELLOW%Both "%REL_PATH!" and "%REL_PATH!.vfsbak" exist after cleanup - keeping the backup, please compare them manually.%ENDSEQ%
            ) else (
                move "%GAME_DIR%\!REL_PATH!.vfsbak" "%GAME_DIR%\!REL_PATH!" >nul && echo %GREEN%➡️ Restored original folder "!REL_PATH!" from backup.%ENDSEQ%
            )
        )
    )
)
taskkill /f /im python.exe >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1

:: Clean up the virtual drive mapping
mountvol %VFS_DRIVE%\ /d >nul 2>&1

:: =====================================================================
:: DEAD JUNCTION & EMPTY SKELETON TEARDOWN SYSTEM
:: =====================================================================
echo %YELLOW%➡️ Cleaning up unmapped virtual folder structures...%ENDSEQ%
if exist "%VFS_DATA_DIR%" (
    cd /d "%VFS_DATA_DIR%"
    for /r %%D in (.) do (
        set "FULL_PATH=%%~fD"
        set "REL_PATH=!FULL_PATH:%VFS_DATA_DIR%\=!"
        if not "!REL_PATH!"=="." if not "!REL_PATH!"=="" (
            if exist "%GAME_DIR%\!REL_PATH!" (
                REM Test if the junction is broken by trying to read its contents
                dir "%GAME_DIR%\!REL_PATH!" >nul 2>&1
                if errorlevel 1 (
                    rmdir "%GAME_DIR%\!REL_PATH!" >nul 2>&1
                )
            )
        )
    )

    :: BOTTOM-UP CLEANER: Re-scans paths and safely drops empty shell frameworks (dead links)
    set "DIR_LIST=%TEMP%\vfs_dir_cleanup.txt"
    if exist "!DIR_LIST!" del "!DIR_LIST!" >nul 2>&1

    for /r %%D in (.) do (
        set "FULL_PATH=%%~fD"
        set "REL_PATH=!FULL_PATH:%VFS_DATA_DIR%\=!"
        if not "!REL_PATH!"=="." if not "!REL_PATH!"=="" (
            if exist "%GAME_DIR%\!REL_PATH!" (
                echo !REL_PATH!>>"!DIR_LIST!"
            )
        )
    )

    if exist "!DIR_LIST!" (
        REM Process the folder trees completely in reverse order (deepest folders first)
        for /f "delims=" %%A in ('sort /r "!DIR_LIST!"') do (
            REM Safe rmdir: fails instantly if any real game file remains inside
            rmdir "%GAME_DIR%\%%A" >nul 2>&1
        )
        del "!DIR_LIST!" >nul 2>&1
    )
)

goto :Done

:: =====================================================================
:SetESC
:: =====================================================================
	REM Enable ANSI Escape sequences, to have colored output. Escape sequence preceded by %ESC%[Nm
	REM Note: #$H# is backscpace and #$E# is Escape from Ascii code.
	REM This function basically does the following: ' set "ESC=" ' But since inserting ESC
	REM character (27 ascii code) in editor is hard, Notepad++ is really easy, just ALT+027.
	for /F "tokens=1,2 delims=#" %%a in ('"prompt #$H#$E# & echo on & for %%b in (1) do rem"') do (
		set ESC=%%b
		exit /B 0
	)
exit /B 0

:: =====================================================================
:SetColors
:: =====================================================================
	REM -- Set color variables
	REM If only one color specified, then it is foreground. If two colors mentioned
	REM then the first is foreground and second is background. If BG is in color name,
	REM then it is background. Color numbers in 90s+ are bright, darker if lower than 90s.
	set WHITE=%ESC%[97m
	set CYAN=%ESC%[96m
	set YELLOW=%ESC%[93m
	set GREEN=%ESC%[92m
	set RED=%ESC%[91m
	set BLUE=%ESC%[94m
	set NORMAL_RED=%ESC%[31m
	set MAGENTA_BG=%ESC%[45m
	set YELLOW_BG=%ESC%[103m
	set BLUE_BG=%ESC%[44m
	set GREEN_BG=%ESC%[42m
	set RED_BG=%ESC%[41m
	REM white foreground, on magenta background
	set WHITE_MAGENTA=%WHITE%%MAGENTA_BG%
	REM normal red foreground on bright yellow background
	set RED_YELLOW=%NORMAL_RED%%YELLOW_BG%
	REM bright white foreground on red background
	set WHITE_RED=%WHITE%%RED_BG%
	REM yellow foreground, blue background
	set YELLOW_BLUE=%YELLOW%%BLUE_BG%
	REM yellow foreground, red background
	set YELLOW_RED=%YELLOW%%RED_BG%
        REM blue foreground, yellow background
        set BLUE_YELLOW=%BLUE%%YELLOW_BG%
	REM white foreground on green background
	set WHITE_GREEN=%WHITE%%GREEN_BG%
	REM switch (or inverse) foreground and background colors
	set SWITCH_FGBG=%ESC%[7m
	REM End Sequence to close the Escape Sequence
	set ENDSEQ=%ESC%[0m
	REM underline, not a color, I know
	set UNDERLINE=%ESC%[4m
	REM bold
	set BOLD=%ESC%[1m
	REM blinking
	set BLINKING=%ESC%[1;5m
	REM carriage return to the beginning of the line and erases the same line
	REM -- The code to go to first column and n lines up is ESC[nF
	REM ---- Moves cursor to beginning of the line n (default 1) lines up.
	REM -- The code to delete to the end of the line is ESC[0K
	REM ---- Erases part of the line. If n is 0 (or missing), clear from cursor to the end of the line.
	REM ---- Note that using 0J instead does the same thing, but it erases from cursor to end of screen.
	REM More details: https://en.wikipedia.org/wiki/ANSI_escape_code
	set LINE_RESET=%ESC%[1F%ESC%[0K
exit /B 0

:: =====================================================================
:InstallPython
:: =====================================================================
echo.
echo %RED%%BOLD%[MISSING DEPENDENCY]%ENDSEQ% %YELLOW%Python is not installed (or the Windows Store stub is unusable).%ENDSEQ%
echo %YELLOW%Python will be installed via winget (latest Python 3) and added to your PATH.%ENDSEQ%
echo.
choice /C YN /N /M "%YELLOW%Install Python now (Y/N)? %ENDSEQ%"
if errorlevel 2 (
    echo %YELLOW%Skipping Python installation.%ENDSEQ%
    exit /b 1
)
echo %CYAN%Installing Python, this may take a minute...%ENDSEQ%
winget install python3 --silent --accept-source-agreements --accept-package-agreements --override "/quiet PrependPath=1"
if errorlevel 1 (
    echo %RED%%BOLD%[ERROR]%ENDSEQ% %RED%Python installer failed or was cancelled.%ENDSEQ%
    exit /b 1
)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
    if exist "%%D\python.exe" set "PYTHON_PATH=%%D\python.exe"
)
exit /b 0

:: =====================================================================
:InstallWinFsp
:: =====================================================================
echo.
echo %RED%%BOLD%[MISSING DEPENDENCY]%ENDSEQ% %YELLOW%WinFSP is not installed.%ENDSEQ%
echo %YELLOW%WinFSP provides the virtual file system driver used to mount the audio drive.%ENDSEQ%
echo.
echo %BLINKING%%RED%%BOLD%   A Windows administrator (UAC) prompt will now appear.%ENDSEQ%
echo %BLINKING%%RED%%BOLD%   Please click "Yes" to allow WinFSP to install.%ENDSEQ%
echo.
choice /C YN /N /M "%YELLOW%Install WinFSP now (Y/N)? %ENDSEQ%"
if errorlevel 2 (
    echo %YELLOW%Skipping WinFSP installation.%ENDSEQ%
    exit /b 1
)
echo %CYAN%Installing WinFSP, please wait...%ENDSEQ%
winget install WinFsp.WinFsp
if errorlevel 1 (
    echo %RED%%BOLD%[ERROR]%ENDSEQ% %RED%WinFSP installation failed or was cancelled.%ENDSEQ%
    exit /b 1
)
set "WINFSP_INSTALLED=0"
for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\WinFsp" /v InstallDir 2^>nul') do if not "%%B"=="" set "WINFSP_DIR=%%B"
if defined WINFSP_DIR if exist "%WINFSP_DIR%bin\WinFSP-x64.dll" set "WINFSP_INSTALLED=1"
exit /b 0

:: =====================================================================
:Done
:: =====================================================================
echo.
echo %WHITE_GREEN%%BOLD%╔════════════════════════════════════════════════════════════════╗%ENDSEQ%
echo %WHITE_GREEN%%BOLD%║ Cleanup completed. Launcher finished and all is done.          ║%ENDSEQ%
echo %WHITE_GREEN%%BOLD%╚════════════════════════════════════════════════════════════════╝%ENDSEQ%
echo.

REM Change Code Page to default (Terminal, ANSI)
chcp 437 > nul

pause

endlocal
exit /b 0
