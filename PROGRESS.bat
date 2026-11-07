@echo off
rem =====================================================================
rem  PROGRESS.bat - what did the last session actually achieve?
rem  Reads logs\server.log and prints the numbers that matter.
rem =====================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "SV=logs\server.log"

if not exist "%SV%" (
  echo No server log yet - run PLAY.bat first.
  goto :end
)

echo ================= ANNO 2070 LOCAL ONLINE - LAST SESSION =============
for %%F in ("%SV%") do echo log: %%~fF   written %%~tF

set RV=0
for /f %%c in ('findstr /c:"RV-RMC [http:/]" "%SV%" ^| find /c /v ""') do set RV=%%c
set FAIL=0
for /f %%c in ('findstr /c:"status=0" "%SV%" ^| find /c /v ""') do set FAIL=%%c
set PUSH=0
for /f %%c in ('findstr /c:"pushed space-params" "%SV%" ^| find /c /v ""') do set PUSH=%%c
set NAME=
for /f "usebackq eol=; tokens=1,* delims==" %%A in ("%~dp0config.ini") do call :cfg "%%A" "%%B"
set "NAME=!PNAME!"

echo.
echo [ session ]   player name       : !NAME!
echo               RV calls answered : !RV!
echo               failed replies    : !FAIL!   (0 is the goal)
echo               notification push : !PUSH! frames

echo.
echo [ services exercised ]
for %%P in (LoginProtocol BackEndServiceProtocol PlayerProfileServiceProtocol VotingServiceProtocol WorldEventServiceProtocol ChatServiceProtocol FriendsServiceProtocol MailServiceProtocol MatchMakingServiceProtocol) do (
  set N=0
  for /f %%c in ('findstr /c:"%%P::" "%SV%" ^| find /c /v ""') do set N=%%c
  if not "!N!"=="0" echo    %%P  : !N! calls
)

echo.
echo [ the headline features ]
findstr /c:"GetArkItems_V1" "%SV%" >nul && (echo    ARK item box      : YES) || echo    ARK item box      : no
findstr /c:"GetVotingInfos_V1" "%SV%" >nul && (echo    senate elections  : YES) || echo    senate elections  : no
findstr /c:"Vote_V1 call" "%SV%" >nul && (echo    a vote was cast   : YES) || echo    a vote was cast   : no
findstr /c:"GetLatestWorldEvent_V1" "%SV%" >nul && (echo    world events      : YES) || echo    world events      : no
findstr /c:"PlayerProfile" "%SV%" >nul && (echo    player profile    : YES) || echo    player profile    : no

echo.
echo [ not implemented yet ]
findstr /c:"CONQ-HTTP <-" "%SV%" >nul && (echo    conquest server   : reached) || echo    conquest server   : not reached
findstr /c:"MatchMakingServiceProtocol::" "%SV%" >nul && (echo    multiplayer lobby : reached) || echo    multiplayer lobby : not reached

echo.
echo [ saved progress ]
set "SPY="
where python >nul 2>&1 && set "SPY=python"
if not defined SPY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "SPY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if defined SPY (
  "!SPY!" "%~dp0server\state_report.py" "%~dp0server\state.json"
) else (
  echo    python not found - cannot read server\state.json
)

echo.
echo [ online services ]
if defined SPY (
  "!SPY!" "%~dp0server\session_report.py" "%SV%"
) else (
  echo    python not found - cannot summarise the session
)

echo.
echo ---------------------------------------------------------------------
if !RV! GEQ 25 (
  echo VERDICT: a full online session - the server answered !RV! calls with
  echo          !FAIL! failures.
) else (
  echo VERDICT: the client did not finish its online bootstrap this time.
  echo          That is a race inside the game's own client - run PLAY.bat again.
)
echo ---------------------------------------------------------------------
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

:end
if /i not "%1"=="nopause" pause
