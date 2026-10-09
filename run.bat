@echo off
rem Starts Elite Trade Copilot without a console window.
cd /d "%~dp0"
where pythonw >nul 2>&1 && (start "" pythonw "%~dp0copilot.py" & exit /b)
where py >nul 2>&1 && (start "" py -3 "%~dp0copilot.py" & exit /b)
echo Python 3 was not found. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH").
pause
