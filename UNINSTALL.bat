@echo off
rem =====================================================================
rem  UNINSTALL.bat - remove "Anno 2070 - Local Online" from this PC.
rem
rem  Double-click it. It undoes what INSTALL.bat did and nothing else:
rem    1. stops a local server / launcher that is still running
rem    2. puts Ubisoft's original upc_r2_loader64.dll back (from the backup
rem       INSTALL.bat keeps), or removes our loader if there is no backup
rem    3. deletes upc_stub.log from the game folder
rem    4. keeps server\state.json + config.ini in %LOCALAPPDATA% and deletes
rem       this folder and the shortcuts
rem
rem  Switches:  /yes    no questions
rem             /purge  also throw away the saved state
rem
rem  The game's own files are never touched. Your savegames are not touched.
rem =====================================================================
setlocal enabledelayedexpansion

if /i "%~1"=="__child" goto :run

rem ---- relaunch myself from %TEMP% -------------------------------------
rem cmd keeps this file open, so the folder can only be fully deleted if the
rem copy that does the work lives somewhere else.
set "PKG=%~dp0."
set "TMPBAT=%TEMP%\Anno2070Online-uninstall.bat"
set "FLAG=%TEMP%\Anno2070Online-uninstall.started"
del /f /q "%FLAG%" >nul 2>&1
copy /y "%~f0" "%TMPBAT%" >nul 2>&1
if not exist "%TMPBAT%" goto :run
echo Re-launching the uninstaller from a temporary folder ...
start "" "%TMPBAT%" __child %*
call :sleep
if exist "%FLAG%" exit /b
rem some environments (a remote shell without a console) cannot start a new
rem window. Then the work is done from here and only this file is left over.
echo The temporary copy did not start - doing the work from here instead.
del /f /q "%TMPBAT%" >nul 2>&1
goto :run

:run
set "CHILD="
if /i "%~1"=="__child" set "CHILD=1"
if defined CHILD echo started> "%FLAG%" 2>nul
rem a directory that exists and is not the one being deleted; the
rem environment is not always complete (a remote shell may have no
rem %SystemRoot%), so check before using it
set "SAFEDIR=%SystemRoot%"
if not exist "%SAFEDIR%\" set "SAFEDIR=%TEMP%"
if not exist "%SAFEDIR%\" set "SAFEDIR=%HOMEDRIVE%%HOMEPATH%"
if not exist "%SAFEDIR%\" set "SAFEDIR=C:\"
cd /d "%SAFEDIR%" 2>nul
if not defined PKG set "PKG=%~dp0."
echo =====================================================================
echo  Anno 2070 - Local Online : uninstall
echo =====================================================================
echo.

set "YES="
set "PURGE="
set "ARGS=%*"
if defined ARGS for %%A in (%ARGS%) do (
  if /i "%%A"=="/yes"   set "YES=1"
  if /i "%%A"=="/purge" set "PURGE=1"
)

rem ---------- where is the game? ---------------------------------------
set "CFG=%PKG%\config.ini"
if exist "%CFG%" (
  for /f "usebackq eol=; tokens=1,* delims==" %%A in ("%CFG%") do call :cfg "%%A" "%%B"
)
if defined GAMEDIR for /f "tokens=* delims= " %%x in ("!GAMEDIR!") do set "GAMEDIR=%%x"
if defined GAMEDIR if not exist "%GAMEDIR%\Anno5.exe" set "GAMEDIR="

if not defined GAMEDIR if exist "%PKG%\Anno5.exe" set "GAMEDIR=%PKG%"
if not defined GAMEDIR if exist "%PKG%\..\Anno5.exe" set "GAMEDIR=%PKG%\.."
if not defined GAMEDIR (
  for /f "tokens=2,*" %%A in ('reg query "HKCU\Software\Valve\Steam" /v SteamPath 2^>nul') do set "STEAM=%%B"
  if defined STEAM if exist "!STEAM:\=/!\steamapps\common\Anno 2070\Anno5.exe" (
    set "GAMEDIR=!STEAM!\steamapps\common\Anno 2070"
  )
)
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
    if exist "%%~C\Anno5.exe" set "GAMEDIR=%%~C"
  )
)

