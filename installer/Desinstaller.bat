@echo off
rem Desinstalle Clipper (SPEC-38f7761891f6 R8). Double-clic, ou depuis un
rem terminal avec des options : Desinstaller.bat [--app <dossier>] [--donnees]
rem [--dry-run]. Toutes les options sont transmises telles quelles a
rem installer\desinstaller.ps1.
rem
rem Audit 10/10 I1 : ce fichier est la copie faite dans app par install.ps1
rem (le chemin documente), et app est supprime par le script qu'il appelle.
rem Deux consequences, mesurees (tests/test_installer.py, test_a1010_i1) :
rem  - cmd.exe lance par double-clic a app pour dossier courant, et Windows
rem    refuse de supprimer le dossier courant d'un processus ("en cours
rem    d'utilisation") : on en sort d'abord (cd /d %TEMP%) ;
rem  - cmd relit un .bat ligne a ligne pendant son execution : une fois app
rem    supprime, la ligne qui suit l'appel PowerShell est lue dans un fichier
rem    disparu ("Le chemin d'acces specifie est introuvable", code 1 apres une
rem    desinstallation pourtant reussie). Le .bat se copie donc dans %TEMP% et
rem    s'y enchaine (sans call : cmd ne revient jamais ici), en passant son
rem    dossier d'origine par CLIPPER_DESINSTALLER_SRC ; la copie se supprime
rem    a la fin par l'idiome (goto) 2>nul, qui clot le contexte du .bat avant
rem    le del. Le nom de la copie (%RANDOM% : un nouveau tirage a CHAQUE
rem    expansion) est fige dans une variable une ligne avant d'etre utilise.
if not defined CLIPPER_DESINSTALLER_SRC set "CLIPPER_DESINSTALLER_COPIE=%TEMP%\Clipper-Desinstaller-%RANDOM%.bat"
if not defined CLIPPER_DESINSTALLER_SRC (cd /d "%TEMP%" & set "CLIPPER_DESINSTALLER_SRC=%~dp0" & copy /y "%~f0" "%CLIPPER_DESINSTALLER_COPIE%" >nul && "%CLIPPER_DESINSTALLER_COPIE%" %*)
powershell -NoProfile -ExecutionPolicy Bypass -File "%CLIPPER_DESINSTALLER_SRC%installer\desinstaller.ps1" %*
set EXIT_CODE=%errorlevel%
set CLIPPER_DESINSTALLER_SRC=
set CLIPPER_DESINSTALLER_COPIE=
rem I4 : en double-clic, cmd fermerait la fenetre (donc le message rouge)
rem moins d'une seconde apres un echec ; cette pause ne gene jamais un appel
rem depuis un terminal ou des tests (toujours en succes dans ce cas).
if not %EXIT_CODE%==0 pause
(goto) 2>nul & del "%~f0" & exit /b %EXIT_CODE%
