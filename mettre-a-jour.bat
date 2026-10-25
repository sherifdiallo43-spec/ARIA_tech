@echo off
rem Mise a jour d'ARIA_point : glisser le fichier ARIA_point_X.Y.Z.zip sur ce fichier.
cd /d "%~dp0"

echo === ARIA_point - Mise a jour ===
echo.

if "%~1"=="" (
    echo Glisse le fichier ARIA_point_X.Y.Z.zip recu d'ARIA_tech sur ce fichier.
    echo.
    pause
    exit /b 1
)

rem Tout sur UNE ligne : la mise a jour peut remplacer ce fichier pendant qu'il tourne,
rem et cmd relit un .bat au fur et a mesure. Une ligne deja lue n'est plus relue.
.venv\Scripts\python main.py mettre-a-jour "%~1" & echo. & pause & exit /b
