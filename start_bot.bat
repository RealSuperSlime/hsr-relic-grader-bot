@echo off
title HSR Relic Grader - Discord bot
cd /d "%~dp0"

rem ignore any PYTHONPATH/PYTHONHOME set on the machine: this project has its own venv
set "PYTHONPATH="
set "PYTHONHOME="

rem Use the parent folder's venv when the bot sits inside the grader checkout,
rem otherwise keep one next to the bot.
set "PY=..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo First run - creating the virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo.
    echo Could not create the venv. Is Python installed and on PATH?
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements-bot.txt
  set "PY=.venv\Scripts\python.exe"
)

"%PY%" -c "import discord, relic_grades" 2>nul
if errorlevel 1 (
  echo Installing requirements...
  "%PY%" -m pip install --quiet -r requirements-bot.txt
)

echo.
echo Starting the bot. Close this window (or press Ctrl+C) to stop it.
echo.
"%PY%" bot.py
echo.
echo The bot stopped.
pause
