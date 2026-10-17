@echo off
setlocal
title 3DX Hybrid Desktop - Preparacao
cd /d "%~dp0"
if not exist "3DX-Hybrid-Desktop.exe" (
  echo ERRO: Executavel nao encontrado.
  echo Extraia todo o pacote ZIP antes de abrir este iniciador.
  pause
  exit /b 2
)
where py >nul 2>&1
if errorlevel 1 (
  echo Para usar a RAM do PC, instale o Python 3.11 ou superior, de 64 bits:
  echo https://www.python.org/downloads/windows/
  echo O laboratorio 3D podera abrir, mas o processador auxiliar exige Python.
  pause
) else (
  py -3 -c "import numpy, PIL" >nul 2>&1
  if errorlevel 1 (
    echo Instalando bibliotecas gratuitas para o auxiliar do Windows...
    py -3 -m pip install --user numpy pillow
  )
)
start "" "%~dp03DX-Hybrid-Desktop.exe"
exit /b 0