echo Folder    : %PKG%
if defined GAMEDIR (
  echo Game      : %GAMEDIR%
) else (
  echo Game      : not found - the loader will be left alone
)
echo.

rem ---------- is the game still running? -------------------------------
tasklist /nh /fo csv /fi "imagename eq Anno5.exe" 2>nul | findstr /b /i "\"Anno5.exe\"" >nul
if not errorlevel 1 (
  echo Anno 2070 is still running. Quit it from the game's own menu and run
  echo this again. Do NOT just kill it: a forced kill can truncate
  echo Config\Engine.ini and then every later launch fails.
  echo.
  goto :end
)

rem ---------- stop our own processes ------------------------------------
tasklist /nh /fo csv /fi "imagename eq Anno2070Online.exe" 2>nul | findstr /b /i "\"Anno2070Online.exe\"" >nul
if not errorlevel 1 (
  echo Stopping the launcher ...
  taskkill /im Anno2070Online.exe >nul 2>&1
  call :pause2
  taskkill /f /im Anno2070Online.exe >nul 2>&1
)
powershell -nop -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $p=@(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*mimic_server.py*' }); foreach($x in $p){ Stop-Process -Id $x.ProcessId -Force }; if($p.Count -gt 0){ 'stopped the local server' }" 2>nul

rem ---------- confirm ---------------------------------------------------
echo This will:
echo   - put Ubisoft's upc_r2_loader64.dll back, or remove ours if there is
echo     no backup of the original
echo   - delete upc_stub.log from the game folder
if defined PURGE (
  echo   - delete this folder INCLUDING your saved online state
) else (
  echo   - keep state.json + config.ini, then delete this folder and the
  echo     shortcuts
)
echo.
if not defined YES (
  set "ANS="
  set /p "ANS=Continue? [y/N] "
  if /i not "!ANS!"=="y" if /i not "!ANS!"=="yes" (
    echo Nothing was changed.
    goto :end
  )
)

rem ---------- the loader in the game folder -----------------------------
echo.
echo [1/4] the loader in the game folder
if not defined GAMEDIR (
  echo       game folder unknown - skipping
  goto :keep
)
set "DLL=%GAMEDIR%\upc_r2_loader64.dll"
if not exist "%DLL%" (
  echo       no loader in the game folder - nothing to remove
  goto :keep
)
if exist "%DLL%.original-backup" (
  move /y "%DLL%.original-backup" "%DLL%" >nul 2>&1
  if errorlevel 1 (
    echo       could not restore the original - is the game running? close
    echo       everything and try again.
  ) else (
    echo       original loader restored
  )
  goto :keep
)
rem no backup: only remove the DLL if it is ours, or after asking
findstr /m /c:"upc_stub.log" "%DLL%" >nul 2>&1
if errorlevel 1 (
  echo       the loader there is not ours - left untouched
  echo       ^(if you installed an older release it may still be ours: %DLL%^)
  goto :keep
)
if not defined YES (
  set "ANS2="
  set /p "ANS2=      delete %DLL% (no backup of the original exists)? [y/N] "
  if /i not "!ANS2!"=="y" if /i not "!ANS2!"=="yes" (
    echo       kept
    goto :keep
  )
)
del /f /q "%DLL%"
if exist "%DLL%" (
  echo       could not delete it - is the game running?
) else (
  echo       our loader deleted
  echo       no backup of Ubisoft's original was found; Steam can put it back:
  echo       right click Anno 2070 ^> Properties ^> Installed files ^>
  echo       Verify integrity of game files
)
:keep
if defined GAMEDIR del /f /q "%GAMEDIR%\upc_stub.log" >nul 2>&1

rem ---------- keep the saved state --------------------------------------
echo.
echo [2/4] your saved state
if defined PURGE (
  echo       /purge given - not kept
  goto :erase
)
if not exist "%PKG%\server\state.json" (
  echo       nothing to keep
  goto :erase
)
set "DEST=%LOCALAPPDATA%\Anno2070Online-backup"
if exist "%DEST%" set "DEST=%LOCALAPPDATA%\Anno2070Online-backup-%RANDOM%"
mkdir "%DEST%" >nul 2>&1
copy /y "%PKG%\server\state.json" "%DEST%\state.json" >nul 2>&1
if exist "%PKG%\config.ini" copy /y "%PKG%\config.ini" "%DEST%\config.ini" >nul 2>&1
if exist "%DEST%\state.json" (
  echo       kept in %DEST%
) else (
  echo       could not copy it - delete this folder by hand if you want it gone
  goto :end
)
:erase

rem ---------- remove this folder ----------------------------------------
echo.
echo [3/4] this folder
if exist "%PKG%\Anno5.exe" (
  rem the package was extracted straight into the game folder.  Deleting the
  rem folder would delete the game, so remove exactly the files the package
  rem brought and leave everything else alone.
  echo       this folder is the game folder - removing only the mod's files
  for %%F in (PLAY.bat INSTALL.bat UNINSTALL.bat PROGRESS.bat SELFTEST.bat RUN-TESTS.bat ^
              READ-ME-FIRST.txt README.md VERSION LICENSE config.ini) do (
    if exist "%PKG%\%%F" del /f /q "%PKG%\%%F" >nul 2>&1
  )
  for %%D in (server docs loader tests logs) do (
    if exist "%PKG%\%%D\." rmdir /s /q "%PKG%\%%D" >nul 2>&1
  )
  echo       done - Anno5.exe and the game's own folders are untouched
  goto :shortcuts
)
if not exist "%PKG%\PLAY.bat" if not exist "%PKG%\server\mimic_server.py" (
  echo       this folder does not look like the mod's folder - not deleting it
  goto :shortcuts
)
cd /d "%SAFEDIR%" 2>nul
rem when this script lives inside that folder the delete can take the
rem script with it, so say it before, not after
if not defined CHILD echo       deleting %PKG% ...
rmdir /s /q "%PKG%" >nul 2>&1
if not exist "%PKG%" (
  echo       deleted
  goto :shortcuts
)
if exist "%PKG%\UNINSTALL.bat" if not exist "%PKG%\PLAY.bat" if not exist "%PKG%\server" (
  echo       everything is gone except this file, which cannot be deleted
  echo       while it runs. Delete it by hand:
  echo       %PKG%\UNINSTALL.bat
) else (
  echo       some files are still locked - delete the folder by hand:
  echo       %PKG%
)

:shortcuts
rem ---------- shortcuts --------------------------------------------------
echo.
echo [4/4] shortcuts
for %%D in (
  "%USERPROFILE%\Desktop"
  "%PUBLIC%\Desktop"
  "%APPDATA%\Microsoft\Windows\Start Menu\Programs"
  "%ProgramData%\Microsoft\Windows\Start Menu\Programs"
) do (
  if exist "%%~D" (
    del /f /q "%%~D\Anno 2070 - Local Online.lnk" >nul 2>&1
    del /f /q "%%~D\Uninstall Anno 2070 - Local Online.lnk" >nul 2>&1
  )
)
echo       done

echo.
echo =====================================================================
echo  Done. The game is back to its normal, offline behaviour.
echo =====================================================================
echo.

:end
if /i not "%1"=="/yes" if not defined YES pause
exit /b

:pause2
ping -n 3 127.0.0.1 >nul
goto :eof

:sleep
ping -n 5 127.0.0.1 >nul
goto :eof

:cfg
rem %1 = key, %2 = value. Called once per config.ini line.
set "k=%~1"
set "v=%~2"
set "k=!k: =!"
for /f "tokens=* delims= " %%x in ("!v!") do set "v=%%x"
if /i "!k!"=="path" set "GAMEDIR=!v!"
goto :eof
