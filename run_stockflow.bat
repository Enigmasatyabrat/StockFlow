@echo off
setlocal
REM ============================================================
REM  Double-click this file, OR drag your photo folder onto its
REM  icon, to run StockFlow. No typing needed.
REM ============================================================

cd /d "%~dp0"

if "%~1"=="" (
    echo Tip: drag your photo folder onto this file's icon to skip this step.
    echo.
    set /p FOLDER="Paste the full path to your photos folder and press Enter: "
) else (
    set "FOLDER=%~1"
)

if "%FOLDER%"=="" (
    echo No folder given. Nothing to do.
    pause
    exit /b 2
)

echo.
echo ============================================================
echo  Preview first ^(no files moved, no API quota spent^)
echo ============================================================
python stockflow.py --dry-run "%FOLDER%"
if errorlevel 1 goto :failed

echo.
set /p GO="Process these images for real? [y/N]: "
if /i not "%GO%"=="y" (
    echo Cancelled. Nothing was changed.
    pause
    exit /b 0
)

echo.
python stockflow.py "%FOLDER%"
if errorlevel 1 goto :failed

echo.
echo ============================================================
echo  Done. Inside your photo folder, check:
echo    Reports\needs_review.txt         ^(read this BEFORE uploading^)
echo    Reports\shutterstock_upload.csv  ^(import this on Shutterstock^)
echo    01_READY_UPLOAD\                 ^(the images to upload^)
echo ============================================================
pause
exit /b 0

:failed
echo.
echo StockFlow exited with an error ^(code %errorlevel%^). Nothing further was run.
pause
exit /b %errorlevel%
