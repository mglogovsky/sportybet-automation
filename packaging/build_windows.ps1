# Build SportyPilot for Windows (run on Windows — PyInstaller doesn't cross-compile).
# Usage: powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

python -m pip install --quiet pyinstaller

if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist }
python -m PyInstaller --noconfirm --distpath dist --workpath build packaging\sportypilot.spec

# NOTE on signing: unsigned builds trigger SmartScreen. For beta a self-signed
# cert is acceptable; for real distribution use an EV code-signing cert.
# Example (once you have one):
#   signtool sign /fd sha256 /a dist\SportyPilot\SportyPilot.exe

Compress-Archive -Path dist\SportyPilot -DestinationPath dist\SportyPilot-windows.zip -Force
Write-Host "built: dist\SportyPilot\ and dist\SportyPilot-windows.zip"
