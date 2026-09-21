@echo off
REM ============================================================
REM  ConvergEX POS - build script
REM  Run this ON the Windows till machine by double-clicking it.
REM  Needs Python 3.10+ from python.org (tick "Add python.exe to
REM  PATH" during install).
REM ============================================================

echo [1/2] Installing what's needed (one time)...
py -m pip install --upgrade pip
py -m pip install flask pywebview openpyxl pyinstaller
if errorlevel 1 (
  echo.
  echo FAILED: Python not found. Install Python from python.org first,
  echo and tick "Add python.exe to PATH" during installation.
  pause
  exit /b 1
)

echo [2/2] Building ConvergEXPOS.exe...
py -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name ConvergEXPOS ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --collect-all webview ^
  app.py
if errorlevel 1 (
  echo.
  echo BUILD FAILED - scroll up to see the error.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  SUCCESS! Your program is: dist\ConvergEXPOS.exe
echo.
echo  Copy dist\ConvergEXPOS.exe anywhere (Desktop, USB stick...)
echo  and double-click it. On first run it creates tuckshop.db,
echo  backups\, archives\ and exports\ right next to the exe.
echo ============================================================
pause
