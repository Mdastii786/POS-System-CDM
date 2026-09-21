@echo off
title ConvergEX POS - Share on Wi-Fi
cd /d "%~dp0"
echo ==============================================
echo   ConvergEX POS  -  SHARE ON WI-FI (one time)
echo ==============================================
echo.
echo  Run this ONCE on the MAIN TILL laptop (the one
echo  with ConvergEXPOS.exe and tuckshop.db).
echo.
echo  IMPORTANT: right-click this file and choose
echo  "Run as administrator", or it cannot open the
echo  firewall.
echo.
netsh advfirewall firewall delete rule name="ConvergEX POS 5000" >nul 2>&1
netsh advfirewall firewall add rule name="ConvergEX POS 5000" dir=in action=allow protocol=TCP localport=5000 profile=private
if errorlevel 1 (
  echo.
  echo  XXX FAILED - you must right-click this file and
  echo  choose "Run as administrator", then try again.
  echo.
  pause
  exit /b 1
)
echo.
echo  DONE! The firewall now allows other laptops on
echo  your private shop Wi-Fi to reach this app.
echo.
echo  NEXT STEPS (do these once the app is running):
echo.
echo  1. Start ConvergEXPOS.exe on THIS laptop and look
echo     at the login screen - it shows an address like:
echo        http://192.168.1.50:5000
echo  2. On the OTHER laptop (same Wi-Fi), open Chrome
echo     or Edge and type that exact address.
echo  3. Log in with that person's PIN (teller = teller
echo     account). Both laptops now work on THE SAME data.
echo.
echo  Remember: THIS main laptop must stay on with the
echo  app open, or the other laptop loses connection.
echo.
pause
