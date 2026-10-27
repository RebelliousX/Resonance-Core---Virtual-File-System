@echo off
setlocal enabledelayedexpansion

:: this is a specific batch file to convert audio found in Commandos 2 Destination Paris
:: they can have multiple extensions ".WAV.wav" for some reasons. 

for %%a in (*.wav) do (
    echo Converting %%a ...

    rem Create a temporary file name
    set "tmp=%%~na_tmp.wav"

    rem Convert to MP3 codec inside WAV container
    ffmpeg -y -i "%%a" -acodec mp3 -f wav "!tmp!"

    rem Replace original file only if FFmpeg succeeded (exit code exactly 0).
    rem Compare to "0" directly: in cmd, "if not errorlevel 1" also matches negative codes.
    if "!errorlevel!"=="0" (
        move /Y "!tmp!" "%%a"
    ) else (
        echo    ERROR: failed to convert "%%a". Keeping the original.
        del "!tmp!" >nul 2>&1
    )
)

echo Done.
pause
