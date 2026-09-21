# Fabrique Studio-Clips.exe (IExpress, intégré à Windows) : embarque setup.ps1 + app.zip,
# et lance setup.ps1 au double-clic. Usage : construire-exe.ps1 -Dossier <dossier avec setup.ps1 + app.zip> -Sortie <exe>
param([Parameter(Mandatory)][string]$Dossier, [Parameter(Mandatory)][string]$Sortie)
$ErrorActionPreference = 'Stop'
$Dossier = (Resolve-Path $Dossier).Path.TrimEnd('\') + '\'
$sed = Join-Path $env:TEMP "studioclips-$([guid]::NewGuid().ToString('N')).sed"
@"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=%DisplayLicense%
FinishMessage=%FinishMessage%
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=%PostInstallCmd%
AdminQuietInstCmd=%AdminQuietInstCmd%
UserQuietInstCmd=%UserQuietInstCmd%
SourceFiles=SourceFiles
[Strings]
InstallPrompt=
DisplayLicense=
FinishMessage=
TargetName=$Sortie
FriendlyName=Studio Clips
AppLaunched=cmd /c powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
FILE0="setup.ps1"
FILE1="app.zip"
[SourceFiles]
SourceFiles0=$Dossier
[SourceFiles0]
%FILE0%=
%FILE1%=
"@ | Set-Content -LiteralPath $sed -Encoding Ascii
if (Test-Path -LiteralPath $Sortie) { Remove-Item -LiteralPath $Sortie -Force }
$p = Start-Process -FilePath "$env:WINDIR\System32\iexpress.exe" -ArgumentList "/N", "/Q", $sed -Wait -PassThru
Remove-Item -LiteralPath $sed -Force -ErrorAction SilentlyContinue
if (-not (Test-Path -LiteralPath $Sortie)) { throw "IExpress n'a pas produit $Sortie (code $($p.ExitCode))" }
Get-Item -LiteralPath $Sortie
