# Build FeedWire - Sporty Bet for Windows (run on Windows - PyInstaller doesn't cross-compile).
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
#   signtool sign /fd sha256 /a "dist\FeedWire - Sporty Bet\FeedWire - Sporty Bet.exe"

Compress-Archive -Path "dist\FeedWire - Sporty Bet" -DestinationPath "dist\FeedWire-SportyBet-windows.zip" -Force
Write-Host "built: dist\FeedWire - Sporty Bet\ and dist\FeedWire-SportyBet-windows.zip"

# Inno Setup installer - preferred distribution artifact. Clients running the
# exe straight from inside the zip get "Failed to load python312.dll" because
# _internal\ never gets extracted; an installer eliminates that failure mode.
$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )) {
        if (Test-Path $candidate) { $iscc = @{ Source = $candidate }; break }
    }
}
if ($iscc) {
    & $iscc.Source "packaging\installer_windows.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed (exit $LASTEXITCODE)" }
    Get-ChildItem dist\FeedWire-SportyBet-Setup-*.exe | ForEach-Object {
        Write-Host "built: $($_.FullName) (installer - ship this one)"
    }
} else {
    Write-Warning "Inno Setup 6 not found - skipping installer. Install from https://jrsoftware.org/isdl.php and re-run to produce dist\FeedWire-SportyBet-Setup-*.exe"
}
