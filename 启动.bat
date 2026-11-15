@echo off
cd /d %~dp0
echo Starting Douyin Live Recorder ...
python run.py
if errorlevel 1 (
    echo.
    echo Program exited with error code %errorlevel%. See messages above.
    pause
)
