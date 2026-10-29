@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
set "TASK_PYTHON=python"
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "TASK_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
"%TASK_PYTHON%" --version
if errorlevel 1 goto no_python
"%TASK_PYTHON%" -c "import PIL, img2pdf, pypdf" >nul 2>&1
if errorlevel 1 (
    "%TASK_PYTHON%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 goto install_failed
)
"%TASK_PYTHON%" "%~dp0sslibrary_to_pdf.py"
pause
exit /b
:no_python
echo Please install Python 3.10+ and enable Add Python to PATH.
pause
exit /b 1
:install_failed
echo Dependency installation failed. Check your network and Python environment.
pause
exit /b 1
