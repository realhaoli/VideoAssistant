@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0start-local.py" %*
) else (
    py -3.12 "%~dp0start-local.py" %*
)
exit /b %errorlevel%
