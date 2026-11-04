@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys, tkinter; assert sys.version_info >= (3, 10)" >nul 2>nul
  if not errorlevel 1 (
    py -3 run_workbench.pyw
    if errorlevel 1 pause
    exit /b
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys, tkinter; assert sys.version_info >= (3, 10)" >nul 2>nul
  if not errorlevel 1 (
    python run_workbench.pyw
    if errorlevel 1 pause
    exit /b
  )
)
echo Python 3.10 or newer with Tkinter is required.
echo Install standard Python from https://www.python.org/downloads/windows/
echo Include the Python launcher and Tcl/Tk options, then run this file again.
pause
