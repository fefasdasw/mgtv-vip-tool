@echo off
cd /d "%~dp0"
echo ============================================================
echo  MGTV downloader
echo  working dir : %CD%
echo ============================================================
python "%~dp0mgtv_vip_downloader.py" %*
set RC=%errorlevel%
echo.
if not "%RC%"=="0" echo [exit code %RC%]
pause
