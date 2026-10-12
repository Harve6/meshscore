@echo off
rem Runs MeshScore and starts it again if it ever exits (a dropped USB cable, a crash...).
rem Close the window or press Ctrl+C twice to stop.
cd /d "%~dp0\.."
if not exist .venv\Scripts\activate.bat (
  echo Run windows\install.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
if not exist meshscore.toml (
  echo No meshscore.toml yet - starting the setup wizard.
  meshscore init
)
:again
meshscore run
echo.
echo MeshScore stopped (exit code %errorlevel%). Restarting in 10 seconds - close this window to stop.
timeout /t 10 >nul
goto again
