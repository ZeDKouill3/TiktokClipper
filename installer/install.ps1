<#
.SYNOPSIS
    Installe Clipper pour un utilisateur sans outil de developpement : Python,
    ffmpeg, claude, modeles, lanceur (SPEC-38f7761891f6 R2, R3 ; ADR-e1dac9ba2284).

.DESCRIPTION
    Compatible Windows PowerShell 5.1 (pas de &&, ni ??, ni operateur
    ternaire), sans elevation. Appele par Installer.bat, qui transmet les
    options telles quelles. Options (style GNU, pas les parametres nommes
    PowerShell) : --app <dossier>, --data <dossier>, --cpu, --cuda,
    --sans-console, --sans-raccourci (aucun raccourci Clipper.lnk sur le
    Bureau), --dry-run.

    --dry-run traverse exactement le meme code de decision que l'installation
    reelle (une fonction par etape, qui recoit -DryRun) : il affiche le plan
    (chemins resolus, decisions CPU/CUDA, premiere installation ou mise a
    jour...) sans rien ecrire ni telecharger. C'est le mode utilise par les
    tests (tests/test_installer.py).
#>

param(
    # Point d'injection pour les tests (dot-sourcing direct des fonctions
    # ci-dessous, puis appel isole d'une fonction Invoke-StepN-...) : jamais
    # transmis par Installer.bat, jamais documente (meme principe que -Port
    # ci-dessous et dans desinstaller.ps1).
    [switch]$NoAutoRun,
    # Port de la console Clipper a verifier avant une mise a jour (R6) :
    # jamais une option publique (Installer.bat ne la transmet pas),
    # seulement un point d'injection pour les tests, la vraie console
    # ecoutant toujours sur 8000.
    [int]$Port = 8000,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RawArgs = @()
)

$ErrorActionPreference = "Stop"

# --------------------------------------------------------------------------
# Constantes et petites fonctions
# --------------------------------------------------------------------------

$TOTAL_STEPS = 11

# ffmpeg Windows 64 bits (GyanD/codexffmpeg, tag de version figee "9.0.2" :
# jamais "latest" ni "master", qui bougent et perimeraient le sha256 ci-
# dessous en silence). Pour renouveler : ouvrir
# https://github.com/GyanD/codexffmpeg/releases, choisir un tag de version
# (pas "latest"), telecharger son asset "*-essentials_build.zip", calculer
# son sha256 avec `Get-FileHash -Algorithm SHA256 <fichier>`, puis remplacer
# les deux constantes par la nouvelle URL (avec le tag dans le chemin) et le
# nouveau sha256.
$FFMPEG_URL = "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip"
$FFMPEG_SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"

function Write-Step {
    param([int]$Number, [string]$Message)
    Write-Host "[$Number/$TOTAL_STEPS] $Message"
}

function Write-Detail {
    param([string]$Message)
    Write-Host "  - $Message"
}

function Fail {
    param([string]$Message, [string]$Remedy)
    Write-Host "[installer] ERREUR : $Message" -ForegroundColor Red
    if ($Remedy) {
        Write-Host "  remede : $Remedy" -ForegroundColor Red
    }
    exit 1
}

function Write-Log {
    param([string]$App, [string]$Message)
    if (-not $App) {
        return
    }
    $logPath = Join-Path $App "installer.log"
    $line = "$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') $Message"
    Add-Content -Path $logPath -Value $line
}

function Test-ConsolePortListening {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $conn)
}

function Compare-ClipperVersion {
    param([string]$A, [string]$B)
    try {
        return ([version]$A).CompareTo([version]$B)
    } catch {
        return [string]::Compare($A, $B)
    }
}

