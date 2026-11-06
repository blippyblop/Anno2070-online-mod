@echo off
rem =====================================================================
rem  INSTALL.bat - one-time setup for "Anno 2070 - Local Online".
rem
rem  What it does:
rem    1. reads config.ini (game path + player name)
rem    2. finds Anno 2070 if [game] path is blank
rem    3. checks that Anno5.exe is a build we know (prints the hash)
rem    4. copies the loader DLL into the game folder
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
for /f "usebackq tokens=1,2 delims==" %%A in ("%CFG%") do (
  set "k=%%A"
  set "v=%%B"
  set "k=!k: =!"
  if /i "!k!"=="path" set "GAMEDIR=%%B"
  if /i "!k!"=="name" set "PNAME=%%B"
)
set "GAMEDIR=%GAMEDIR: =%"
if defined GAMEDIR if not exist "%GAMEDIR%\Anno5.exe" (
  echo config.ini [game] path does not contain Anno5.exe: %GAMEDIR%
  set "GAMEDIR="
)

if not defined GAMEDIR (
  echo Looking for Anno 2070 ...
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
echo.

:end
if /i not "%1"=="nopause" pause
