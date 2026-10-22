@echo off
rem ============================================================
rem  Start NapCat - hardened launcher
rem
rem  Lessons baked in (each one broke a previous attempt):
rem   1. NapCat's own launcher elevates via wt.exe and dies silently.
rem   2. Calling NapCatWinBootMain.exe directly WITHOUT the five
rem      NAPCAT_* variables makes the injection misbehave.
rem   3. "Start-Process -Verb RunAs" on a .bat is unreliable -> we
rem      elevate cmd.exe through _elevate.ps1 instead.
rem   4. Per AGENTS.md: no bare command names, every program is
rem      called by full path.
rem
rem  ASCII-only on purpose: cmd.exe parses .bat under OEM code page.
rem ============================================================
chcp 65001 >nul
setlocal

set "NPC=%~dp0napcat\NapCat.Shell"
set "LOG=%~dp0napcat-launch.log"
set "SYS=%SystemRoot%\System32"

call :log "===== start ====="
call :log "NPC=%NPC%"

if not exist "%NPC%\NapCatWinBootMain.exe" (
  echo [x] NapCat not found at %NPC%
  call :log "ABORT: NapCatWinBootMain.exe missing"
  pause
  exit /b 1
)

rem --- 1. QQ must be closed
"%SYS%\tasklist.exe" /FI "IMAGENAME eq QQ.exe" 2>nul | "%SYS%\find.exe" /I "QQ.exe" >nul
if not errorlevel 1 (
  echo [x] QQ is still running. Quit it first ^(tray icon too^).
  call :log "ABORT: QQ still running"
  pause
  exit /b 1
)
call :log "step1 ok: QQ not running"

rem --- 2. need administrator
rem  NAPCAT_SKIP_ELEVATE=1 lets us test the rest of the chain
rem  without a UAC prompt. Never set it in normal use.
if defined NAPCAT_SKIP_ELEVATE (
  call :log "step2 skipped via NAPCAT_SKIP_ELEVATE"
  goto :after_admin
)
"%SYS%\net.exe" session >nul 2>&1
if errorlevel 1 (
  echo Requesting administrator rights ...
  call :log "not admin -> elevating via _elevate.ps1"
  "%SYS%\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0_elevate.ps1" -Target "%~f0"
  call :log "elevation helper returned %ERRORLEVEL%"
  exit /b
)
call :log "step2 ok: running as administrator"

:after_admin

rem --- 3. locate QQ.exe
rem  NOTE: do not quote "%SYS%\reg.exe" inside for /f - cmd parses the
rem  first token as the program and quoted paths break it.
set "UNINST="
for /f "tokens=2*" %%a in ('%SYS%\reg.exe query "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ" /v UninstallString 2^>nul') do set "UNINST=%%~b"
call :log "registry UninstallString=%UNINST%"

set "QQDIR="
if defined UNINST for %%d in ("%UNINST%") do set "QQDIR=%%~dpd"
set "QQPATH=%QQDIR%QQ.exe"

if not exist "%QQPATH%" (
  echo [x] QQ.exe not found at %QQPATH%
  call :log "ABORT: QQ.exe missing"
  pause
  exit /b 1
)
call :log "step3 ok: QQ=%QQPATH%"

rem --- 4. the five variables NapCat's launcher normally sets
set "NAPCAT_PATCH_PACKAGE=%NPC%\qqnt.json"
set "NAPCAT_LOAD_PATH=%NPC%\loadNapCat.js"
set "NAPCAT_INJECT_PATH=%NPC%\NapCatWinBootHook.dll"
set "NAPCAT_LAUNCHER_PATH=%NPC%\NapCatWinBootMain.exe"
set "NAPCAT_MAIN_PATH=%NPC%\napcat.mjs"
call :log "step4 ok: env vars set"

echo.
echo  QQ     : %QQPATH%
echo  NapCat : %NPC%
echo  Launching ...
echo.
call :log "launching booter"

cd /d "%NPC%"
"%NAPCAT_LAUNCHER_PATH%" "%QQPATH%" "%NAPCAT_INJECT_PATH%"
set "RC=%ERRORLEVEL%"

call :log "booter exited with code %RC%"
echo.
echo NapCat exited with code %RC%.
echo See napcat-launch.log in this folder for the trace.
pause
exit /b %RC%

:log
>>"%LOG%" echo [%time:~0,8%] %~1
goto :eof
