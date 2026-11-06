@echo off
rem Runs the offline test suites (no game needed). Development aid.
cd /d "%~dp0"
set "PY=python"
where python >nul 2>&1 || set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
set FAIL=0
for %%T in (tests\test_*.py) do (
  echo === %%T
  "%PY%" "%%T" || set FAIL=1
)
echo.
if "%FAIL%"=="0" (echo ALL SUITES PASSED) else (echo SOME SUITES FAILED)
if /i not "%1"=="nopause" pause
