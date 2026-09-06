@echo off
chcp 65001 >nul
cd /d %~dp0
python -m pip install -q mss soundcard scipy numpy pillow opencv-python-headless rapidocr_onnxruntime
python autofish.py
pause
