@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0pcl_home_server.py"
  goto :eof
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%~dp0pcl_home_server.py"
  goto :eof
)

echo Python launcher not found.
echo Install Python 3 or make sure py/python is available in PATH.
pause
