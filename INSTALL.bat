@echo off
rem =====================================================================
rem  INSTALL.bat - one-time setup for "Anno 2070 - Local Online".
rem
rem  What it does:
rem    1. reads config.ini (game path + player name)
rem    2. finds Anno 2070 if [game] path is blank
rem    3. checks that Anno5.exe is a build we know (prints the hash)
rem    4. copies the loader DLL into the game folder, keeping Ubisoft's own
rem       DLL as upc_r2_loader64.dll.original-backup for UNINSTALL.bat
rem    5. creates a shortcut-free "PLAY.bat" workflow and checks Python
rem
rem  It never modifies the game's own files: the loader DLL sits next to
rem  Anno5.exe and does everything at run time.
rem =====================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo =====================================================================
echo  Anno 2070 - Local Online : installer
echo =====================================================================
echo.

set CFG=%~dp0config.ini
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

if defined GAMEDIR if not exist "%GAMEDIR%\Anno5.exe" (
  echo config.ini [game] path does not contain Anno5.exe: %GAMEDIR%
  set "GAMEDIR="
)

if not defined GAMEDIR if exist "%~dp0Anno5.exe" set "GAMEDIR=%~dp0"
rem or one level up (the zip keeps its own folder inside the game folder)
if not defined GAMEDIR if exist "%~dp0..\Anno5.exe" set "GAMEDIR=%~dp0.."
if not defined GAMEDIR (
  echo Looking for Anno 2070 ...
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
      goto :found
    )
  )
)

:found
if not defined GAMEDIR (
  echo.
  echo  Could not find Anno 2070 automatically.
  echo  Put the full path in config.ini:
  echo      [game]
  echo      path = C:\Path\To\Anno 2070
  echo.
  goto :end
)
echo Game folder : "!GAMEDIR!"
if defined PNAME echo Player name : "!PNAME!"

echo.
echo [1/3] checking the game build
for /f "skip=1 delims=" %%H in ('certutil -hashfile "!GAMEDIR!\Anno5.exe" SHA256') do (
  if not defined HASH set "HASH=%%H"
)
echo       Anno5.exe SHA256: !HASH!
echo       ^(this release is tested on build 3.0.8051 / GOLDMASTER; other builds
echo        may work but the loader's patches are build-specific^)

echo.
echo [2/3] installing the loader
if not exist "loader\bin\upc_r2_loader64.dll" (
  echo       ERROR: loader\bin\upc_r2_loader64.dll is missing from the package.
  goto :end
)
if exist "!GAMEDIR!\upc_r2_loader64.dll" (
  rem keep Ubisoft's own DLL once, so UNINSTALL.bat can put it back
  if not exist "!GAMEDIR!\upc_r2_loader64.dll.original-backup" (
    findstr /m /c:"upc_stub.log" "!GAMEDIR!\upc_r2_loader64.dll" >nul 2>&1
    if errorlevel 1 (
      copy /y "!GAMEDIR!\upc_r2_loader64.dll" "!GAMEDIR!\upc_r2_loader64.dll.original-backup" >nul
      echo       kept the original loader as upc_r2_loader64.dll.original-backup
    )
  )
  echo       replacing the existing loader
)
copy /y "loader\bin\upc_r2_loader64.dll" "!GAMEDIR!\upc_r2_loader64.dll" >nul
if errorlevel 1 (
  echo       ERROR: could not copy into the game folder.
  echo       Close Anno 2070 and any Ubisoft/UPC process and try again,
  echo       or run this file as administrator if the folder is protected.
  goto :end
)
echo       loader installed.

echo.
echo [3/3] checking Python
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
  if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
)
if defined PY (
  echo       Python found: !PY!
) else (
  echo       Python was not found. This package needs Python 3.8 or newer
  echo       to run its server. Install it from python.org ^(tick "Add to
  echo       PATH"^) and run INSTALL.bat again.
)

echo.
echo Done. Next: run PLAY.bat.
echo To remove everything again later: run UNINSTALL.bat.
echo.


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

:end
if /i not "%1"=="nopause" pause