function Get-GpuDecision {
    param([switch]$Cpu, [switch]$Cuda)
    if ($Cpu) {
        return "cpu"
    }
    if ($Cuda) {
        return "cuda"
    }
    $cmd = Get-Command "nvidia-smi" -ErrorAction SilentlyContinue
    if (-not $cmd) {
        return "cpu"
    }
    # Audit 10/10 I5 : un nvidia-smi present mais en echec (pilote casse, GPU
    # retire, machine virtuelle) ecrit un message d'erreur non vide et sort
    # en code non nul ; "sortie non vide" seule le prenait pour un GPU et
    # installait l'extra [cuda] (~2 Go) pour rien (R4). Detection = code 0
    # ET au moins un nom de GPU renvoye par la requete.
    $code = 1
    $output = $null
    try {
        $output = & nvidia-smi --query-gpu=name --format=csv,noheader 2>$null
        $code = $LASTEXITCODE
    } catch {
        $script:GpuDetail = "nvidia-smi present mais injoignable : $($_.Exception.Message)"
        return "cpu"
    }
    $text = ($output | Out-String).Trim()
    if ($code -ne 0) {
        $script:GpuDetail = "nvidia-smi present mais en echec (code $code) : aucun GPU detecte"
        return "cpu"
    }
    if ($text.Length -eq 0) {
        $script:GpuDetail = "nvidia-smi ne renvoie aucun nom de GPU : aucun GPU detecte"
        return "cpu"
    }
    $script:GpuDetail = "nvidia-smi : $(($text -split "`n")[0].Trim())"
    return "cuda"
}

# Detail de la decision GPU (renseigne par Get-GpuDecision quand nvidia-smi
# a ete interroge), affiche a l'etape 3 pour que l'utilisateur sache
# pourquoi il est en CPU malgre un nvidia-smi present (ADR-ad2e).
$script:GpuDetail = ""

function Resolve-CheminOption {
    <#
    .SYNOPSIS
        Nettoie, verifie et rend absolu un chemin recu d'une option (--app,
        --data) ou relu d'un install.json. Audit 10/10 M2 : `--data "C:\Mes
        Docs\"` (antislash final) arrive ici termine par un guillemet, que
        powershell -File a pris pour un guillemet echappe ; un caractere
        interdit (| < > " ...) est refuse tout de suite par Fail, jamais par
        une exception .NET brute a l'etape 7 (M1 pour un pointeur corrompu).
    #>
    param([string]$Chemin, [string]$Source)
    $propre = $Chemin.TrimEnd('"')
    # Antislash final retire, sauf la racine d'un lecteur (D:\).
    while ($propre.Length -gt 3 -and $propre.EndsWith('\')) {
        $propre = $propre.Substring(0, $propre.Length - 1)
    }
    if ($propre.Trim().Length -eq 0) {
        Fail "$Source : chemin vide" "passe un dossier, par exemple C:\Clipper"
    }
    $interdits = [IO.Path]::GetInvalidPathChars() + [char[]]@('"', '<', '>', '|', '*', '?')
    foreach ($c in $interdits) {
        if ($propre.IndexOf($c) -ge 0) {
            Fail "$Source : chemin invalide ($Chemin), caractere interdit" "corrige le chemin (sans guillemet ni < > | * ?) puis relance Installer.bat"
        }
    }
    # M2 (ancien) : chemins toujours resolus en absolu, jamais ecrits relatifs
    # tels quels dans le lanceur ou install.json (un "--app monapp" depuis un
    # terminal casserait le lanceur et le raccourci des qu'ils sont lances
    # d'ailleurs).
    try {
        return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($propre)
    } catch {
        Fail "$Source : chemin invalide ($Chemin) : $($_.Exception.Message)" "corrige le chemin puis relance Installer.bat"
    }
}

function Read-InstallInfo {
    <#
    .SYNOPSIS
        Lit un install.json (pointeur %LOCALAPPDATA%\Clipper\install.json ou
        <app>\install.json). Audit 10/10 M1 : un fichier tronque ou edite a la
        main plantait ConvertFrom-Json en exception brute ; ici un Fail qui
        nomme le fichier et le remede (ADR-ad2e).
    #>
    param([string]$Path, [string]$Quoi)
    try {
        return (Get-Content -Path $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop)
    } catch {
        Fail "$Quoi illisible ($Path) : $($_.Exception.Message)" "supprime ce fichier ou passe --app et --data explicitement"
    }
}

function Resolve-Wheel {
    param([string]$Root)
    $wheel = Get-ChildItem -Path $Root -Filter "clipper-*-py3-none-any.whl" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($wheel) {
        return $wheel.FullName
    }
    return $null
}

function Format-ClipperBat {
    <#
    .SYNOPSIS
        Remplit installer/Clipper.bat.template avec les chemins absolus de
        app et data. Utilisee a l'identique pour ecrire le vrai lanceur
        (installation reelle) et pour afficher le resultat (--dry-run,
        criterion 4) : une seule fonction, jamais deux copies divergentes.
    #>
    param([string]$TemplatePath, [string]$App, [string]$Data)
    $content = Get-Content -Path $TemplatePath -Raw
    return $content.Replace("__APP__", $App).Replace("__DATA__", $Data)
}

# --------------------------------------------------------------------------
# Etapes (R3) : une fonction par etape, qui recoit -DryRun. En --dry-run,
# aucune ne cree de dossier, n'ecrit de fichier ni ne lance de telechargement :
# seule la ligne de decision est affichee.
# --------------------------------------------------------------------------

function Get-PrevolManques {
    <#
    .SYNOPSIS
        Audit 10/10 M6 : ce que l'etape 3 verifiera sans aucun reseau (uv.exe,
        wheel, overrides.txt du zip), controle AVANT la suppression de .venv :
        un zip incomplet (telechargement interrompu, antivirus) ne doit jamais
        detruire une installation existante puis echouer.
    #>
    param([string]$Root, [string]$Uv)
    $manques = @()
    if (-not (Test-Path $Uv)) {
        $manques += "uv.exe introuvable ($Uv)"
    }
    if (-not (Resolve-Wheel -Root $Root)) {
        $manques += "wheel clipper introuvable a cote d'Installer.bat ($Root)"
    }
    $overridesPath = Join-Path $Root "installer\overrides.txt"
    if (-not (Test-Path $overridesPath)) {
        $manques += "fichier d'overrides introuvable ($overridesPath)"
    }
    return $manques
}

function Invoke-Step1-Prepare {
    param([string]$App, [string]$Data, [string]$NewVersion, [int]$Port, [string]$Root, [string]$Uv, [switch]$DryRun)
    $versionFile = Join-Path $App "version.txt"
    $venvDir = Join-Path $App ".venv"
    $isUpdate = $false
    if (Test-Path $versionFile) {
        $oldVersion = (Get-Content $versionFile -Raw).Trim()
        $cmp = Compare-ClipperVersion $NewVersion $oldVersion
        if ($cmp -lt 0) {
            Fail `
                "version $NewVersion plus ancienne que la version installee ($oldVersion) : pas de retrogradation" `
                "telecharge la derniere version depuis la page des releases GitHub"
        }
        $isUpdate = $true
        Write-Step 1 "Preparation : app=$App (mise a jour $oldVersion -> $NewVersion) ; data=$Data"
        Write-Detail ".venv sera supprime puis recree (python/ et ffmpeg/ conserves)"
    } else {
        Write-Step 1 "Preparation : app=$App (premiere installation, version $NewVersion) ; data=$Data"
        if (Test-Path $venvDir) {
            Write-Detail ".venv existant (installation precedente interrompue) sera supprime puis recree"
        }
    }
    # R6, et audit 10/10 M5 : le port de la console est controle des qu'un
    # .venv existe (premiere installation interrompue apres l'etape 9 avec la
    # console lancee comprise), pas seulement en mise a jour : sinon
    # Remove-Item .venv tombe sur un python.exe verrouille.
    $venvEnJeu = $isUpdate -or (Test-Path $venvDir)
    $portOccupe = $venvEnJeu -and (Test-ConsolePortListening -Port $Port)
    $manques = Get-PrevolManques -Root $Root -Uv $Uv
    if ($DryRun) {
        if ($portOccupe) {
            Write-Detail "le port $Port est deja occupe : une vraie installation s'arreterait ici avant de toucher .venv"
        }
        foreach ($m in $manques) {
            Write-Detail "$m : une vraie installation s'arreterait ici avant de toucher .venv"
        }
    } else {
        if ($portOccupe) {
            Fail "la console Clipper tourne (port $Port)" "ferme la fenetre 'Clipper serve' puis relance Installer.bat"
        }
        if ($manques.Count -gt 0) {
            Fail ($manques -join " ; ") "retelecharge le zip complet depuis la page des releases (rien n'a ete modifie)"
        }
        New-Item -ItemType Directory -Force -Path $App | Out-Null
        New-Item -ItemType Directory -Force -Path $Data | Out-Null
        # Supprime .venv qu'il s'agisse d'une mise a jour ou d'une premiere
        # installation interrompue apres l'etape 3 (C3) : version.txt (ecrit
        # seulement a l'etape 11) ne distingue pas ces deux cas, mais dans
        # les deux un .venv existant doit disparaitre avant 'uv venv'.
        if (Test-Path $venvDir) {
            Remove-Item -Recurse -Force -Path $venvDir
        }
        Write-Log $App "etape 1/$TOTAL_STEPS : preparation ($App, $Data)"
    }
    return $isUpdate
}

function Invoke-Step2-Python {
    param([string]$App, [string]$Uv, [switch]$DryRun)
    $pythonDir = Join-Path $App "python"
    Write-Step 2 "Python 3.11 sous $pythonDir (uv python install 3.11)"
    if ($DryRun) {
        return
    }
    if (-not (Test-Path $Uv)) {
        Fail "uv.exe introuvable ($Uv)" "retelecharge le zip complet depuis la page des releases"
    }
    # UV_CACHE_DIR pose ici (M4), avant le premier appel a uv : sans cela,
    # 'uv python install' ecrit l'archive CPython telechargee dans
    # %LOCALAPPDATA%\uv\cache (jamais nettoye), au lieu de app\cache (vide a
    # l'etape 11).
    $env:UV_CACHE_DIR = Join-Path $App "cache"
    $env:UV_PYTHON_INSTALL_DIR = $pythonDir
    & $Uv python install 3.11
    if ($LASTEXITCODE -ne 0) {
        Fail "'uv python install 3.11' a echoue" "verifie la connexion reseau puis relance Installer.bat"
    }
    Write-Log $App "etape 2/$TOTAL_STEPS : python installe sous $pythonDir"
}

function Invoke-Step3-Venv {
    param([string]$App, [string]$Root, [string]$Uv, [string]$Device, [switch]$DryRun)
    $venvDir = Join-Path $App ".venv"
    if ($Device -eq "cuda") {
        Write-Step 3 "Environnement sous $venvDir ; GPU : [cuda] detecte, extra clipper[cuda] installe"
    } else {
        Write-Step 3 "Environnement sous $venvDir ; GPU : CPU (transcription plus lente)"
    }
    if ($script:GpuDetail) {
        Write-Detail $script:GpuDetail
    }
    if ($DryRun) {
        return
    }
    if (-not (Test-Path $Uv)) {
        Fail "uv.exe introuvable ($Uv)" "retelecharge le zip complet depuis la page des releases"
    }
    $wheel = Resolve-Wheel -Root $Root
    if (-not $wheel) {
        Fail "wheel clipper introuvable a cote d'Installer.bat" "retelecharge le zip complet depuis la page des releases"
    }
    & $Uv venv $venvDir --python 3.11
    if ($LASTEXITCODE -ne 0) {
        Fail "'uv venv' a echoue" "relance Installer.bat"
    }
    $target = $wheel
    if ($Device -eq "cuda") {
        $target = "$wheel[cuda]"
    }
    # Override livre dans le zip (I1), jamais genere a l'installation :
    # 'uv pip install' ne lit [tool.uv] override-dependencies que dans un
    # pyproject.toml trouve en remontant depuis le dossier courant, absent
    # chez l'utilisateur (Telechargements). Sans lui, scenedetect tire
    # opencv-python et mediapipe tire opencv-contrib-python : deux paquets
    # ecrivent le meme cv2/ (AGENTS.md, Pieges).
    $overridesPath = Join-Path $Root "installer\overrides.txt"
    if (-not (Test-Path $overridesPath)) {
        Fail "fichier d'overrides introuvable ($overridesPath)" "retelecharge le zip complet depuis la page des releases"
    }
    # --python explicite : sans lui, "uv pip install" cherche un .venv en
    # remontant depuis le dossier courant (ou VIRTUAL_ENV) et peut installer
    # dans un venv totalement different de celui qu'on vient de creer sous
    # $App (observe reellement quand Installer.bat est lance depuis un
    # dossier dont un ancetre contient un .venv de developpement).
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    & $Uv pip install --python $venvPython --override $overridesPath $target
    if ($LASTEXITCODE -ne 0) {
        Fail "'uv pip install' a echoue" "verifie la connexion reseau puis relance Installer.bat"
    }
    $sitePackages = Join-Path $venvDir "Lib\site-packages"
    $opencvDistInfos = @(Get-ChildItem -Path $sitePackages -Filter "opencv*.dist-info" -ErrorAction SilentlyContinue)
    if ($opencvDistInfos.Count -ne 1) {
        Fail `
            "l'environnement contient $($opencvDistInfos.Count) paquet(s) OpenCV (1 attendu) : $($opencvDistInfos.Name -join ', ')" `
            "verifie l'override OpenCV (installer\overrides.txt) puis relance Installer.bat"
    }
    & $venvPython -c "import cv2"
    if ($LASTEXITCODE -ne 0) {
        Fail "'import cv2' a echoue dans l'environnement installe" "relance Installer.bat ; si l'echec persiste, ouvre une issue avec le rapport clipper doctor"
    }
    Write-Log $App "etape 3/$TOTAL_STEPS : environnement installe ($Device)"
}

function Invoke-Step4-Ffmpeg {
    param([string]$App, [switch]$DryRun)
    $binDir = Join-Path $App "ffmpeg\bin"
    $ffmpegExe = Join-Path $binDir "ffmpeg.exe"
    if (Test-Path $ffmpegExe) {
        Write-Step 4 "ffmpeg deja present sous $binDir (conserve)"
        return
    }
    Write-Step 4 "ffmpeg sera telecharge sous $binDir ($FFMPEG_URL)"
    if ($DryRun) {
        return
    }
    $archive = Join-Path $env:TEMP "clipper-ffmpeg.zip"
    Invoke-WebRequest -Uri $FFMPEG_URL -OutFile $archive
    $actualHash = (Get-FileHash -Path $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $FFMPEG_SHA256) {
        Remove-Item -Force -Path $archive -ErrorAction SilentlyContinue
        Fail "sha256 de l'archive ffmpeg invalide ($actualHash)" "relance Installer.bat ; si l'echec persiste, l'URL ffmpeg epinglee a change"
    }
    $extractDir = Join-Path $env:TEMP "clipper-ffmpeg-extract"
    if (Test-Path $extractDir) {
        Remove-Item -Recurse -Force -Path $extractDir
    }
    Expand-Archive -Path $archive -DestinationPath $extractDir
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $sourceBin = Get-ChildItem -Path $extractDir -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    if (-not $sourceBin) {
        Fail "ffmpeg.exe introuvable dans l'archive telechargee" "relance Installer.bat"
    }
    Copy-Item -Path (Join-Path $sourceBin.DirectoryName "ffmpeg.exe") -Destination $binDir -Force
    Copy-Item -Path (Join-Path $sourceBin.DirectoryName "ffprobe.exe") -Destination $binDir -Force
    Remove-Item -Force -Path $archive -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force -Path $extractDir -ErrorAction SilentlyContinue
    Write-Log $App "etape 4/$TOTAL_STEPS : ffmpeg installe sous $binDir"
}

function Invoke-Step5-Claude {
    param([string]$App, [switch]$DryRun)
    $found = Get-Command "claude" -ErrorAction SilentlyContinue
    if ($found) {
        Write-Step 5 "claude trouve ($($found.Source)) : connexion verifiee (claude auth status)"
    } else {
        Write-Step 5 "claude introuvable : sera installe (irm https://claude.ai/install.ps1 | iex) puis connexion demandee"
    }
    if ($DryRun) {
        return
    }
    if (-not $found) {
        # .Content peut etre un System.Byte[] (pas une String) selon le
        # Content-Type renvoye : Invoke-Expression exige une String, sinon
        # ParameterBindingException ("Impossible de convertir System.Byte[]"),
        # jamais observe sur un PC de dev ou 'claude' est deja sur le PATH
        # (cette branche n'est alors jamais executee).
        $installScript = (Invoke-WebRequest -Uri "https://claude.ai/install.ps1" -UseBasicParsing).Content
        if ($installScript -is [byte[]]) {
            $installScript = [Text.Encoding]::UTF8.GetString($installScript)
        }
        Invoke-Expression $installScript
        $found = Get-Command "claude" -ErrorAction SilentlyContinue
        if (-not $found) {
            # L'installeur officiel pose claude.exe sous ...\.local\bin et ne
            # met a jour que le PATH utilisateur (registre), jamais
            # $env:Path du process courant (confirme par un run reel,
            # TASK-4f1d7d1ee341, douteux #1) : cherche-le explicitement avant
            # de conclure a un echec.
            $localBin = Join-Path $env:USERPROFILE ".local\bin"
            $localClaude = Join-Path $localBin "claude.exe"
            if (Test-Path $localClaude) {
                $env:Path = "$localBin;" + $env:Path
                $found = Get-Command "claude" -ErrorAction SilentlyContinue
            }
        }
        if (-not $found) {
            Fail "l'installation de claude a echoue" "installe-le manuellement (https://claude.ai/install.ps1) puis relance Installer.bat"
        }
    }
    & claude auth status
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[installer] claude n'est pas connecte : ouverture de 'claude auth login'..." -ForegroundColor Yellow
        Start-Process -FilePath "claude" -ArgumentList "auth", "login"
        Write-Host "[installer] connecte-toi dans la fenetre ouverte, puis appuie sur une touche ici..."
        [void][System.Console]::ReadKey($true)
        & claude auth status
        if ($LASTEXITCODE -ne 0) {
            Fail "claude n'est toujours pas connecte" "lance 'claude auth login' puis relance Installer.bat (ADR-b1c1, ADR-ad2e : pas de LLM, pas d'installation)"
        }
    }
    Write-Log $App "etape 5/$TOTAL_STEPS : claude pret"
}

