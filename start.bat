@echo off
rem ============================================================
rem  feiyu-dual launcher
rem
rem  ASCII-only on purpose: cmd.exe parses .bat files under the
rem  OEM code page, so non-ASCII here would corrupt the script.
rem
rem  What it does:
rem    1. find Python
rem    2. reuse the current Python if it already has the deps,
rem       otherwise create .venv and pip install
rem    3. offer to download NapCat (QQ protocol side) if missing
rem    4. start the web console
rem
rem  Notes:
rem    - pip runs WITHOUT -q and with a timeout: a stuck mirror
rem      fails fast instead of hanging forever.
rem    - never run "pip install --upgrade pip": it goes to the
rem      default index and can hang for minutes.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul 2>nul

rem ---------------------------------------------------------- 1. python
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo [x] Python not found.
  echo.
  echo     Install Python 3.10 or newer:
  echo       https://www.python.org/downloads/
  echo     During setup, tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

rem ---------------------------------------------------------- 2. deps
%PY% -c "import httpx, websocket, qrcode" >nul 2>nul
if not errorlevel 1 (
  set "RUN=%PY%"
  goto :deps_ok
)

if not exist ".venv\Scripts\python.exe" (
  echo [*] Creating virtual environment .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [x] Failed to create venv.
    pause
    exit /b 1
  )
)
set "VPY=.venv\Scripts\python.exe"

"%VPY%" -c "import httpx, websocket, qrcode" >nul 2>nul
if not errorlevel 1 (
  set "RUN=%VPY%"
  goto :deps_ok
)

echo [*] Installing dependencies ... (1-3 minutes, progress below)
"%VPY%" -m pip install --disable-pip-version-check --timeout 20 --retries 2 -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
  echo.
  echo [!] Mirror failed, trying the default index ...
  "%VPY%" -m pip install --disable-pip-version-check --timeout 20 --retries 2 -r requirements.txt
)
"%VPY%" -c "import httpx, websocket, qrcode" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [x] Dependencies are still missing.
  echo     Run this by hand to see the real error:
  echo       .venv\Scripts\python -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)
set "RUN=%VPY%"

:deps_ok

rem ---------------------------------------------------------- 3. NapCat
rem  Only needed for the QQ channel; the Bilibili side works without it.
if exist "napcat\NapCat.Shell\NapCatWinBootMain.exe" goto :run

echo.
echo [i] NapCat not found.
echo     It is the QQ protocol bridge: required for the QQ channel,
echo     NOT needed for the Bilibili side.
%RUN% -m app.setup_napcat
if errorlevel 1 (
  echo [!] QQ channel stays offline. Retry later with:
  echo       %RUN% -m app.setup_napcat
  echo.
  timeout /t 5 >nul
)

:run
if "%~1"=="" (
  %RUN% -m app.main run
  echo.
  echo [*] Stopped. Press any key to close.
  pause >nul
) else (
  rem pass-through mode for scripting: start.bat doctor / selftest
  %RUN% -m app.main %*
)
