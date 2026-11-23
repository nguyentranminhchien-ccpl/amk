@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0" >nul 2>nul
echo ====================================================================
echo   CAI DAT THU VIEN PYTHON CAN THIET (opencv-python, numpy, openpyxl)
echo ====================================================================
echo.

set "PY_CMD="
where python >nul 2>nul
if %errorlevel%==0 (
    set "PY_CMD=python"
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set "PY_CMD=py"
    )
)

if "%PY_CMD%"=="" (
    echo [LOI] Khong tim thay Python trong PATH!
    echo Vui long kiem tra lai phien ban Python da cai dat tren may.
    popd >nul 2>nul
    pause
    exit /b 1
)

echo [INFO] Dang dung trinh Python:
%PY_CMD% -c "import sys; print('  ', sys.executable, sys.version.split()[0])"
echo.

if exist "%~dp0requirements.txt" (
    echo [INFO] Dang cai dat cac thu vien tu requirements.txt...
    %PY_CMD% -m pip install -r "%~dp0requirements.txt"
) else (
    echo [INFO] Dang cai dat opencv-python, numpy, openpyxl...
    %PY_CMD% -m pip install opencv-python numpy openpyxl
)

if %errorlevel% neq 0 (
    echo.
    echo [LOI] Cai dat that bai!
    echo Neu may khoa khong co mang Internet, vui long chup anh ket qua
    echo lenh "python --version" de duoc dong goi bo cai offline.
    popd >nul 2>nul
    pause
    exit /b 1
)

echo.
echo [INFO] Kiem tra lai cac thu vien da cai...
%PY_CMD% -c "import cv2, numpy, openpyxl; print('[OK] cv2=' + cv2.__version__, '| numpy=' + numpy.__version__, '| openpyxl=' + openpyxl.__version__)"
if %errorlevel% neq 0 (
    echo [LOI] Thu vien chua nap duoc sau khi cai dat.
    popd >nul 2>nul
    pause
    exit /b 1
)

echo.
echo ====================================================================
echo   HOAN TAT! Bay gio ban co the chay run_menu.py hoac cac file .py.
echo ====================================================================
popd >nul 2>nul
pause
