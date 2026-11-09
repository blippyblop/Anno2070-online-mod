# loader/

`upc_r2_loader64.dll` is **our own** implementation of the small Ubisoft `upc_r2`
client API that Anno 2070 imports. Ubisoft's original DLL was installed by the
Ubisoft Game Launcher and stopped being deployed when that service was retired,
so the game refuses to start (or starts in a crippled offline mode).

* `src/upc_stub.c` — the whole thing, ~6.5k lines of C, no third-party code.
* `bin/upc_r2_loader64.dll` — the prebuilt 64-bit DLL that INSTALL.bat copies
  next to `Anno5.exe`. Build it yourself with `build.ps1` (or `build-dll.sh`
  under Linux/mingw) if you prefer.
* `bin/upc_r2_loader64.dll.politics` — the same DLL including the **opt-in Ark
  Politics patch** (`UPC_POLITICS=probe|on`). It is *not* installed by default:
  copy it over `upc_r2_loader64.dll` (after INSTALL.bat, which keeps Ubisoft's
  original as `.original-backup`) to try it. See `docs/known-issues.md` #10.

## What it does

1. **Exports the UPC API** the game imports (`UPC_Init`, ownership/state queries,
   product lists, callbacks, ...) and answers them for a single local player.
   This is what makes the game believe it has a profile.
2. **Applies a few in-memory fixes** so the game's own online bootstrap can
   complete: the SDK's "my config store is live" predicate, two use-after-free
   guards, and a compatibility path for the notification-driven parameter
   document. These are runtime writes only; `Anno5.exe` on disk is not touched.
3. **Offers diagnostics** that are all **off by default** and only enabled by
   `UPC_*` environment variables (used during development, not by players).

## Build

```
powershell -ExecutionPolicy Bypass -File build.ps1
```
Requires a mingw-w64 toolchain (`x86_64-w64-mingw32-gcc`). The result is
reproducible from `src/upc_stub.c` alone.

## Notes

* The DLL is placed in the game folder because that is where the game looks for
  `upc_r2_loader64.dll`; the file name is part of the game's import table, not a
  copy of Ubisoft code.
* Every patch is guarded: it verifies the bytes it expects before writing and
  skips (logging why) on an unknown game build, so a future game version fails
  safe instead of crashing.
