<#
.SYNOPSIS
    Enchaine tools/build_portable.py (SPEC-54ed21c61caf R1) : construit la
    wheel puis Clipper-portable-<version>.zip dans dist/.

.DESCRIPTION
    Facultatif (R1) : un simple relais vers 'python tools/build_portable.py',
    pour lancer la construction sans taper la commande Python complete.
    Compatible Windows PowerShell 5.1 (pas de && ni ??). A lancer depuis
    n'importe ou : le chemin du depot est deduit de l'emplacement du script.
    Toutes les options sont transmises telles quelles a build_portable.py.
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

python "$RepoRoot\tools\build_portable.py" @args
exit $LASTEXITCODE
