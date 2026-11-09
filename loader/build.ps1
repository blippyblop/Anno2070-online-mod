# Build the loader DLL from source.
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$gcc = 'x86_64-w64-mingw32-gcc'
if (-not (Get-Command $gcc -ErrorAction SilentlyContinue)) {
    throw "mingw-w64 not found: install it and put $gcc on PATH"
}
New-Item -ItemType Directory -Force -Path "$here\bin" | Out-Null
# OUT lets you build the opt-in variant (see docs/known-issues.md #10):
#   $env:OUT = "$here\bin\upc_r2_loader64.dll.politics"
$out = if ($env:OUT) { $env:OUT } else { "$here\bin\upc_r2_loader64.dll" }
& $gcc -shared -O2 -o $out "$here\src\upc_stub.c" -static
if ($LASTEXITCODE -ne 0) { throw "build failed" }
Write-Host "built $out"
