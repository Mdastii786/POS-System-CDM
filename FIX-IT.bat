@echo off
title ConvergEX POS - FIX-IT
cd /d "%~dp0"
echo ==============================================
echo   ConvergEX POS  -  ONE-CLICK FIX-IT
echo ==============================================
echo.
echo  This closes any stuck old copies, rebuilds the
echo  app from the latest files, and starts it fresh.
echo  Takes about a minute. Just let it run.
echo.

echo [1/4] Closing any old copies of the app...
taskkill /F /IM ConvergEXPOS.exe >nul 2>&1
taskkill /F /IM TuckshopPOS.exe >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter 'Name like ''python%%''' -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*app.py*' } | Stop-Process -Force -ErrorAction SilentlyContinue" >nul 2>&1
echo      done.
echo.

echo [2/4] Checking the files are in the right place...
if not exist app.py goto wrongplace
if not exist templates\stock.html goto wrongplace
if not exist static\common.js goto wrongplace
echo      done.
echo.

echo [3/4] Building ConvergEXPOS.exe (please wait)...
set "PYCMD=py -3.12"
%PYCMD% --version >nul 2>&1
if errorlevel 1 set "PYCMD=py"
%PYCMD% --version >nul 2>&1
if errorlevel 1 set "PYCMD=python"
%PYCMD% --version >nul 2>&1
if errorlevel 1 goto nopython
echo      Using: %PYCMD%
%PYCMD% -m pip install flask pywebview openpyxl pyinstaller >fix_log.txt 2>&1
if errorlevel 1 goto pipfail
%PYCMD% -m PyInstaller --noconfirm --clean --onefile --windowed --name ConvergEXPOS --add-data "templates;templates" --add-data "static;static" --collect-all webview app.py >>fix_log.txt 2>&1
if errorlevel 1 goto buildfail
if not exist dist\ConvergEXPOS.exe goto buildfail
echo      done.
echo.

echo [4/4] Making sure your data sits next to the exe...
if exist tuckshop.db if not exist dist\tuckshop.db copy /y tuckshop.db dist\ >nul
echo      done.
echo.
echo ==============================================
echo   SUCCESS!  Starting ConvergEX POS now.
echo   Login:  manager / 1357  (or your new PIN)
echo.
echo   From now on, just double-click:
echo   dist\ConvergEXPOS.exe
echo ==============================================
start "" "%CD%\dist\ConvergEXPOS.exe"
pause
exit /b 0

:wrongplace
echo.
echo  XXX PROBLEM: app.py / templates / static are NOT in this folder:
echo      %CD%
echo.
echo  The zip was probably extracted into a sub-folder.
echo  FIX: open tuckshop-deploy.zip and drag EVERYTHING
echo  straight into the DEPLOY POS folder (so app.py sits
echo  next to this FIX-IT.bat), then double-click FIX-IT
echo  again.
echo.
pause
exit /b 1

:nopython
echo.
echo  XXX PROBLEM: Python was not found on this PC.
echo  FIX: run:  py install 3.12
echo  (or install Python 3.12 from python.org, ticking
echo  "Add python.exe to PATH"), then run FIX-IT again.
echo.
pause
exit /b 1

:pipfail
echo.
echo  XXX PROBLEM: Could not install the components
echo  (flask / pywebview / openpyxl / pyinstaller).
echo  Check the internet connection, then run FIX-IT again.
echo  I'm opening fix_log.txt - take a PHOTO of it and
echo  send it to me if it keeps failing.
start notepad fix_log.txt
echo.
pause
exit /b 1

:buildfail
echo.
echo  XXX PROBLEM: The build failed.
echo  I'm opening fix_log.txt - take a PHOTO of the bottom
echo  of that file and send it to me.
start notepad fix_log.txt
echo.
pause
exit /b 1
