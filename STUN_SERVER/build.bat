@echo off
REM build.bat - Script para construir executável do STUN Relay Server no Windows

REM 1. Ativar o ambiente virtual (venv)
IF EXIST "venv\Scripts\activate.bat" (
    echo Ativando venv...
    call "venv\Scripts\activate.bat"
) ELSE (
    echo Ambiente virtual nao encontrado em venv\, criando um...
    python -m venv venv
    call "venv\Scripts\activate.bat"
)

REM 2. Instalar/atualizar pip e PyInstaller
echo Instalando/atualizando pip e PyInstaller...
python -m ensurepip --default-pip
python -m pip install --upgrade pip
python -m pip install pyinstaller

REM 3. Limpar builds anteriores
echo Limpando builds anteriores...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%~n1.spec" del /q "%~n1.spec"
if exist __pycache__ rmdir /s /q __pycache__

REM 4. Executar PyInstaller para criar o executavel
echo Gerando executavel com PyInstaller...
python -m PyInstaller --onefile --name stun_relay --log-level WARN main.py

IF %ERRORLEVEL% NEQ 0 (
    echo Erro durante o build.
    pause
    exit /b %ERRORLEVEL%
)

echo Build concluido com sucesso! Executavel em \dist\stun_relay.exe
pause