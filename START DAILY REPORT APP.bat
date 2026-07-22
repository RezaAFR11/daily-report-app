@echo off
title Daily Report App v2 - PT. GPA
cd /d "%~dp0"

echo.
echo  ============================================
echo   PT. GARUDA PRIMA AKSARA
echo   Daily Report Generator  v2
echo  ============================================
echo.

:: ---- Step 1: Find Python -----------------------------------
set PYTHON=

python --version >nul 2>&1
if not errorlevel 1 ( set PYTHON=python & goto check_pip )

py --version >nul 2>&1
if not errorlevel 1 ( set PYTHON=py & goto check_pip )

:: Add all common Python install locations
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python313;%LOCALAPPDATA%\Programs\Python\Python313\Scripts"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python310;%LOCALAPPDATA%\Programs\Python\Python310\Scripts"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python39;%LOCALAPPDATA%\Programs\Python\Python39\Scripts"
set "PATH=%PATH%;C:\Python313;C:\Python312;C:\Python311;C:\Python310;C:\Python39"
set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WindowsApps"
set "PATH=%PATH%;%PROGRAMFILES%\Python313;%PROGRAMFILES%\Python312;%PROGRAMFILES%\Python311"

:: Also check Anaconda / Miniconda
set "PATH=%PATH%;%USERPROFILE%\anaconda3;%USERPROFILE%\anaconda3\Scripts"
set "PATH=%PATH%;%USERPROFILE%\miniconda3;%USERPROFILE%\miniconda3\Scripts"
set "PATH=%PATH%;%LOCALAPPDATA%\Continuum\anaconda3;%LOCALAPPDATA%\Continuum\anaconda3\Scripts"

python --version >nul 2>&1
if not errorlevel 1 ( set PYTHON=python & goto check_pip )

py --version >nul 2>&1
if not errorlevel 1 ( set PYTHON=py & goto check_pip )

:: ---- Python not found - download and install ---------------
echo  Python is not installed. Downloading Python 3.11...
echo  (One-time setup - please wait about 2-3 minutes)
echo.

set "PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
set "PY_INST=%TEMP%\python_setup.exe"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Write-Host '  Downloading Python 3.11...' ; ^
   try { Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INST%' -UseBasicParsing; Write-Host '  Download complete.' } ^
   catch { Write-Host ('  ERROR: ' + $_.Exception.Message); exit 1 }"

if errorlevel 1 (
    echo.
    echo  ERROR: Could not download Python automatically.
    echo  Please install Python manually: https://www.python.org/downloads/
    echo  IMPORTANT: Check "Add Python to PATH" during install, then re-run this file.
    echo.
    pause
    exit /b 1
)

echo  Installing Python 3.11 - please wait...
"%PY_INST%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1
del "%PY_INST%" >nul 2>&1

:: Reload PATH from registry
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v PATH 2^>nul') do set "UPATH=%%B"
if defined UPATH set "PATH=%PATH%;%UPATH%"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts"

python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Python was installed but this window needs to be restarted.
    echo  Please CLOSE this window and run START DAILY REPORT APP.bat again.
    echo.
    pause
    exit /b 1
)
set PYTHON=python
echo  Python installed successfully!
echo.

:: ---- Step 2: Check pip is available -------------------------
:check_pip
echo  Python found: & %PYTHON% --version
echo.

%PYTHON% -m pip --version >nul 2>&1
if errorlevel 1 (
    echo  pip not found - attempting to install pip...
    %PYTHON% -m ensurepip --upgrade >nul 2>&1
    if errorlevel 1 (
        echo  Downloading pip installer...
        powershell -NoProfile -ExecutionPolicy Bypass -Command ^
          "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%TEMP%\get-pip.py' -UseBasicParsing"
        %PYTHON% "%TEMP%\get-pip.py" --quiet
        del "%TEMP%\get-pip.py" >nul 2>&1
    )
)

:: ---- Step 3: Install required packages ----------------------
:check_packages
%PYTHON% -c "import flask, reportlab, PIL" >nul 2>&1
if not errorlevel 1 goto launch

echo  Installing required packages (first-time only)...
echo  This may take 1-2 minutes on first run.
echo.

%PYTHON% -m pip install --upgrade pip --quiet --disable-pip-version-check
%PYTHON% -m pip install flask reportlab pillow --no-warn-script-location

echo.
%PYTHON% -c "import flask, reportlab, PIL" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Package installation failed.
    echo  Possible fixes:
    echo    1. Right-click "START DAILY REPORT APP.bat" and choose "Run as administrator"
    echo    2. Check your internet connection
    echo    3. Temporarily disable antivirus and try again
    echo.
    pause
    exit /b 1
)

echo  All packages ready!
echo.

:: ---- Step 4: Open firewall port (silent) --------------------
:launch
netsh advfirewall firewall show rule name="Daily Report App" >nul 2>&1
if errorlevel 1 (
    netsh advfirewall firewall add rule name="Daily Report App" dir=in action=allow protocol=TCP localport=5050 >nul 2>&1
)

:: ---- Step 5: Launch app ------------------------------------
echo  Starting app...
echo  This PC  :  http://localhost:5050
echo  Network  :  http://YOUR-PC-IP:5050  (exact IP shown after app starts)
echo.
echo  The browser will open automatically in a few seconds.
echo  To stop the app: close this window or press Ctrl+C
echo.

%PYTHON% daily_report_app.py
pause
