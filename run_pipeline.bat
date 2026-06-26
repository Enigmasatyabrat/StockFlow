@echo off
REM ============================================================
REM  Double-click this file, OR drag your photo folder onto its
REM  icon, to run the stock metadata pipeline. No typing needed.
REM ============================================================

cd /d "%~dp0"

if "%~1"=="" (
    echo Drag your photo folder onto this file's icon next time to skip this step.
    echo.
    set /p FOLDER="Paste the full path to your photos folder and press Enter: "
) else (
    set FOLDER=%~1
)

echo.
echo Running pipeline on: %FOLDER%
echo.

python stock_pipeline_v3.py "%FOLDER%"

echo.
echo ============================================================
echo Done. Inside your photo folder, check:
echo   - needs_review.txt        (read this BEFORE uploading)
echo   - shutterstock_upload.csv (import this on Shutterstock)
echo ============================================================
pause