function Invoke-Step6-Chrome {
    param([string]$App, [switch]$DryRun)
    $candidates = @(
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    )
    $chrome = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if ($chrome) {
        Write-Step 6 "Google Chrome trouve : $chrome"
    } else {
        Write-Host "[6/$TOTAL_STEPS] ATTENTION : Google Chrome introuvable (necessaire seulement pour publier) : https://www.google.com/chrome" -ForegroundColor Yellow
    }
    if (-not $DryRun) {
        Write-Log $App "etape 6/$TOTAL_STEPS : chrome $(if ($chrome) { $chrome } else { 'absent' })"
    }
}

function Invoke-Step7-Data {
    param([string]$App, [string]$Data, [string]$PremierClipSource, [switch]$DryRun)
    $configPath = Join-Path $Data "config.toml"
    $initNeeded = -not (Test-Path $configPath)
    $premierClipPath = Join-Path $Data "PREMIER-CLIP.txt"
    # M5 : copie seulement a la premiere installation (ou si le fichier a
    # disparu) ; sinon une mise a jour reecrit PREMIER-CLIP.txt dans data a
    # chaque fois, contrairement a R6/docs ("data jamais ecrit").
    $premierClipNeeded = $initNeeded -or -not (Test-Path $premierClipPath)
    if ($initNeeded) {
        Write-Step 7 "Donnees : clipper init sera execute dans $Data (config.toml absent)"
    } else {
        Write-Step 7 "Donnees : $Data existe deja (config.toml present), clipper init non appele"
    }
    if ($premierClipNeeded) {
        Write-Detail "$premierClipPath sera ecrit"
    } else {
        Write-Detail "$premierClipPath deja present, conserve"
    }
    if ($DryRun) {
        return
    }
    if ($initNeeded) {
        $clipper = Join-Path $App ".venv\Scripts\clipper.exe"
        Push-Location $Data
        try {
            & $clipper init
            if ($LASTEXITCODE -ne 0) {
                Fail "'clipper init' a echoue dans $Data" "verifie les droits d'ecriture de $Data"
            }
        } finally {
            Pop-Location
        }
    }
    if ($premierClipNeeded) {
        Copy-Item -Path $PremierClipSource -Destination $premierClipPath -Force
    }
    Write-Log $App "etape 7/$TOTAL_STEPS : donnees pretes dans $Data (init $(if ($initNeeded) { 'execute' } else { 'ignore' }))"
}

