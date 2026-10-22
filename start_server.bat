@echo off
REM BB10 Assistant server autostart (Task Scheduler, at logon)
REM Idempotent: if 8080 is already listening, do nothing.
netstat -ano | findstr "0.0.0.0:8080" | findstr LISTENING >nul && exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:BB10_WHISPER_MODEL='base'; $env:BB10_WHISPER_LANG='auto'; $env:BB10_WHISPER_BEAM='1'; $env:BB10_WATCHDOG='1'; $env:BB10_WATCHDOG_IPS='192.168.0.140,192.168.0.166,192.168.0.151'; Start-Process -FilePath 'E:\KimiData\daimon-share\daimon\runtime\python\.venv\Scripts\python.exe' -ArgumentList '-u','server.py','--port','8080' -WorkingDirectory 'E:\bb10-assistant-server' -RedirectStandardOutput 'E:\bb10-assistant-server\server_live.log' -RedirectStandardError 'E:\bb10-assistant-server\server_live.err' -WindowStyle Hidden"
exit /b 0
