@echo off
cd /d C:\Users\DELL\Documents\pointage-scolaire

echo === ARIA_point - Preparation de la demo ===
echo.

rem Securite : la demo ne doit jamais envoyer de vrais messages ni de vrais emails.
.venv\Scripts\python -c "import sys; from pointage.config import charger; c = charger(); sys.exit(c.notifications['mode'] != 'console' or c.directeur.get('mode', 'console') != 'console')"
if errorlevel 1 (
    echo ARRET : [notifications] ou [directeur] pas en mode "console" dans config.toml.
    echo La demo enverrait de vrais messages. Remets mode = "console" puis relance.
    goto fin
)

echo [1/3] Simulation des pointages...
.venv\Scripts\python main.py simuler --n 9 --profs 4
if errorlevel 1 goto erreur
echo.

echo [2/3] Synchronisation...
.venv\Scripts\python main.py sync
if errorlevel 1 goto erreur
echo.

echo [3/3] Generation du rapport PDF...
.venv\Scripts\python main.py rapport
if errorlevel 1 goto erreur
echo.

for /f "tokens=*" %%i in ('.venv\Scripts\python -c "from datetime import date; print(date.today())"') do set TODAY=%%i
start rapports\journaliers\rapport_%TODAY%.pdf

echo Demo prete ! Le PDF s'ouvre...
goto fin

:erreur
echo.
echo ARRET : l'etape ci-dessus a echoue, la demo n'est pas complete.

:fin
echo.
pause
