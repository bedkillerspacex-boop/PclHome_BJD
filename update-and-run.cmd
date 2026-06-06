@echo off
setlocal
cd /d "%~dp0"

set "REPO_ZIP_URL=https://gh-proxy.com/https://github.com/bedkillerspacex-boop/PclHome_BJD/archive/refs/heads/master.zip"
set "TMP_ZIP=%TEMP%\PclHome_BJD-master.zip"
set "TMP_DIR=%TEMP%\PclHome_BJD-master"

echo Updating PclHome_BJD from remote...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue';" ^
  "try { Invoke-WebRequest -UseBasicParsing '%REPO_ZIP_URL%' -OutFile '%TMP_ZIP%' } catch { exit 1 }"
if errorlevel 1 (
  echo Update download failed. Starting local version...
  call "%~dp0run-server.cmd"
  goto :eof
)

if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%"
mkdir "%TMP_DIR%" >nul 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -LiteralPath '%TMP_ZIP%' -DestinationPath '%TMP_DIR%' -Force"
if errorlevel 1 (
  echo Update extract failed. Starting local version...
  call "%~dp0run-server.cmd"
  goto :eof
)

for /d %%D in ("%TMP_DIR%\PclHome_BJD-*") do (
  xcopy "%%D\*" "%~dp0" /E /I /Y >nul
  goto :copied
)

:copied
echo Update finished.
call "%~dp0run-server.cmd"
