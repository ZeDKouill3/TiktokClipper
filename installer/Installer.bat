@echo off
rem Installe Clipper (SPEC-54ed21c61caf). Double-clic, ou depuis un terminal
rem avec des options : Installer.bat --app <dossier> --data <dossier> [--cpu
rem | --cuda] [--sans-console] [--sans-raccourci] [--dry-run]. Toutes les
rem options sont transmises telles quelles a installer\install.ps1.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1" %*
set EXIT_CODE=%errorlevel%
rem I4 : en double-clic, cmd fermerait la fenetre (donc le message rouge)
rem moins d'une seconde apres un echec ; cette pause ne gene jamais un appel
rem depuis un terminal ou des tests (toujours en succes dans ce cas).
if not %EXIT_CODE%==0 pause
exit /b %EXIT_CODE%
