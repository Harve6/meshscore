@echo off
rem MeshScore quick install for Windows: makes a virtual environment next to this folder and installs the bot.
rem Needs Python 3.10+ from https://www.python.org/downloads/ ("Add python.exe to PATH" ticked).
cd /d "%~dp0\.."
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/ and tick "Add to PATH".
  pause
  exit /b 1
)
if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install ".[ble]" || goto :fail
echo.
echo ============================================================
echo  MeshScore is installed.
echo  Next:   meshscore init     (radio, channels, teams)
echo          meshscore check
echo          windows\run-meshscore.bat
echo ============================================================
echo.
cmd /k
exit /b 0
:fail
echo Install failed - see the messages above.
pause
exit /b 1
