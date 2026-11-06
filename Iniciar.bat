@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Preparando o bot pela primeira vez...
  py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python -m pip install -q -r requirements.txt
.venv\Scripts\python main.py ui
pause
