# Remove ChemAssistent for the current Windows user. Keeps the config.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1

$ErrorActionPreference = "Stop"

$AppName = "chem-assistent"
$AppDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$ConfigDir = Join-Path $env:APPDATA $AppName
$StateDir = Join-Path $env:LOCALAPPDATA $AppName
$PidFile = Join-Path $StateDir "app.pid"
$Programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"

if (Test-Path $PidFile) {
    $oldPid = (Get-Content $PidFile -Raw).Trim()
    $process = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -like "python*") {
        Stop-Process -Id $oldPid -Force
        $process.WaitForExit(5000) | Out-Null
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

foreach ($link in @(
    (Join-Path $Programs "ChemAssistent.lnk"),
    (Join-Path $Programs "ChemAssistent Settings.lnk"),
    (Join-Path $Programs "Startup\ChemAssistent.lnk")
)) {
    Remove-Item $link -Force -ErrorAction SilentlyContinue
}
Remove-Item $AppDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Application removed. MSYS2 and its packages were left installed."
Write-Host "Configuration was kept at:"
Write-Host "  $ConfigDir"
Write-Host "The log (may be deleted):"
Write-Host "  $StateDir"
