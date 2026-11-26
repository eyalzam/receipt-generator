@echo off
cd /d "%~dp0"
if not exist venv (
    echo Installing for first time...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)
start http://127.0.0.1:5050
python app.py
pause
