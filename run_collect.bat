@echo off
REM Collecte Chronicle - point d'entree de la tache planifiee.
REM
REM Pourquoi un .bat et pas "python main.py collect" directement dans le
REM planificateur : une tache planifiee demarre dans C:\Windows\System32,
REM sans l'environnement d'un terminal. Il faut donc TOUT expliciter :
REM le repertoire de travail, l'interpreteur du venv, et le code de sortie.

REM /d : change aussi de lecteur si besoin.
cd /d "%~dp0"

REM Le conteneur PostgreSQL peut etre arrete (PC redemarre). Le demarrer
REM est idempotent : s'il tourne deja, la commande ne fait rien.
REM Si Docker Desktop lui-meme est eteint, collect echouera proprement
REM avec "Base injoignable" dans le log.
docker compose up -d >nul 2>&1

".venv\Scripts\python.exe" main.py collect

REM Propager le code de sortie : c'est lui qui alimente la colonne
REM "Dernier resultat de l'execution" du planificateur Windows.
REM 0 = tout va bien, 1 = au moins un endpoint en echec.
exit /b %ERRORLEVEL%
