param([switch]$Auto)   # -Auto : lancé par Studio-Clips.exe (pas de pause à la fin)
# Installation automatique de Studio Clips.
# Lancé par INSTALLER.bat — installe tout, choisit les modèles selon la puissance du PC.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$App = $PSScriptRoot
Set-Location -LiteralPath $App

function Etape($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Ok($t)    { Write-Host "    OK : $t" -ForegroundColor Green }
function Stop-Erreur($t) {
    Write-Host ""; Write-Host "PROBLEME : $t" -ForegroundColor Red
    Write-Host "Fais une capture de cette fenetre et envoie-la." -ForegroundColor Yellow
    Read-Host "Appuie sur Entree pour fermer"; exit 1
}
function Winget-Installer($id, $nom) {
    Write-Host "    Installation de $nom (peut prendre quelques minutes)..."
    & winget install --id $id -e --source winget --scope user --silent `
        --accept-source-agreements --accept-package-agreements | Out-Null
    # 0 = installé ; -1978335189 = déjà installé ; certains paquets refusent --scope user
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
        & winget install --id $id -e --source winget --silent `
            --accept-source-agreements --accept-package-agreements | Out-Null
    }
}

Write-Host "=============================================" -ForegroundColor Magenta
Write-Host "  Installation de Studio Clips" -ForegroundColor Magenta
Write-Host "  Ne ferme pas cette fenetre, tout est auto." -ForegroundColor Magenta
Write-Host "=============================================" -ForegroundColor Magenta

# --- 0. Puissance du PC -> choix des modèles -------------------------------
$ramGo = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
if     ($ramGo -ge 15) { $ia = 'qwen3:8b';    $whisper = 'small' }
elseif ($ramGo -ge 7)  { $ia = 'qwen3:4b';    $whisper = 'base'  }
else                   { $ia = 'qwen3:1.7b'; $whisper = 'base'  }
Etape "PC detecte : $ramGo Go de RAM -> IA '$ia', transcription '$whisper'"

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Stop-Erreur "winget est absent. Ouvre le Microsoft Store, cherche 'Programme d'installation d'application', mets-le a jour, puis relance INSTALLER.bat."
}

# --- 1. Python + environnement de l'appli ----------------------------------
Etape "1/5 Python"
function Trouver-Python {
    $c = @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
           "$env:ProgramFiles\Python312\python.exe")
    foreach ($p in $c) { if (Test-Path -LiteralPath $p) { return $p } }
    return $null
}
$py = Trouver-Python
if (-not $py) { Winget-Installer 'Python.Python.3.12' 'Python 3.12'; $py = Trouver-Python }
if (-not $py) { Stop-Erreur "Python ne s'est pas installe." }
Ok $py

$venvPy = Join-Path $App '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host "    Creation de l'environnement de l'appli..."
    & $py -m venv (Join-Path $App '.venv')
    if ($LASTEXITCODE -ne 0) { Stop-Erreur "Impossible de creer l'environnement Python." }
}
Write-Host "    Installation des bibliotheques (quelques minutes)..."
& $venvPy -m pip install --disable-pip-version-check -q --upgrade pip | Out-Null
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $App 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Stop-Erreur "L'installation des bibliotheques Python a echoue (verifie la connexion internet)." }
Ok "bibliotheques installees"

# --- 2. FFmpeg (copié dans bin\ de l'appli : pas besoin de toucher au PATH) ---
Etape "2/5 FFmpeg"
$bin = Join-Path $App 'bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $bin 'ffmpeg.exe'))) {
    $src = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $src) {
        Winget-Installer 'Gyan.FFmpeg' 'FFmpeg'
        $src = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages", "$env:ProgramFiles\WinGet\Packages" `
            -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        $dir = if ($src) { $src.DirectoryName } else { $null }
    } else { $dir = Split-Path $src.Source }
    if (-not $dir) { Stop-Erreur "FFmpeg ne s'est pas installe." }
    Copy-Item -LiteralPath (Join-Path $dir 'ffmpeg.exe'), (Join-Path $dir 'ffprobe.exe') -Destination $bin -Force
}
Ok "ffmpeg + ffprobe dans bin\"

# --- 3. Ollama (l'IA qui choisit les passages) -----------------------------
Etape "3/5 Ollama"
$ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if (-not (Test-Path -LiteralPath $ollama)) { Winget-Installer 'Ollama.Ollama' 'Ollama' }
if (-not (Test-Path -LiteralPath $ollama)) {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { $ollama = $cmd.Source } else { Stop-Erreur "Ollama ne s'est pas installe." }
}
function Ollama-Repond { try { Invoke-RestMethod http://localhost:11434/api/tags -TimeoutSec 3 | Out-Null; $true } catch { $false } }
if (-not (Ollama-Repond)) {
    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
    for ($i = 0; $i -lt 30 -and -not (Ollama-Repond); $i++) { Start-Sleep 1 }
}
if (-not (Ollama-Repond)) { Stop-Erreur "Ollama ne demarre pas." }
Ok "Ollama tourne"

# --- 4. Téléchargement des modèles -----------------------------------------
Etape "4/5 Telechargement de l'IA '$ia' (1 a 5 Go, sois patient)"
& $ollama pull $ia
if ($LASTEXITCODE -ne 0) { Stop-Erreur "Le telechargement du modele IA a echoue (connexion ?)." }

# Réglage des modèles dans l'appli selon la puissance du PC
$utf8 = New-Object System.Text.UTF8Encoding($false)
$f = Join-Path $App 'analyze.py'
$t = [IO.File]::ReadAllText($f)
$t = $t -replace '(?m)^OLLAMA_MODEL = ".*"$', "OLLAMA_MODEL = `"$ia`""
[IO.File]::WriteAllText($f, $t, $utf8)
$f = Join-Path $App 'transcribe.py'
$t = [IO.File]::ReadAllText($f)
$t = $t -replace 'model_size: str = "[a-z0-9.-]+"', "model_size: str = `"$whisper`""
[IO.File]::WriteAllText($f, $t, $utf8)

Write-Host "    Telechargement du modele de transcription '$whisper'..."
& $venvPy -c "from faster_whisper import WhisperModel; WhisperModel('$whisper', device='cpu', compute_type='int8')"
if ($LASTEXITCODE -ne 0) { Stop-Erreur "Le telechargement du modele de transcription a echoue." }
Ok "modeles prets"

# --- 5. Raccourci sur le Bureau --------------------------------------------
Etape "5/5 Raccourci sur le Bureau"
$bureau = [Environment]::GetFolderPath('Desktop')
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $bureau 'Studio Clips.lnk'))
$lnk.TargetPath = Join-Path $App '.venv\Scripts\pythonw.exe'
$lnk.Arguments = "`"$(Join-Path $App 'lanceur.py')`""
$lnk.WorkingDirectory = $App
$lnk.Description = "Studio Clips (se met a jour tout seul)"
$lnk.WindowStyle = 1
$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
if (Test-Path -LiteralPath $edge) { $lnk.IconLocation = "$edge,0" }
$lnk.Save()
Ok "raccourci 'Studio Clips' cree"

Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host "  TOUT EST INSTALLE !" -ForegroundColor Green
Write-Host "  Double-clique sur 'Studio Clips' sur le Bureau." -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
if (-not $Auto) { Read-Host "Appuie sur Entree pour fermer" }
