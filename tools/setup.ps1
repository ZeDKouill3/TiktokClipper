<#
.SYNOPSIS
    Installe clipper : verifie les prerequis, cree .venv, installe le paquet
    et lance la suite de tests.

.DESCRIPTION
    Compatible Windows PowerShell 5.1 (pas de && ni ??). A lancer depuis
    n'importe ou : le chemin du depot est deduit de l'emplacement du script.
    S'arrete (exit non nul) au premier prerequis obligatoire manquant, avec
    un message qui dit quoi installer. Le GPU (nvidia-smi) est facultatif :
    son absence n'est qu'une information, clipper tourne sur CPU sans lui.
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Fail($message) {
    Write-Host "[setup] ERREUR : $message" -ForegroundColor Red
    exit 1
}

function Test-Command($name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    return ($null -ne $cmd)
}

Write-Host "[setup] Verification des prerequis..."

if (-not (Test-Command "uv")) {
    Fail "uv introuvable dans le PATH. Installe-le (https://docs.astral.sh/uv/) puis relance ce script."
}

$pythonVersionOutput = & uv python find "3.11"
if ($LASTEXITCODE -ne 0) {
    Fail "Python 3.11 introuvable via uv. Installe-le avec 'uv python install 3.11' puis relance ce script."
}
Write-Host "[setup] Python 3.11 : $pythonVersionOutput"

if (-not (Test-Command "ffmpeg")) {
    Fail "ffmpeg introuvable dans le PATH (necessaire pour l'audio, le rendu et le controle qualite)."
}

if (-not (Test-Command "claude")) {
    Fail "Claude Code CLI ('claude') introuvable dans le PATH (backend LLM par defaut, ADR-b1c1)."
}

if (Test-Command "nvidia-smi") {
    Write-Host "[setup] GPU NVIDIA detecte :"
    & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
} else {
    Write-Host "[setup] Pas de nvidia-smi : le pipeline tournera sur CPU (plus lent, voir ADR-fb9b)."
}

Write-Host "[setup] Creation de .venv (Python 3.11)..."
& uv venv --python 3.11
if ($LASTEXITCODE -ne 0) {
    Fail "'uv venv' a echoue."
}

Write-Host "[setup] Installation du paquet (pip seul casserait l'override OpenCV, voir AGENTS.md)..."
& uv pip install -e ".[test]"
if ($LASTEXITCODE -ne 0) {
    Fail "'uv pip install -e .[test]' a echoue."
}

$env:Path = "$RepoRoot\.venv\Scripts;" + $env:Path

if (-not (Test-Command "ank")) {
    Write-Host "[setup] Avertissement : ank introuvable dans le PATH. Il n'est utile qu'aux agents (claim/log/done sur les taches de ce depot), pas pour utiliser clipper ; installe-le depuis https://github.com/haksolot/ank (binaire de release) si tu travailles sur les taches."
}

Write-Host "[setup] Verification des imports..."
& python -c "import clipper; import fastapi; import uvicorn; import faster_whisper; import mediapipe; import scenedetect"
if ($LASTEXITCODE -ne 0) {
    Fail "Une dependance du paquet clipper ne s'importe pas apres l'installation."
}

Write-Host "[setup] Verification de playwright (profils de navigateur, publier sur TikTok)..."
& python -c "import playwright.sync_api"
if ($LASTEXITCODE -ne 0) {
    Fail "La dependance playwright ne s'importe pas : relance 'uv pip install -e .[test]' (elle est declaree dans pyproject.toml)."
}

# Chrome n'est necessaire que pour publier sur TikTok / exporter les cookies YouTube
# (clipper browser login) : son absence n'arrete pas l'installation du pipeline, mais
# elle est signalee clairement, jamais ignoree.
$chromeCandidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)
$chrome = $chromeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($chrome) {
    Write-Host "[setup] Google Chrome : $chrome"
} else {
    Write-Host "[setup] ATTENTION : Google Chrome introuvable. Il est necessaire pour 'clipper browser login' (publier sur TikTok, cookies YouTube). Installe Google Chrome (https://www.google.com/chrome/) ou lance 'playwright install chrome', puis relance ce script." -ForegroundColor Yellow
}

Write-Host "[setup] Lancement de la suite de tests..."
& pytest
if ($LASTEXITCODE -ne 0) {
    Fail "pytest a echoue : voir la sortie ci-dessus."
}

Write-Host "[setup] OK : environnement pret. Copie config.example.toml vers config.toml puis lance 'python -m clipper'." -ForegroundColor Green
