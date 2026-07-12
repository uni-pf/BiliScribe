@echo off
REM BiliScribe CLI entry point (Windows)
REM Usage: biliscribe <BV号/链接> [options]
REM 
REM This script locates its own directory and invokes transcribe.py,
REM so you can put it anywhere on your PATH.

set "SCRIPT_DIR=%~dp0"
set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"

REM Try project venv first, then system python, then python3
if not exist "%PYTHON%" set "PYTHON=python"
if not exist "%PYTHON%" set "PYTHON=python3"

"%PYTHON%" "%SCRIPT_DIR%scripts\transcribe.py" %*
