@echo off
setlocal enabledelayedexpansion

echo Starting WAV to OGG conversion...
echo ---------------------------------------

:: Loop through all WAV files recursively
for /r %%i in (*.wav) do (
    echo Processing: "%%~nxi"
    
    :: Use quotes directly around the full input and output paths
    ffmpeg -y -i "%%i" -c:a libvorbis -q:a 5 "%%~dpni.ogg" >nul 2>&1
    
    :: Verify the new file exists using the exact same path formatting
    set "RC=!errorlevel!"
    set "OK=0"
    if "!RC!"=="0" if exist "%%~dpni.ogg" set "OK=1"
    if "!OK!"=="1" (
        echo Success! Deleting original WAV...
        del "%%i"
    ) else (
        echo ERROR: Failed to convert "%%~nxi". Keeping WAV file.
    )
    echo ---------------------------------------
)

echo Conversion complete!
pause
