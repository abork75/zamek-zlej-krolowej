@echo off
cd /d %~dp0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8003 " ^| findstr "LISTENING"') do (
    echo Ubijam poprzedni proces na porcie 8003 [PID %%p]...
    taskkill /PID %%p /F >nul 2>&1
)
if not exist .venv (
    echo Tworzenie virtualenv...
    py -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
)
set NGROK=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe
if exist "%NGROK%" (
    echo Uruchamiam ngrok...
    start "ngrok" "%NGROK%" http 8003
    timeout /t 2 /nobreak >nul
)
echo Uruchamiam Zamek Zlej Krolowej na http://localhost:8003
.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8003 --reload
pause
