@echo off
rem SELFTEST.bat - check the whole online feature set without starting the game.
rem Needs Python (the same one PLAY.bat uses). Nothing is written to your save.
setlocal
cd /d "%~dp0"
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY (
  echo Python 3.8+ is required - see INSTALL.bat.
  goto :end
)
"%PY%" "%~dp0server\selftest.py"
:end
if /i not "%1"=="nopause" pause
