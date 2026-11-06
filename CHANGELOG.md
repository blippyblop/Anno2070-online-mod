# Changelog

## Unreleased

Added
- **Uninstaller.** `UNINSTALL.bat` for the PLAY.bat package and
  `Uninstall-Anno2070Online.exe` for the .exe build (also a Start Menu entry).
  It stops a running launcher/server, restores Ubisoft's original
  `upc_r2_loader64.dll` from the backup INSTALL.bat now keeps, deletes our
  `upc_stub.log`, keeps `state.json` + `config.ini` in
  `%LOCALAPPDATA%\Anno2070Online-backup`, then removes the runtime folder and
  the shortcuts. `--dry-run` shows the plan without touching anything,
  `--purge` / `/purge` drops the saved state as well. The game's own files and
  savegames are never touched; a loader DLL that is not ours is left alone.
- INSTALL.bat and Setup.exe keep the original loader as
  `upc_r2_loader64.dll.original-backup` before copying ours, so uninstalling is
  exact.
- Test: `tests/test_uninstall.py` builds a fake game + runtime in a temp folder
  and checks restore/delete/keep/purge/dry-run/foreign-DLL behaviour (runs in CI).
- CI: the frozen uninstaller is smoke-tested with `--dry-run`, and
  `Uninstall-Anno2070Online.exe` is attached to every release.

Fixed
- The uninstaller refuses to delete a folder that is not ours (`--dir` misuse),
  and refuses to run while Anno 2070 is up instead of force-killing it - a hard
  kill can truncate `Config\Engine.ini`.

## 0.1.1 - 2026-11-06

**The bootstrap crash is fixed.** Failed launches no longer die with an access
violation; they end up at the menu in offline mode instead.

Fixed
- Loader: the queue push into a destroyed `CBackEndServiceImpl` pool is now
  skipped (guard on the pool's critical-section pointer, which is NULL only
  after teardown). This was the `0xc0000005` write to `+8` that killed roughly
  one launch in three.
- Loader: the `rdv::ConnectionManager` constructor probe is disabled. Its
  displaced bytes contained a RIP-relative `lea`, so the trampoline computed a
  garbage vtable pointer and stored it on the object **every boot**.
- Loader: the probe that could execute garbage on a PRUDP datagram was reviewed
  and is correct as written (its displaced prologue ends exactly on an
  instruction boundary) - no change needed, documented so it is not "fixed"
  later by mistake.

Changed
- Server: RMC replies are no longer delayed by 200 ms by default
  (`RV_DELAY_MS` stays as a knob). The delay guaranteed a call was in flight
  when the client's own teardown fired, which is what turned the race into a
  crash.
- Server: `us-sdkClientHttpConfig` timing values can be overridden per run
  (`RV_HTTPCFG_TIMEOUT_MS`, `RV_HTTPCFG_POPEVENTS_MS`, `RV_HTTPCFG_REST_MS`,
  `RV_HTTPCFG_CREATESESSION_REST_MS`) for measurement.
- Launcher: a launch that has not reached the login burst by 60 s is treated as
  failed, closed (forcefully if needed - the game ignores a close request) and
  retried, up to 3 attempts.

Known
- About 6 launches in 10 still fail to come up online. The remaining failure is
  a race inside the game's own client: it cancels its login at a fixed ~36 s and
  the server's replies arrive too late to matter. Restarting the game is the
  workaround; 1-3 attempts normally succeed.
- Conquest/multiplayer remains unreachable (see `docs/known-issues.md`).

## 0.1.0 - 2026-11-06

First packaged release: installer + portable build, online session, ARK item
box, senate and World Council elections and votes, world events, profile,
chat/friends/mail.
