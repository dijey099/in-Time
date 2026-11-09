@echo off
color 0a

title in-Time - Relay build
mode con:cols=100 lines=30
cls
cd /d "%~dp0"
REM All build options (bundled files, skipped Qt plugins, icon) are in relay.spec.
REM Build from a Python environment that only has requirements.txt + pyinstaller:
REM PyInstaller bundles every package it can import.
pyinstaller --clean --noconfirm relay.spec

pause
