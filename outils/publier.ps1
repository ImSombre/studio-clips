# Publie une nouvelle version de Studio Clips sur GitHub.
# Les applis déjà installées la récupèrent toutes seules à leur prochain lancement.
#
#   .\outils\publier.ps1 -Notes "Ce qui change"              (version actuelle + 0.1)
#   .\outils\publier.ps1 -Version 3.0 -Notes "Grosse refonte"
param([string]$Version, [Parameter(Mandatory)][string]$Notes)
$ErrorActionPreference = 'Stop'
$racine = Split-Path $PSScriptRoot -Parent
$app = Join-Path $racine 'app'
$gh = "$env:ProgramFiles\GitHub CLI\gh.exe"
$u8 = New-Object System.Text.UTF8Encoding($false)
Set-Location -LiteralPath $racine

$depot = (& $gh repo view --json nameWithOwner -q .nameWithOwner)
if (-not $depot) { throw "Dépôt GitHub introuvable (gh auth login fait ?)" }

# 1. Numéro de version
$studio = Join-Path $app 'studio.py'
$code = [IO.File]::ReadAllText($studio)
$actuelle = [regex]::Match($code, '(?m)^VERSION = "([\d.]+)"').Groups[1].Value
if (-not $Version) {
    $p = $actuelle.Split('.'); $Version = "$($p[0]).$([int]$p[1] + 1)"
}
$code = [regex]::Replace($code, '(?m)^VERSION = "[\d.]+"', "VERSION = `"$Version`"")
[IO.File]::WriteAllText($studio, $code, $u8)

# 2. Le lanceur doit savoir où chercher les mises à jour
$l = Join-Path $app 'lanceur.py'
$code = [regex]::Replace([IO.File]::ReadAllText($l), '(?m)^DEPOT = ".*?"', "DEPOT = `"$depot`"")
[IO.File]::WriteAllText($l, $code, $u8)

# 3. Archives : zip de l'appli + version.json + l'installateur .exe
$dist = Join-Path $racine 'dist'
if (Test-Path $dist) { Get-ChildItem $dist | Remove-Item -Recurse -Force }
New-Item -ItemType Directory -Force "$dist\pack\studio-clips", "$dist\exe" | Out-Null
Get-ChildItem -LiteralPath $app | Where-Object { $_.Name -notin '__pycache__', 'projets', '.venv', 'bin' -and $_.Name -notmatch '\.log$' } |
    Copy-Item -Destination "$dist\pack\studio-clips" -Recurse
Compress-Archive -Path "$dist\pack\studio-clips" -DestinationPath "$dist\studio-clips.zip" -Force
@{ version = $Version; zip = "https://github.com/$depot/releases/download/v$Version/studio-clips.zip"; notes = $Notes } |
    ConvertTo-Json | Set-Content "$dist\version.json" -Encoding Ascii
Copy-Item "$dist\studio-clips.zip" "$dist\exe\app.zip"
Copy-Item (Join-Path $app 'setup.ps1') "$dist\exe\setup.ps1"
& (Join-Path $PSScriptRoot 'construire-exe.ps1') -Dossier "$dist\exe" -Sortie "$dist\Studio-Clips.exe" | Out-Null

# 4. Code + release sur GitHub (git écrit ses avertissements sur stderr : ce ne sont pas des erreurs)
$ErrorActionPreference = 'Continue'
git add -A 2>$null
git commit -q -m "Studio Clips v$Version : $Notes" 2>$null
git push -q -u origin HEAD 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "git push a echoue" }
& $gh release create "v$Version" "$dist\studio-clips.zip" "$dist\version.json" "$dist\Studio-Clips.exe" `
    --title "Studio Clips v$Version" --notes $Notes
Write-Host ""
Write-Host "Publie : v$Version" -ForegroundColor Green
Write-Host "Lien d'installation (toujours la derniere version) :" -ForegroundColor Green
Write-Host "  https://github.com/$depot/releases/latest/download/Studio-Clips.exe"
