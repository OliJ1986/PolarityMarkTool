@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM  build.bat  —  PolarityMark EXE builder
REM  Futtatás: dupla kattintás vagy cmd-ből:  build.bat
REM ─────────────────────────────────────────────────────────────────────────

setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo  PolarityMark – PyInstaller build
echo ============================================================
echo.

REM Activate virtual environment
if not exist ".venv\Scripts\activate.bat" (
    echo [HIBA] .venv nem talalhato! Futtasd elobb: python -m venv .venv
    pause & exit /b 1
)
call .venv\Scripts\activate.bat

REM Check PyInstaller
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] PyInstaller telepitese...
    pip install pyinstaller --quiet
)

REM Clean previous build
echo [INFO] Torlom az elozo buildet...
if exist "build" rmdir /s /q "build"
if exist "dist\PolarityMark" rmdir /s /q "dist\PolarityMark"

REM Run build
echo [INFO] Build indul...
echo.
pyinstaller PolarityMark.spec --clean

if errorlevel 1 (
    echo.
    echo [HIBA] Build sikertelen! Ellenorizd a fenti hibauzeneteket.
    pause & exit /b 1
)

REM Result
echo.
echo ============================================================
echo  Build kesz!
echo  Helye: dist\PolarityMark\PolarityMark.exe
echo ============================================================
echo.

REM Show size
for /f "tokens=*" %%i in ('powershell -command "'{0:N0} MB' -f ((Get-ChildItem dist\PolarityMark -Recurse | Measure-Object Length -Sum).Sum / 1MB)"') do set SIZE=%%i
echo  Teljes meret: %SIZE%
echo.

set /p OPEN="Megnyissuk a dist mappat? (I/n): "
if /i not "%OPEN%"=="n" explorer "dist\PolarityMark"

endlocal

