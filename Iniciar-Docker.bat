@echo off
cd /d "%~dp0"
docker compose up -d --build
if errorlevel 1 (
  echo Nao foi possivel iniciar. Veja se o Docker Desktop esta aberto.
  pause
  exit /b 1
)
timeout /t 5 >nul
start "" http://127.0.0.1:8765
