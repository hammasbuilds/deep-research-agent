@echo off
REM Start the deep-research-agent web page on http://127.0.0.1:8845/ (change with --port).
REM Add --no-browser to start it without opening a browser window.
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
uv run --no-project python -m research.web %*
