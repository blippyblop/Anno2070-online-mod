@echo off
rem =====================================================================
rem  PLAY.bat - the one thing to double-click.
rem
rem  Starts the local Anno 2070 online server, then starts the game ONCE.
rem  If the client drops its own login during bootstrap (a race inside the
rem  game, it no longer crashes) this says so and stops - just run it again.
rem
rem  Reads everything from config.ini next to this file.
rem =====================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "CFG=%~dp0config.ini"
set "HERE=%~dp0"

rem ---------- read config.ini (comments start with ';' or '#') -----------
for /f "usebackq eol=; tokens=1,* delims==" %%A in ("%CFG%") do call :cfg "%%A" "%%B"
if defined GAMEDIR (
  for /f "tokens=* delims= " %%x in ("!GAMEDIR!") do set "GAMEDIR=%%x"
)
if defined PNAME (
  for /f "tokens=* delims= " %%x in ("!PNAME!") do set "PNAME=%%x"
)
if defined HPORT (
  for /f "tokens=* delims= " %%x in ("!HPORT!") do set "HPORT=%%x"
)
if defined LOGDIR (
  for /f "tokens=* delims= " %%x in ("!LOGDIR!") do set "LOGDIR=%%x"
)
if defined HPORT set "HPORT=%HPORT: =%"
if not defined HPORT set "HPORT=8443"
if not defined LOGDIR set "LOGDIR=logs"
set "LOGDIR=%LOGDIR: =%"

rem registry hints first (Steam library, Ubisoft install dir)
for /f "tokens=2,*" %%A in ('reg query "HKCU\Software\Valve\Steam" /v SteamPath 2^>nul') do (
  set "STEAM=%%B"
)
if defined STEAM (
  for %%S in ("!STEAM:\=/!") do (
    if exist "%%~S\steamapps\common\Anno 2070\Anno5.exe" set "GAMEDIR=%%~S\steamapps\common\Anno 2070"
  )
)
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\Ubisoft\Anno 2070" /v InstallDir 2^>nul') do (
  if exist "%%B\Anno5.exe" set "GAMEDIR=%%B"
)
rem if the script sits next to Anno5.exe (zip extracted into the game
rem folder), that IS the game folder - no config needed
if not defined GAMEDIR if exist "%~dp0Anno5.exe" set "GAMEDIR=%~dp0"
rem or one level up (the zip keeps its own folder inside the game folder)
if not defined GAMEDIR if exist "%~dp0..\Anno5.exe" set "GAMEDIR=%~dp0.."
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
echo [1/5] checking for leftovers
rem Never force-close the game: a hard kill can truncate Config\Engine.ini and
rem then every later launch fails. Ask the player instead.
tasklist /nh /fo csv /fi "imagename eq Anno5.exe" 2>nul | findstr /b /i "\"Anno5.exe\"" >nul
if not errorlevel 1 (
  echo       Anno 2070 is already running.
  echo       Quit it from the game's own menu ^(not Task Manager^) and run
  echo       PLAY.bat again.
  goto :end
)
rem A leftover server would own the port and keep its own copy of the state.
rem Only that process is stopped - never every python.exe on the machine.
set "HOLDER=0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":!HPORT! " ^| findstr LISTENING') do set "HOLDER=%%p"
if not "!HOLDER!"=="0" (
  echo       a local server is still on port !HPORT! ^(PID !HOLDER!^) - stopping it
  taskkill /f /pid !HOLDER! >nul 2>&1
  call :sleep 2
)

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
rem the state file is named explicitly: it must be the same file on every
rem launch (a vote that "forgets itself" is almost always a different path)
start "Anno 2070 Local Server" /min "!PY!" "!HERE!server\mimic_server.py" --config "%CFG%" --state-file "!HERE!server\state.json" --log-dir "!HERE!!LOGDIR!"
echo       state: !HERE!server\state.json

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
rem The port opens before the server's handlers are warm.  Launching into that
rem window is one of the ways a client's own login can time out, so wait until
rem the server actually ANSWERS an HTTP request, not just accepts a connection.
set /a warm=0
:warmwait
"!PY!" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:!HPORT!/v3/applications/0', timeout=2).read(1)" >nul 2>&1
if not errorlevel 1 goto :warmok
set /a warm+=1
if !warm! lss 20 (
  call :sleep 1
  goto :warmwait
)
echo       WARNING: the server accepts connections but did not answer yet.
goto :launch
:warmok
echo       server is answering.

:launch
echo [5/5] starting Anno 2070
cd /d "!GAMEDIR!"
set UPC_CONQUEST_SUPPRESS_MS=0
start "" Anno5.exe
cd /d "!HERE!"
echo       waiting for the game to finish its online bootstrap
call :sleep 8
tasklist /nh /fo csv /fi "imagename eq Anno5.exe" 2>nul | findstr /b /i "\"Anno5.exe\"" >nul
if errorlevel 1 (
  echo.
  echo       The game did not start: no Anno5.exe is running.
  echo       Check that "!GAMEDIR!" really contains Anno5.exe, or set
  echo       [game] path in config.ini and run PLAY.bat again.
  goto :end
)
set /a waited=8
:watch
call :sleep 2
set /a waited+=2
tasklist /nh /fo csv /fi "imagename eq Anno5.exe" 2>nul | findstr /b /i "\"Anno5.exe\"" >nul
if errorlevel 1 goto :gone
if !waited! lss 100 goto :watch
goto :up

:gone
echo.
echo       The game closed during its online bootstrap.
echo       That is a race inside the game's own client ^(it cancels its own
echo       login^), not a server problem, and PLAY.bat does not retry any
echo       more. Run it again - it normally comes up on the first or second try.
echo       What the server did last is printed below.
echo.
call "!HERE!PROGRESS.bat" nopause
goto :end

:up
echo.
echo       The game is up. Have fun.
echo       Close Anno 2070 from its own menu when you are done, then close
echo       the "Anno 2070 Local Server" window.
echo.
call "!HERE!PROGRESS.bat" nopause
goto :end


:cfg
rem %1 = key, %2 = value (both trimmed). Called once per config.ini line.
set "k=%~1"
set "v=%~2"
rem keys never contain spaces, so removing them all is safe and kills the
rem trailing space that "path =" leaves behind ("=path" would never match)
set "k=!k: =!"
for /f "tokens=* delims= " %%x in ("!v!") do set "v=%%x"
if /i "!k!"=="name"        set "PNAME=!v!"
if /i "!k!"=="path"        set "GAMEDIR=!v!"
if /i "!k!"=="http_port"   set "HPORT=!v!"
if /i "!k!"=="log_dir"     set "LOGDIR=!v!"
goto :eof

:sleep
rem %1 = seconds. ping is the only sleep that works without a console.
set /a _s=%1+1
ping -n !_s! 127.0.0.1 >nul
goto :eof

:end
if /i not "%1"=="nopause" pause
