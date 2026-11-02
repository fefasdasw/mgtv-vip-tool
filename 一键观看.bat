@echo off
cd /d "%~dp0"
python "%~dp0watch.py" %*
set RC=%errorlevel%
if not "%RC%"=="0" echo.
pause
