@echo off
echo ============================================
echo  Duplicate Photo Finder - Windows Build
echo ============================================
echo.

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo Building executable...
pyinstaller --onefile --windowed ^
    --name "DuplicatePhotoFinder" ^
    --hidden-import=pillow_heif ^
    --hidden-import=PIL ^
    find_duplicates_gui.py

if errorlevel 1 (
    echo ERROR: Build failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  BUILD COMPLETE!
echo  Your .exe is in the "dist" folder:
echo    dist\DuplicatePhotoFinder.exe
echo ============================================
echo.
echo You can copy DuplicatePhotoFinder.exe anywhere
echo and run it - no Python needed!
echo.
pause
