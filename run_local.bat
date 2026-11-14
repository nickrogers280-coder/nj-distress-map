@echo off
REM Run the tracker on a Windows PC (use with Task Scheduler if GitHub Actions is blocked).
REM Reads email settings from a .env file in this folder.
cd /d "%~dp0"
if exist .env (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do set "%%A=%%B"
)
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\pip install -r requirements.txt
)
.venv\Scripts\python run.py %*
