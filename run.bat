@echo off
rem RefTrack launcher - creates venv on first run, then starts the server.
cd /d "%~dp0"
if not exist .venv (
  echo First run: creating virtual environment...
  python -m venv .venv
  .venv\Scripts\pip install -r requirements.txt
)
echo Starting RefTrack at http://127.0.0.1:8377 ...
start "" http://127.0.0.1:8377
.venv\Scripts\python -m uvicorn reftrack.main:app --port 8377
