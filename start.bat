@echo off
cd /d %~dp0
if not exist .venv (
    echo Tworzenie virtualenv...
    py -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
)
echo Uruchamiam Zamek Zlej Krolowej na http://localhost:8003
.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8003 --reload
pause
