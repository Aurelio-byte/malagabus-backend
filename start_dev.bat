@echo off
setlocal
cd /d "%~dp0"

REM (1) Activar entorno virtual si existe (opcional)
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
    echo [start_dev] Entorno venv activado.
) else if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
    echo [start_dev] Entorno .venv activado.
)

REM (2) Instalar dependencias si faltan
pip show fastapi >nul 2>&1
if errorlevel 1 (
    echo [start_dev] Instalando requirements...
    pip install -r requirements.txt
) else (
    echo [start_dev] Dependencias ya presentes.
)

REM (3) Lanzar API en puerto 5070
echo [start_dev] Arrancando MalagaBus API en http://localhost:5070
python run.py
