# Installation complète de Studio Clips (lancée par Studio-Clips.exe).
# Installe dans %LOCALAPPDATA%\StudioClips, récupère les projets d'une ancienne installation,
# puis ouvre l'appli. Rien à décompresser, rien à choisir.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$ici = $PSScriptRoot
# Toute erreur imprévue : on l'affiche et on attend, au lieu de fermer la fenêtre sans rien dire.
trap {
    Write-Host ""; Write-Host "PROBLEME : $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Fais une capture de cette fenetre et envoie-la." -ForegroundColor Yellow
    Read-Host "Appuie sur Entree pour fermer"; exit 1
}
$dest = Join-Path $env:LOCALAPPDATA 'StudioClips'
function Creer-Raccourcis($App) {
    # Bureau + menu Démarrer. Le raccourci passe par demarrer.vbs : pas de fenêtre noire, et un
    # message clair si on clique avant la fin de l'installation.
    $ws = New-Object -ComObject WScript.Shell
    foreach ($dossier in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
        if (-not $dossier -or -not (Test-Path -LiteralPath $dossier)) { continue }
        $lnk = $ws.CreateShortcut((Join-Path $dossier 'Studio Clips.lnk'))
        $lnk.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
        $lnk.Arguments = "`"$(Join-Path $App 'demarrer.vbs')`""
        $lnk.WorkingDirectory = $App
        $lnk.IconLocation = "$(Join-Path $App 'icone.ico'),0"
        $lnk.Description = 'Studio Clips (se met a jour tout seul)'
        $lnk.Save()
    }
}

Write-Host "=============================================" -ForegroundColor Magenta
Write-Host "        Installation de Studio Clips" -ForegroundColor Magenta
Write-Host "  Ne ferme pas cette fenetre, tout est auto." -ForegroundColor Magenta
Write-Host "=============================================" -ForegroundColor Magenta

# Ancienne installation (dossier dézippé à la main) : on retient où elle est pour ses projets
$ancien = $null
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Studio Clips.lnk'
if (Test-Path -LiteralPath $lnk) {
    $d = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk).WorkingDirectory
    if ($d -and (Test-Path -LiteralPath (Join-Path $d 'studio.py')) -and ($d -ne $dest)) { $ancien = $d }
}

# L'appli ne doit pas tourner pendant qu'on remplace ses fichiers
# (un venv lance le vrai Python ailleurs : on reconnaît l'appli à sa ligne de commande)
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -like "*$dest*" -or ($ancien -and $_.CommandLine -like "*$ancien*")) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 800

Write-Host ""; Write-Host "==> Copie de l'application" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$tmp = Join-Path $env:TEMP ("studio_setup_" + [guid]::NewGuid().ToString('N'))
Expand-Archive -LiteralPath (Join-Path $ici 'app.zip') -DestinationPath $tmp -Force
$racine = (Get-ChildItem -LiteralPath $tmp -Recurse -Filter 'studio.py' | Select-Object -First 1).DirectoryName
Copy-Item -Path (Join-Path $racine '*') -Destination $dest -Recurse -Force
Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "    OK : $dest" -ForegroundColor Green
# Le raccourci arrive TOUT DE SUITE (l'installation complète peut prendre 30 min)
Creer-Raccourcis $dest
Write-Host "    OK : raccourci 'Studio Clips' sur le Bureau" -ForegroundColor Green

if ($ancien -and (Test-Path -LiteralPath (Join-Path $ancien 'projets'))) {
    Write-Host "    Recuperation de tes projets de l'ancienne version..."
    New-Item -ItemType Directory -Force -Path (Join-Path $dest 'projets') | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $ancien 'projets') -Directory | ForEach-Object {
        $cible = Join-Path (Join-Path $dest 'projets') $_.Name
        if (-not (Test-Path -LiteralPath $cible)) { Copy-Item -LiteralPath $_.FullName -Destination $cible -Recurse }
    }
}

# Python, FFmpeg, IA, raccourci : c'est l'installateur de l'appli qui s'en charge
& (Join-Path $dest 'installer.ps1') -Auto
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""; Write-Host "==> Ouverture de Studio Clips..." -ForegroundColor Green
Start-Process -FilePath (Join-Path $dest '.venv\Scripts\pythonw.exe') -ArgumentList "`"$(Join-Path $dest 'lanceur.py')`"" -WorkingDirectory $dest
Start-Sleep -Seconds 3
