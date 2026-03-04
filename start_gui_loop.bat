@echo off
set "BASE_PATH=C:\Users\Administrador\Documents\DisparadorWhatsapp"

REM Este script inicia APENAS o frontend Python (GUI) num loop.
REM O backend Node.js deve ser gerido pelo NSSM.

:loop_py
echo Iniciando Frontend Python (GUI)...
cd /d %BASE_PATH%
py app_sender_pro.py

echo Aplicativo Python fechou. Reiniciando em 5 segundos...
timeout /t 5
goto loop_py