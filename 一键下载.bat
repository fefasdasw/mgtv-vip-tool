@echo off
cd /d "%~dp0"
python "%~dp0wsyzy.py" %*
set RC=%errorlevel%
if not "%RC%"=="0" (
  echo.
  echo [exit code %RC%]
)
pause
