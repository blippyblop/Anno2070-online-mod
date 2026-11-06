# Build the two Windows executables from source.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build-exes.ps1
#
# Produces, in dist\:
#   Anno2070Online\Anno2070Online.exe        the launcher (server bundled)
#   Anno2070Online-Setup.exe                 the installer (payload embedded)
#
# Needs Python 3.8+ with pip; PyInstaller is installed if missing. The build is
# reproducible from this repository alone - no game files are used.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {
    $cand = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python310\python.exe'
    if (Test-Path $cand) { $py = $cand } else { throw 'python not found' }
}
Write-Host "python: $py"

& $py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'installing pyinstaller...'
    & $py -m pip install --disable-pip-version-check --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'could not install pyinstaller' }
}

Write-Host '--- building the launcher (onedir)'
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist\Anno2070Online) { Remove-Item -Recurse -Force dist\Anno2070Online }
# The launcher imports the server modules from server\ at run time, so
# PyInstaller cannot see them. Point it at that folder and name every module
# explicitly, otherwise its dependency analysis misses the stdlib imports the
# server makes (json, struct, ...) and --server mode dies with
# "No module named 'json'" inside a frozen build.
$serverMods = @(
    'mimic_server', 'rmc_services', 'prudp_transport', 'conquest_http',
    'conquest_shapes', 'content_voting', 'content_events',
    'rv_shapes', 'rv_shapes.login', 'rv_shapes.profile', 'rv_shapes.voting',
    'rv_shapes.backend', 'rv_shapes.auth', 'rv_shapes.matchmaking'
)
$hidden = $serverMods | ForEach-Object { "--hidden-import=$_" }
& $py -m PyInstaller --noconfirm --onedir --console --name Anno2070Online `
    --distpath dist --workpath build --specpath build `
    --paths server @hidden packaging\launcher_app.py
if ($LASTEXITCODE -ne 0) { throw 'launcher build failed' }

Write-Host '--- bundling the server next to the launcher'
Copy-Item -Recurse -Force server dist\Anno2070Online\server
Copy-Item -Force config.ini dist\Anno2070Online\config.ini
Copy-Item -Force docs\known-issues.md dist\Anno2070Online\known-issues.md
Copy-Item -Force loader\bin\upc_r2_loader64.dll dist\Anno2070Online\upc_r2_loader64.dll

Write-Host '--- packing the payload'
$payload = Join-Path $root 'build\payload.zip'
if (Test-Path $payload) { Remove-Item -Force $payload }
Compress-Archive -Path (Join-Path $root 'dist\Anno2070Online') -DestinationPath $payload
Write-Host ("payload: {0:N0} KB" -f ((Get-Item $payload).Length / 1KB))

Write-Host '--- building the installer (onefile)'
if (Test-Path dist\Anno2070Online-Setup.exe) { Remove-Item -Force dist\Anno2070Online-Setup.exe }
& $py -m PyInstaller --noconfirm --onefile --console --name Anno2070Online-Setup `
    --distpath dist --workpath build --specpath build `
    --add-data "$payload;." `
    --add-data "$root\loader\bin\upc_r2_loader64.dll;." `
    --add-data "$root\config.ini;." `
    packaging\setup_app.py
if ($LASTEXITCODE -ne 0) { throw 'installer build failed' }

Write-Host ''
Write-Host 'built:'
Get-ChildItem dist | ForEach-Object { Write-Host ("  {0}" -f $_.Name) }
Get-Item dist\Anno2070Online-Setup.exe | ForEach-Object {
    Write-Host ("  Anno2070Online-Setup.exe  {0:N0} KB" -f ($_.Length / 1KB))
}
