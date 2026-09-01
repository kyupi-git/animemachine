@echo off
setlocal
chcp 65001 >nul
title AnimeMachine Web
set "PWSH=C:\Program Files\PowerShell\7\pwsh.exe"
if not exist "%PWSH%" set "PWSH=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
"%PWSH%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0AnimeMachine.ps1" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo AnimeMachine failed to start. Review the error above.
  pause
)
exit /b %CODE%
