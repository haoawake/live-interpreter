@echo off
setlocal
cd /d "%~dp0"
echo === Build: dev environment + models + exe ===

if exist ".venv\Scripts\python.exe" goto deps
where uv >nul 2>nul
if %errorlevel%==0 (
    uv venv --python 3.12 .venv || goto fail
) else (
    python -m venv .venv || goto fail
)

:deps
where uv >nul 2>nul
if %errorlevel%==0 (
    uv pip install --python .venv\Scripts\python.exe -r requirements.txt pyinstaller pillow || goto fail
) else (
    .venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller pillow || goto fail
)

.venv\Scripts\python.exe setup_models.py || goto fail
.venv\Scripts\python.exe build.py || goto fail

echo.
echo Done.
pause
exit /b 0

:fail
echo.
echo Build FAILED - see the messages above.
pause
exit /b 1
