@echo off
setlocal enabledelayedexpansion
title WAV to OGG/MP3 Converter

:: =====================================================================
:: 1. CHECK FFMPEG - install it via winget if it is missing
::    (looks on the PATH first, then in the WinGet Links shims folder)
:: =====================================================================
set "WINGET_LINKS=%LOCALAPPDATA%\Microsoft\WinGet\Links"
set "FFMPEG="
set "FFPROBE="

where ffmpeg >nul 2>&1
if not errorlevel 1 (
    set "FFMPEG=ffmpeg"
    set "FFPROBE=ffprobe"
)
if not defined FFMPEG if exist "%WINGET_LINKS%\ffmpeg.exe" (
    set "FFMPEG=%WINGET_LINKS%\ffmpeg.exe"
    set "FFPROBE=%WINGET_LINKS%\ffprobe.exe"
)

if not defined FFMPEG (
    echo FFmpeg is not installed. Installing it via winget...
    winget install --id BtbN.FFmpeg.GPL.8.1 -e --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo.
        echo [ERROR] FFmpeg installation failed or was cancelled.
        pause
        exit /b 1
    )
    echo FFmpeg installed successfully.
    if exist "%WINGET_LINKS%\ffmpeg.exe" (
        set "FFMPEG=%WINGET_LINKS%\ffmpeg.exe"
        set "FFPROBE=%WINGET_LINKS%\ffprobe.exe"
    )
)

if not defined FFMPEG (
    echo.
    echo [ERROR] FFmpeg could not be located after installation.
    echo Close this window, open a new one, and re-run this script.
    pause
    exit /b 1
)
echo Using FFmpeg: !FFMPEG!
echo.

:: =====================================================================
:: 2. ASK WHICH FORMAT TO ENCODE
:: =====================================================================
echo Select the output format:
echo   1) OGG  (libvorbis)
echo   2) MP3  (libmp3lame)
echo.
choice /C 12 /N /M "Enter 1 or 2: "
set "OUT_EXT=ogg"
set "AUDIO_ARGS=-c:a libvorbis -q:a 5"
if errorlevel 2 (
    set "OUT_EXT=mp3"
    set "AUDIO_ARGS=-c:a libmp3lame -q:a 4"
)
echo Selected format: !OUT_EXT!
echo.

:: =====================================================================
:: 3. CONVERT ALL WAV FILES RECURSIVELY
::    The sample rate and channel count of each source WAV are read with
::    ffprobe and passed explicitly (-ar / -ac), so the encoded output
::    always matches the original frequency/rate of the source file.
:: =====================================================================
dir /b /s *.wav >nul 2>&1
if errorlevel 1 (
    echo No .wav files were found in this folder or its subfolders.
    pause
    exit /b 0
)

set /a CONVERTED=0
set /a FAILED=0

for /r %%i in (*.wav) do (
    echo Processing: "%%~nxi"

    rem Read the source sample rate and channel count via ffprobe.
    rem NOTE: the args with = or , MUST be quoted - cmd.exe's tokenizer
    rem splits for /f command strings on = and , (ffprobe would receive
    rem mangled arguments). ffprobe runs directly to a temp file instead.
    set "SRATE="
    set "SCHANNELS="
    "!FFPROBE!" -v error -select_streams a:0 -show_entries "stream=sample_rate,channels" -of "csv=p=0" "%%i" > "%TEMP%\_vfs_probe.txt" 2>nul
    for /f "usebackq tokens=1,2 delims=," %%A in ("%TEMP%\_vfs_probe.txt") do (
        set "SRATE=%%A"
        set "SCHANNELS=%%B"
    )
    del "%TEMP%\_vfs_probe.txt" >nul 2>&1
    if not defined SRATE set "SRATE=44100"
    if not defined SCHANNELS set "SCHANNELS=2"

    "!FFMPEG!" -y -i "%%i" -ar !SRATE! -ac !SCHANNELS! !AUDIO_ARGS! "%%~dpni.!OUT_EXT!" >nul 2>&1
    if "!errorlevel!"=="0" (
        if exist "%%~dpni.!OUT_EXT!" (
            echo    Success! Deleted original WAV.
            del "%%i"
            set /a CONVERTED+=1
        ) else (
            echo    ERROR: output file was not created for "%%~nxi". Keeping the original WAV.
            set /a FAILED+=1
        )
    ) else (
        echo    ERROR: failed to convert "%%~nxi". Keeping the original WAV.
        set /a FAILED+=1
    )
    echo ---------------------------------------
)

echo.
echo Conversion complete: !CONVERTED! converted, !FAILED! failed.
pause
