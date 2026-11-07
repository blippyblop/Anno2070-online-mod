# Update an EXISTING install in place, keeping the player's own files.
#
#   powershell -ExecutionPolicy Bypass -File packaging\update-install.ps1 `
#       -Game 'C:\projects\Anno 2070' -Package C:\pkgu.zip
#
# Copies PLAY/PROGRESS/INSTALL/UNINSTALL, server/, docs/, loader/, tests/ from
# the package over an install that was extracted straight into the game folder,
# and:
#   * never overwrites an existing config.ini (name/ports/game path are the
#     player's),
#   * PARKS server\state.json + .bak before replacing server\ (the save lives
#     in there - without this an update silently wipes the player's progress),
#   * backs up everything it touches to _backup-before-update-<date>.zip,
#   * leaves the loader DLL alone when it is already current,
#   * prints a verification block (version, single-launch, name terminator fix,
#     state report, uninstaller, imports).
param(
    [string]$Game    = 'C:\projects\Anno 2070',
    [string]$Package = 'C:\pkgu.zip'
)
$ErrorActionPreference = 'Stop'
$game = $Game
$stamp = '2026-11-07'
$backup = Join-Path $game ("_backup-before-update-" + $stamp + ".zip")
$stage = Join-Path $env:TEMP 'annoupdate'

# ---- 1. back up everything this update may overwrite ---------------------
$names = @('PLAY.bat','PROGRESS.bat','SELFTEST.bat','INSTALL.bat','UNINSTALL.bat','config.ini',
           'README.md','VERSION','LICENSE')
$present = @()
foreach ($n in $names) { if (Test-Path (Join-Path $game $n)) { $present += (Join-Path $game $n) } }
foreach ($d in @('server','docs','loader','tests','data')) {
    $p = Join-Path $game $d
    if (Test-Path $p) { $present += $p }
}
if ($present.Count -gt 0) {
    if (Test-Path $backup) { Remove-Item -Force $backup }
    Compress-Archive -Path $present -DestinationPath $backup -Force
    Write-Output ("backup : {0} ({1:N0} KB)" -f $backup, ((Get-Item $backup).Length / 1KB))
} else {
    Write-Output 'backup : nothing to back up'
}

# ---- 2. unpack the new package ------------------------------------------
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage | Out-Null
Expand-Archive -Path $Package -DestinationPath $stage
$src = Join-Path $stage 'Anno2070Online'
if (-not (Test-Path (Join-Path $src 'PLAY.bat'))) { throw 'the uploaded zip has no PLAY.bat' }
Write-Output ("update : " + (Get-Content (Join-Path $src 'VERSION') -Raw).Trim() + "  from " + $src)

# ---- 3. copy the runtime in place, keeping the player's own files --------
# the SAVE lives inside server\ (state.json + .bak): park it before replacing
# the folder and put it back, otherwise an update wipes the player's progress
$saveDir = Join-Path $env:TEMP ('annosave-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $saveDir | Out-Null
$parked = @()
foreach ($n in @('state.json','state.json.bak')) {
    $f = Join-Path $game ('server\' + $n)
    if (Test-Path $f) { Copy-Item -Force $f (Join-Path $saveDir $n); $parked += $n }
}
foreach ($d in @('server','docs','loader','tests')) {
    $to = Join-Path $game $d
    if (Test-Path $to) { Remove-Item -Recurse -Force $to }
    Copy-Item -Recurse -Force (Join-Path $src $d) $to
}
foreach ($n in $parked) {
    Copy-Item -Force (Join-Path $saveDir $n) (Join-Path $game ('server\' + $n))
}
if ($parked.Count -gt 0) { Write-Output ('save   : kept ' + ($parked -join ', ')) }
foreach ($n in @('PLAY.bat','PROGRESS.bat','SELFTEST.bat','INSTALL.bat','UNINSTALL.bat','README.md',
                 'VERSION','LICENSE','READ-ME-FIRST.txt')) {
    $from = Join-Path $src $n
    if (Test-Path $from) { Copy-Item -Force $from (Join-Path $game $n) }
}
# config.ini: only if the player has none (never overwrite a name/ports)
$cfg = Join-Path $game 'config.ini'
if (-not (Test-Path $cfg)) {
    Copy-Item -Force (Join-Path $src 'config.ini') $cfg
    Write-Output 'config : installed a fresh config.ini (name = Player)'
} else {
    Write-Output 'config : kept the existing config.ini'
}

# ---- 4. the loader next to Anno5.exe ------------------------------------
$dll = Join-Path $game 'upc_r2_loader64.dll'
$newDll = Join-Path $src 'loader\bin\upc_r2_loader64.dll'
$oldHash = if (Test-Path $dll) { (Get-FileHash $dll -Algorithm MD5).Hash.ToLower() } else { 'missing' }
$newHash = (Get-FileHash $newDll -Algorithm MD5).Hash.ToLower()
if ($oldHash -ne $newHash) {
    if (Test-Path $dll) {
        $bak = $dll + '.original-backup'
        if (-not (Test-Path $bak)) { Copy-Item -Force $dll $bak }
    }
    Copy-Item -Force $newDll $dll
    Write-Output "loader : replaced ($oldHash -> $newHash)"
} else {
    Write-Output "loader : already current ($newHash)"
}

# ---- 5. verify ----------------------------------------------------------
Write-Output '--- verify ---'
Write-Output ("version      : " + (Get-Content (Join-Path $game 'VERSION') -Raw).Trim())
$play = Get-Content (Join-Path $game 'PLAY.bat') -Raw
Write-Output ("single launch: " + (-not $play.Contains('attempt')) + "  state-file: " + $play.Contains('--state-file'))
$qstr = Get-Content (Join-Path $game 'server\rv_shapes\__init__.py') -Raw
Write-Output ("name terminator fix: " + $qstr.Contains('len(b) + 1'))
Write-Output ("state report : " + (Test-Path (Join-Path $game 'server\state_report.py')))
Write-Output ("uninstaller  : " + (Test-Path (Join-Path $game 'UNINSTALL.bat')))
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python310\python.exe' }
& $py -c "import sys; sys.path.insert(0, r'$game\server'); import mimic_server, rmc_services, content_events, content_voting, state_report; print('imports    : ok', len(rmc_services.METHODS), 'methods')"
