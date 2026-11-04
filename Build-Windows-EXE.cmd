@echo off
setlocal
cd /d "%~dp0"
echo This optional build installs PyInstaller into a local virtual environment.
py -3 -m venv .build-venv
if errorlevel 1 goto failed
.build-venv\Scripts\python.exe -m pip install pyinstaller
if errorlevel 1 goto failed
.build-venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name RGH-FPS-Workbench --add-data "recipes;recipes" --add-data "catalog;catalog" run_workbench.pyw
if errorlevel 1 goto failed
echo Built dist\RGH-FPS-Workbench.exe. XexTool remains a separate dependency.
pause
exit /b 0
:failed
echo Build failed. Read the error above. The source launcher is still available.
pause
exit /b 1
