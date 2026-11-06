@echo off
rem =====================================================================
rem  PLAY.bat - the one thing to double-click.
rem
rem  Starts the local Anno 2070 online server, launches the game, and
rem  restarts the game up to 3 times if it dies during its online
rem  bootstrap (a known client-side crash, roughly 1 run in 3).
rem
rem  Reads everything from config.ini next to this file.
rem =====================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "CFG=%~dp0config.ini"
set "HERE=%~dp0"

rem ---------- read config.ini -----------------------------------------
for /f "usebackq tokens=1,2 delims==" %%A in ("%CFG%") do (
  set "k=%%A"
  set "v=%%B"
  set "k=!k: =!"
  if /i "!k!"=="name"       set "PNAME=%%B"
  if /i "!k!"=="path"       set "GAMEDIR=%%B"
  if /i "!k!"=="http_port"  set "HPORT=%%B"
  if /i "!k!"=="log_dir"    set "LOGDIR=%%B"
)
set "GAMEDIR=%GAMEDIR: =%"
if defined HPORT set "HPORT=%HPORT: =%"
if not defined HPORT set "HPORT=8443"
if not defined LOGDIR set "LOGDIR=logs"
set "LOGDIR=%LOGDIR: =%"

if not defined GAMEDIR (
  for %%C in (
    "C:\Program Files (x86)\Steam\steamapps\common\Anno 2070"
    "C:\Program Files\Steam\steamapps\common\Anno 2070"
    "C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\games\Anno 2070"
    "C:\Program Files\Ubisoft\Ubisoft Game Launcher\games\Anno 2070"
    "D:\Steam\steamapps\common\Anno 2070"
    "D:\Games\Anno 2070"
    "C:\Games\Anno 2070"
  ) do (
    if exist "%%~C\Anno5.exe" (
      set "GAMEDIR=%%~C"
      goto :gotgame
    )
  )
)
:gotgame
if not defined GAMEDIR (
  echo Could not find Anno 2070. Set [game] path in config.ini and run again.
  goto :end
)

rem ---------- python ---------------------------------------------------
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
  if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
)
if not defined PY (
  echo Python 3.8+ is required. Install it from python.org and run INSTALL.bat.
  goto :end
)

echo =====================================================================
echo  Anno 2070 - Local Online
echo  player: !PNAME!   game: !GAMEDIR!
echo =====================================================================
echo [1/5] clearing stale processes
taskkill /f /im Anno5.exe >nul 2>&1
taskkill /f /im python.exe >nul 2>&1
call :sleep 3

echo [2/5] making sure the loader is in place
if not exist "!GAMEDIR!\upc_r2_loader64.dll" (
  echo       not installed yet - copying it now
  copy /y "loader\bin\upc_r2_loader64.dll" "!GAMEDIR!\upc_r2_loader64.dll" >nul
  if errorlevel 1 (
    echo       ERROR: could not copy the loader into "!GAMEDIR!".
    echo       Run INSTALL.bat as administrator, then try again.
    goto :end
  )
)

echo [3/5] starting the local server
if not exist "!LOGDIR!" mkdir "!LOGDIR!"
if exist "!LOGDIR!\server.log" move /y "!LOGDIR!\server.log" "!LOGDIR!\server.prev.log" >nul 2>&1
start "Anno 2070 Local Server" /min "!PY!" "!HERE!server\mimic_server.py" --config "%CFG%" --log-dir "!HERE!!LOGDIR!"

echo [4/5] waiting for the server on port !HPORT!
set /a tries=0
:waitport
call :sleep 2
netstat -an | findstr ":!HPORT!" | findstr LISTENING >nul && goto :portok
set /a tries+=1
if !tries! lss 20 goto :waitport
echo       WARNING: port !HPORT! never opened. Check "!LOGDIR!\server.log".
echo       Starting the game anyway.
goto :launch
:portok
echo       server is up.

:launch
set /a attempt=1
:trygame
echo [5/5] starting Anno 2070 (attempt !attempt! of 3)
cd /d "!GAMEDIR!"
set UPC_CONQUEST_SUPPRESS_MS=0
start "" Anno5.exe
cd /d "!HERE!"
call :sleep 100
tasklist /fi "imagename eq Anno5.exe" | findstr /i "Anno5.exe" >nul
if not errorlevel 1 goto :up
echo.
echo       The game closed during its online bootstrap. This is the known
echo       client-side crash (about 1 run in 3), not a server problem.
set /a attempt+=1
if !attempt! leq 3 goto :trygame
echo       Three attempts failed. Run PROGRESS.bat to see what the server did,
echo       and read docs\known-issues.md.
goto :end

:up
echo.
echo       The game is up. Have fun.
echo       Close Anno 2070 from its own menu when you are done, then close
echo       the "Anno 2070 Local Server" window.
echo.
call "!HERE!PROGRESS.bat" nopause

:sleep
rem %1 = seconds. ping is the only sleep that works without a console.
set /a _s=%1+1
ping -n !_s! 127.0.0.1 >nul
goto :eof

:end
if /i not "%1"=="nopause" pause
