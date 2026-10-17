@echo off
setlocal EnableExtensions
title 3DX Hybrid Lite 2 - Preparacao
echo ===================================================
echo   3DX HYBRID LITE 2 - INTERFACE WINDOWS 10
echo ===================================================
set "DIR=%~dp0"
where py >nul 2>&1
if errorlevel 1 (
  echo Python 3.11+ nao encontrado. Instale Python 64 bits:
  echo https://www.python.org/downloads/windows/
  pause
  exit /b 2
)
py -3 -c "import sys; assert sys.version_info >= (3,11)" >nul 2>&1
if errorlevel 1 (
  echo Atualize para Python 3.11 ou superior.
  pause
  exit /b 2
)
if not exist "%DIR%hybrid_gui.py" (
  echo Arquivo hybrid_gui.py nao encontrado na mesma pasta.
  echo Extraia TODOS os arquivos do ZIP antes de executar.
  pause
  exit /b 3
)
if not exist "%DIR%hybrid_lite.py" (
  echo Arquivo hybrid_lite.py nao encontrado na mesma pasta.
  echo Extraia TODOS os arquivos do ZIP antes de executar.
  pause
  exit /b 3
)
py -3 -c "import numpy,PIL" >nul 2>&1
if errorlevel 1 (
  echo Instalando bibliotecas gratuitas de imagens...
  py -3 -m pip install --user numpy pillow
  if errorlevel 1 (
    echo Falha ao instalar dependencias. Verifique sua internet.
    pause
    exit /b 4
  )
)
echo Abrindo janela profissional do 3DX...
py -3 "%DIR%hybrid_gui.py"
if errorlevel 1 (
  echo O aplicativo encerrou com erro; envie uma foto desta janela.
  pause
)
