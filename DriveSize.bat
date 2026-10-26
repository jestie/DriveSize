@echo off
title DriveSize
python "%~dp0drivesize.py" %*
if errorlevel 1 pause
