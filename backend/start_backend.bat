@echo off
setlocal
cd /d "%~dp0"
set HOST=127.0.0.1
set PORT=8788
python server.py
endlocal
