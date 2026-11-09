#!/bin/sh
# Build the loader DLL (the drop-in upc_r2_loader64.dll that redirects the game's
# online endpoints and carries the optional client patches).
#
#   ./build-dll.sh                       -> loader/bin/upc_r2_loader64.dll
#   OUT=loader/bin/upc_r2_loader64.dll.politics ./build-dll.sh
#
# Needs mingw-w64 (`x86_64-w64-mingw32-gcc`). The release DLL is built exactly
# this way; nothing else is linked in (the server is plain Python).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$HERE/bin/upc_r2_loader64.dll}
CC=${CC:-x86_64-w64-mingw32-gcc}

command -v "$CC" >/dev/null 2>&1 || {
    echo "build-dll: $CC not found (install mingw-w64)" >&2
    exit 1
}

"$CC" -shared -O2 -o "$OUT" "$HERE/src/upc_stub.c" -static
echo "build-dll: wrote $OUT ($(wc -c <"$OUT") bytes)"