function Invoke-Step8-Models {
    param([string]$App, [string]$Data, [switch]$DryRun)
    Write-Step 8 "Modeles : clipper models prefetch sera execute dans $Data (whisper, mediapipe)"
    if ($DryRun) {
        return
    }
    $clipper = Join-Path $App ".venv\Scripts\clipper.exe"
    Push-Location $Data
    try {
        & $clipper models prefetch
        if ($LASTEXITCODE -ne 0) {
            Fail "'clipper models prefetch' a echoue" "verifie la connexion reseau puis relance Installer.bat (une relance reprend la ou il s'est arrete)"
        }
    } finally {
        Pop-Location
    }
    Write-Log $App "etape 8/$TOTAL_STEPS : modeles prets"
}

function Invoke-Step9-Launcher {
    param([string]$App, [string]$Data, [string]$TemplatePath, [string]$Root, [switch]$SansRaccourci, [switch]$DryRun)
    $launcherPath = Join-Path $App "Clipper.bat"
    $filled = Format-ClipperBat -TemplatePath $TemplatePath -App $App -Data $Data
    $appIconPath = Join-Path $App "clipper.ico"
    $sourceIconPath = Join-Path $Root "installer\clipper.ico"
    $appDesinstallerBat = Join-Path $App "Desinstaller.bat"
    $appDesinstallerPs1 = Join-Path $App "installer\desinstaller.ps1"
    if ($SansRaccourci) {
        Write-Step 9 "Lanceur : $launcherPath (PATH = ffmpeg\bin puis .venv\Scripts, data courant, ouvre http://127.0.0.1:8000) ; aucun raccourci (--sans-raccourci)"
    } else {
        Write-Step 9 "Lanceur : $launcherPath (PATH = ffmpeg\bin puis .venv\Scripts, data courant, ouvre http://127.0.0.1:8000) et raccourci Clipper.lnk sur le Bureau"
    }
    # I7 : Desinstaller.bat (et desinstaller.ps1) copies dans app, sinon
    # jamais presents une fois le dossier dezippe supprime (R2, docs).
    Write-Detail "$appDesinstallerBat sera copie (desinstallation future)"
    # M1 : icone copiee dans app, sinon le raccourci pointe sur le zip
    # dezippe et perd son icone une fois ce dossier supprime.
    Write-Detail "$appIconPath sera copie"
    if ($DryRun) {
        Write-Host "--- $launcherPath (apercu) ---"
        Write-Host $filled
        Write-Host "--- fin de l'apercu ---"
        return
    }
    # Encodage OEM (I2), pas la page ANSI par defaut de Set-Content en
    # PowerShell 5.1 : cmd.exe lit un .bat dans sa page de code OEM, pas en
    # cp1252. Un chemin accentue (--data "...\Desire\...") ecrit en cp1252
    # est relu comme un autre caractere par cmd (echec silencieux du 'cd').
    $oemCodePage = [Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage
    $oemEncoding = [Text.Encoding]::GetEncoding($oemCodePage)
    [IO.File]::WriteAllText($launcherPath, $filled, $oemEncoding)
    New-Item -ItemType Directory -Force -Path (Join-Path $App "installer") | Out-Null
    Copy-Item -Path (Join-Path $Root "Desinstaller.bat") -Destination $appDesinstallerBat -Force
    Copy-Item -Path (Join-Path $Root "installer\desinstaller.ps1") -Destination $appDesinstallerPs1 -Force
    if (Test-Path $sourceIconPath) {
        Copy-Item -Path $sourceIconPath -Destination $appIconPath -Force
    }
    if ($SansRaccourci) {
        Write-Log $App "etape 9/$TOTAL_STEPS : lanceur $launcherPath, pas de raccourci (--sans-raccourci)"
        return
    }
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "Clipper.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($shortcutPath)
    $lnk.TargetPath = $launcherPath
    $lnk.WorkingDirectory = $Data
    if (Test-Path $appIconPath) {
        $lnk.IconLocation = "$appIconPath,0"
    }
    $lnk.Description = "Console Clipper"
    $lnk.Save()
    Write-Log $App "etape 9/$TOTAL_STEPS : lanceur $launcherPath, raccourci $shortcutPath"
}

function Invoke-Step10-Doctor {
    param([string]$App, [string]$Data, [switch]$DryRun)
    $binDir = Join-Path $App "ffmpeg\bin"
    $scriptsDir = Join-Path $App ".venv\Scripts"
    Write-Step 10 "Controle : clipper doctor dans $Data (PATH = $binDir;$scriptsDir)"
    if ($DryRun) {
        return
    }
    $previousPath = $env:Path
    $previousLocation = Get-Location
    try {
        $env:Path = "$binDir;$scriptsDir;" + $env:Path
        Set-Location $Data
        $report = & clipper doctor
        $exitCode = $LASTEXITCODE
        $report | ForEach-Object { Write-Host $_ }
        if ($exitCode -ne 0) {
            Fail "clipper doctor rapporte un probleme (voir le rapport ci-dessus)" "corrige le point signale puis relance Installer.bat"
        }
    } finally {
        $env:Path = $previousPath
        Set-Location $previousLocation
    }
    Write-Log $App "etape 10/$TOTAL_STEPS : clipper doctor ok"
}

function Invoke-Step11-Finish {
    param([string]$App, [string]$Data, [string]$Version, [string]$Device, [switch]$SansConsole, [switch]$DryRun)
    $installJson = Join-Path $App "install.json"
    $cacheDir = Join-Path $App "cache"
    Write-Step 11 "Fin : $installJson ecrit, $cacheDir supprime$(if (-not $SansConsole) { ', console ouverte' })"
    if ($DryRun) {
        return
    }
    $payload = @{
        app     = $App
        data    = $Data
        version = $Version
        cuda    = ($Device -eq "cuda")
        date    = (Get-Date -Format "o")
    }
    $payload | ConvertTo-Json | Set-Content -Path $installJson
    # Pointeur (I5) sous %LOCALAPPDATA%\Clipper\install.json, toujours a cet
    # emplacement fixe meme avec --app personnalise : une relance sans
    # --app/--data le relit (voir plus bas, avant Step1) pour retrouver le
    # meme app/data plutot que de retomber sur les chemins par defaut.
    $pointerDir = Join-Path $env:LOCALAPPDATA "Clipper"
    New-Item -ItemType Directory -Force -Path $pointerDir | Out-Null
    $payload | ConvertTo-Json | Set-Content -Path (Join-Path $pointerDir "install.json")
    # Step1-Prepare lit ce fichier pour decider premiere installation / mise
    # a jour : sans lui, une relance se croit toujours a sa premiere
    # installation, ne supprime jamais l'ancien .venv et 'uv venv' echoue
    # (deja observe reellement, R2).
    Set-Content -Path (Join-Path $App "version.txt") -Value $Version -NoNewline
    if (Test-Path $cacheDir) {
        Remove-Item -Recurse -Force -Path $cacheDir
    }
    Write-Log $App "etape 11/$TOTAL_STEPS : installation terminee ($Version, $Device)"
    Write-Host ""
    Write-Host "Installation terminee : programme sous $App, donnees sous $Data" -ForegroundColor Green
    if (-not $SansConsole) {
        & (Join-Path $App "Clipper.bat")
    }
}

if ($NoAutoRun) {
    # Point d'injection pour les tests : les fonctions ci-dessus sont
    # definies dans la portee de l'appelant (dot-sourcing), rien d'autre ne
    # s'execute.
    return
}

# --------------------------------------------------------------------------
# Lecture des options (style GNU : --app, --data, --cpu, --cuda,
# --sans-console, --dry-run), puis execution des 11 etapes dans l'ordre.
# --------------------------------------------------------------------------

$App = Join-Path $env:LOCALAPPDATA "Clipper\app"
$Data = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Clipper"
$AppGiven = $false
$DataGiven = $false
$Cpu = $false
$Cuda = $false
$SansConsole = $false
$SansRaccourci = $false
$DryRun = $false

$i = 0
while ($i -lt $RawArgs.Count) {
    $token = $RawArgs[$i]
    if ($token -eq "--app") {
        if ($i + 1 -ge $RawArgs.Count) {
            Fail "option $token sans valeur" "passe un dossier apres $token, par exemple $token C:\Clipper"
        }
        $i++
        $App = $RawArgs[$i]
        $AppGiven = $true
    } elseif ($token -eq "--data") {
        if ($i + 1 -ge $RawArgs.Count) {
            Fail "option $token sans valeur" "passe un dossier apres $token, par exemple $token C:\Clipper"
        }
        $i++
        $Data = $RawArgs[$i]
        $DataGiven = $true
    } elseif ($token -eq "--cpu") {
        $Cpu = $true
    } elseif ($token -eq "--cuda") {
        $Cuda = $true
    } elseif ($token -eq "--sans-console") {
        $SansConsole = $true
    } elseif ($token -eq "--sans-raccourci") {
        $SansRaccourci = $true
    } elseif ($token -eq "--dry-run") {
        $DryRun = $true
    } else {
        Fail "option inconnue : $token" "options valides : --app, --data, --cpu, --cuda, --sans-console, --sans-raccourci, --dry-run"
    }
    $i++
}

if ($Cpu -and $Cuda) {
    Fail "options --cpu et --cuda incompatibles (choisis l'une des deux, ou aucune pour la detection automatique)" "relance avec --cpu ou --cuda, jamais les deux"
}

if ($AppGiven) {
    $App = Resolve-CheminOption -Chemin $App -Source "option --app"
}
if ($DataGiven) {
    $Data = Resolve-CheminOption -Chemin $Data -Source "option --data"
}

# I5 : sans --app ni --data, relit le pointeur laisse par une installation
# precedente (toujours a cet emplacement fixe, meme avec --app personnalise,
# voir Step11-Finish) plutot que de retomber sur les chemins par defaut, qui
# pointeraient vers une installation vide.
# Audit 10/10 I3 : chaque option est relue separement (R2 "relues comme
# defauts") : --app seul reprend le data de <app>\install.json (sinon une
# mise a jour pointait lanceur et raccourci vers Documents\Clipper vide,
# bibliotheque "perdue") ; --data seul reprend le app du pointeur.
$pointerPath = Join-Path $env:LOCALAPPDATA "Clipper\install.json"
if (-not $AppGiven -and -not $DataGiven) {
    if (Test-Path $pointerPath) {
        $pointerInfo = Read-InstallInfo -Path $pointerPath -Quoi "pointeur d'installation"
        if ($pointerInfo.app) {
            $App = Resolve-CheminOption -Chemin ([string]$pointerInfo.app) -Source "pointeur $pointerPath, champ app"
        }
        if ($pointerInfo.data) {
            $Data = Resolve-CheminOption -Chemin ([string]$pointerInfo.data) -Source "pointeur $pointerPath, champ data"
        }
    }
} elseif ($AppGiven -and -not $DataGiven) {
    $appInfoPath = Join-Path $App "install.json"
    if (Test-Path $appInfoPath) {
        $appInfo = Read-InstallInfo -Path $appInfoPath -Quoi "install.json de l'installation"
        if ($appInfo.data) {
            $Data = Resolve-CheminOption -Chemin ([string]$appInfo.data) -Source "$appInfoPath, champ data"
        }
    }
} elseif ($DataGiven -and -not $AppGiven) {
    if (Test-Path $pointerPath) {
        $pointerInfo = Read-InstallInfo -Path $pointerPath -Quoi "pointeur d'installation"
        if ($pointerInfo.app) {
            $App = Resolve-CheminOption -Chemin ([string]$pointerInfo.app) -Source "pointeur $pointerPath, champ app"
        }
    }
}

# Audit 10/10 I4 : data egal a app ou sous app est refuse. desinstaller.ps1
# supprime app sans condition, avant et independamment de la reponse a
# "Supprimer aussi les donnees ?" : workspace/, output/ et state/ partiraient
# avec app malgre un N (INSTALLATION.md : "les donnees sont conservees par
# defaut"). Comparaison insensible a la casse, sur un separateur entier
# (C:\x et C:\xy restent deux dossiers voisins).
$appPrefixe = $App.TrimEnd('\') + '\'
if (($Data -ieq $App) -or $Data.StartsWith($appPrefixe, [StringComparison]::OrdinalIgnoreCase)) {
    Fail "le dossier de donnees ($Data) est le dossier programme ($App) ou se trouve dedans : la desinstallation les effacerait ensemble" "choisis un --data hors de --app, par exemple --app C:\Clipper\app --data C:\Clipper\donnees"
}

$versionFile = Join-Path (Split-Path -Parent $PSScriptRoot) "version.txt"
if (-not (Test-Path $versionFile)) {
    Fail "fichier version.txt introuvable ($versionFile)" "reconstruis le zip (tools/build_portable.py)"
}
$newVersion = (Get-Content $versionFile -Raw).Trim()
$root = Split-Path -Parent $versionFile
$uv = Join-Path $root "uv.exe"
$templatePath = Join-Path $PSScriptRoot "Clipper.bat.template"
$premierClipSource = Join-Path $PSScriptRoot "PREMIER-CLIP.txt"

$isUpdate = Invoke-Step1-Prepare -App $App -Data $Data -NewVersion $newVersion -Port $Port -Root $root -Uv $uv -DryRun:$DryRun
Invoke-Step2-Python -App $App -Uv $uv -DryRun:$DryRun
$device = Get-GpuDecision -Cpu:$Cpu -Cuda:$Cuda
Invoke-Step3-Venv -App $App -Root $root -Uv $uv -Device $device -DryRun:$DryRun
Invoke-Step4-Ffmpeg -App $App -DryRun:$DryRun
Invoke-Step5-Claude -App $App -DryRun:$DryRun
Invoke-Step6-Chrome -App $App -DryRun:$DryRun
Invoke-Step7-Data -App $App -Data $Data -PremierClipSource $premierClipSource -DryRun:$DryRun
Invoke-Step8-Models -App $App -Data $Data -DryRun:$DryRun
Invoke-Step9-Launcher -App $App -Data $Data -TemplatePath $templatePath -Root $root -SansRaccourci:$SansRaccourci -DryRun:$DryRun
Invoke-Step10-Doctor -App $App -Data $Data -DryRun:$DryRun
Invoke-Step11-Finish -App $App -Data $Data -Version $newVersion -Device $device -SansConsole:$SansConsole -DryRun:$DryRun

exit 0
