#!/usr/bin/env python3
"""Uninstall.exe - remove "Anno 2070 - Local Online" from this PC.

Undoes exactly what setup_app.py did, and nothing else:

  1. stops a launcher or a local server that is still running (the game is
     never killed by force - a hard kill can truncate Config\\Engine.ini and
     every later launch then fails, see docs/known-issues.md)
  2. puts Ubisoft's own upc_r2_loader64.dll back from the backup the installer
     takes before it copies ours; if there is no backup and the DLL in the game
     folder is ours, it is deleted and you are told how to get the original
     back (Steam: Verify integrity of game files)
  3. deletes our upc_stub.log from the game folder
  4. keeps your saved online state (server\\state.json) and config.ini in
     %LOCALAPPDATA%\\Anno2070Online-backup, then deletes the runtime folder
  5. removes the Desktop and Start Menu shortcuts

The game's own files are never touched apart from the loader DLL, which the
installer put there in the first place.

Flags (all optional):
  --yes            no questions, do it (for scripted removal)
  --purge          do not keep state.json / config.ini
  --dry-run        print what would happen, change nothing
  --game <path>    Anno 2070 folder, if it cannot be found automatically
  --dir <path>     runtime folder (default %LOCALAPPDATA%\\Anno2070Online)
  --backup-dir <p> where to keep state.json / config.ini
  --no-pause       do not wait for Enter at the end
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

APP_NAME = "Anno 2070 - Local Online"
FOLDER_NAME = "Anno2070Online"
LAUNCHER = "Anno2070Online.exe"
LOADER = "upc_r2_loader64.dll"
LOG_FILE = "upc_stub.log"
# the name this installer uses, plus the one the project itself used on the
# development box, in case a user copied that layout over
LOADER_BACKUPS = (LOADER + ".original-backup",
                  LOADER + ".ubisoft-orig.bak",
                  LOADER + ".original")
# a plain ASCII string that only our loader contains (Ubisoft's does not), used
# to tell "our DLL" from "the player's own DLL we never replaced"
LOADER_MARKER = b"upc_stub.log"
SHORTCUTS = (APP_NAME + ".lnk", "Uninstall " + APP_NAME + ".lnk")

GAME_CANDIDATES = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Anno 2070",
    r"C:\Program Files\Steam\steamapps\common\Anno 2070",
    r"C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\games\Anno 2070",
    r"C:\Program Files\Ubisoft\Ubisoft Game Launcher\games\Anno 2070",
    r"D:\Steam\steamapps\common\Anno 2070",
    r"D:\Games\Anno 2070",
    r"C:\Games\Anno 2070",
]


# ----------------------------------------------------------------- helpers
def say(text=""):
    print(text, flush=True)


def arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def have(flag):
    return flag in sys.argv


def on_windows():
    return os.name == "nt"


def is_inside(path, folder):
    try:
        path = os.path.abspath(path).lower()
        folder = os.path.abspath(folder).lower()
    except (TypeError, ValueError):
        return False
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


def install_dir():
    explicit = arg("--dir")
    if explicit:
        return os.path.abspath(explicit)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, FOLDER_NAME)


def read_config_value(cfg, section, key):
    """Value of `key` inside `[section]` of an ini file, or ''."""
    try:
        with open(cfg, "r", encoding="utf-8-sig", errors="replace") as fh:
            current = None
            for line in fh:
                st = line.strip()
                if st.startswith("[") and st.endswith("]"):
                    current = st[1:-1].strip().lower()
                    continue
                if current != section or "=" not in st:
                    continue
                if st.startswith((";", "#")):
                    continue
                k, v = st.split("=", 1)
                if k.strip().lower() == key:
                    return v.strip()
    except OSError:
        pass
    return ""


def find_game(hint=None):
    if hint and os.path.isfile(os.path.join(hint, "Anno5.exe")):
        return os.path.abspath(hint)
    if not on_windows():
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
        cand = os.path.join(steam.replace("/", "\\"), "steamapps", "common", "Anno 2070")
        if os.path.isfile(os.path.join(cand, "Anno5.exe")):
            return cand
    except Exception:
        pass
    try:
        with open(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf", "r",
                  encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"path"' in line:
                    lib = line.split('"')[3].replace("\\\\", "\\")
                    cand = os.path.join(lib, "steamapps", "common", "Anno 2070")
                    if os.path.isfile(os.path.join(cand, "Anno5.exe")):
                        return cand
    except (OSError, IndexError):
        pass
    for cand in GAME_CANDIDATES:
        if os.path.isfile(os.path.join(cand, "Anno5.exe")):
            return cand
    return None


def process_list():
    """Image names of the processes we care about, best effort."""
    if not on_windows():
        return []
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=25,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        names = {line.split('","')[0].strip('"').lower()
                 for line in (out.stdout or "").splitlines() if line.startswith('"')}
    except Exception:
        return []
    return [n for n in ("anno5.exe", "anno2070online.exe") if n in names]


def stop_process(image):
    """Ask a program to close, then force it if it refuses (our processes only)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for args in (["taskkill", "/im", image],
                 ["taskkill", "/f", "/im", image]):
        try:
            subprocess.run(args, capture_output=True, timeout=25, creationflags=flags)
        except Exception:
            pass
        time.sleep(1.5)
        if image.lower() not in process_list():
            return True
    return False


def stop_server():
    """Stop a `python ... mimic_server.py` started by PLAY.bat, if any.

    Returns True if at least one such process was found and stopped.
    """
    if not on_windows():
        return False
    ps = ("$ErrorActionPreference='SilentlyContinue';"
          "$p = @(Get-CimInstance Win32_Process | "
          "Where-Object { $_.CommandLine -like '*mimic_server.py*' });"
          "foreach ($x in $p) { Stop-Process -Id $x.ProcessId -Force };"
          "$p.Count")
    try:
        out = subprocess.run(["powershell", "-nop", "-ExecutionPolicy", "Bypass",
                              "-Command", ps], capture_output=True, text=True,
                             timeout=40,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        count = (out.stdout or "0").strip().strip('"')
        return count not in ("", "0")
    except Exception:
        return False


def is_our_loader(path):
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                if LOADER_MARKER in chunk:
                    return True
    except OSError:
        pass
    return False


def remove_tree(path, attempts=6):
    def onexc(func, target, _exc):
        try:
            os.chmod(target, 0o666)
            func(target)
        except OSError:
            pass

    for attempt in range(attempts):
        if not os.path.isdir(path):
            return True
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=onexc)
            else:
                shutil.rmtree(path, onerror=lambda f, p, e: onexc(f, p, e))
        except OSError:
            pass
        if not os.path.isdir(path):
            return True
        time.sleep(0.7 * (attempt + 1))   # a virus scanner may hold a handle
    return not os.path.isdir(path)


def looks_like_ours(folder):
    """Never delete a folder by accident: it must be our runtime.

    A folder that holds Anno5.exe is the game folder - even if the package was
    unzipped straight into it, deleting it would delete the game.
    """
    if os.path.isfile(os.path.join(folder, "Anno5.exe")):
        return False
    return (os.path.isfile(os.path.join(folder, "server", "mimic_server.py"))
            or os.path.isfile(os.path.join(folder, LAUNCHER)))


def unique_dir(path):
    if not os.path.exists(path):
        return path
    n = 2
    while os.path.exists(f"{path}-{n}"):
        n += 1
    return f"{path}-{n}"


# ----------------------------------------------------------------- steps
def uninstall_loader(game, dry_run=False):
    """Restore Ubisoft's DLL, or remove ours. Returns what happened."""
    dll = os.path.join(game, LOADER)
    if not os.path.isfile(dll):
        say("      no loader in the game folder - nothing to remove")
        return "absent"
    for name in LOADER_BACKUPS:
        bak = os.path.join(game, name)
        if os.path.isfile(bak):
            if dry_run:
                say(f"      would put the original loader back (from {name})")
                return "would-restore"
            os.replace(bak, dll)
            say(f"      original loader restored (was backed up as {name})")
            return "restored"
    if not is_our_loader(dll):
        say("      the loader in the game folder is not ours - left untouched")
        return "foreign"
    if dry_run:
        say("      would delete our loader (no backup of the original was found)")
        return "would-delete"
    try:
        os.remove(dll)
    except OSError as exc:
        say(f"      could not delete the loader: {exc}")
        return "error"
    say("      our loader deleted; no backup of Ubisoft's original was found")
    say("      -> Steam: right click Anno 2070 > Properties > Installed files >")
    say("         Verify integrity of game files (restores the original DLL)")
    return "deleted"


def remove_log(game, dry_run=False):
    for name in (LOG_FILE, LOG_FILE + ".prev"):
        path = os.path.join(game, name)
        if os.path.isfile(path):
            if dry_run:
                say(f"      would delete {path}")
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass


def backup_state(runtime, target, dry_run=False):
    """Keep state.json + config.ini so an uninstall does not lose progress."""
    wanted = [os.path.join("server", "state.json"), "config.ini"]
    present = [w for w in wanted if os.path.isfile(os.path.join(runtime, w))]
    if not present:
        return None
    if dry_run:
        say(f"      would keep {', '.join(present)} in {target}")
        return target
    os.makedirs(target, exist_ok=True)
    kept = []
    for rel in present:
        dst = os.path.join(target, os.path.basename(rel))
        try:
            shutil.copy2(os.path.join(runtime, rel), dst)
            kept.append(os.path.basename(rel))
        except OSError:
            pass
    return target if kept else None


def shortcut_dirs():
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    programdata = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return [os.path.join(home, "Desktop"),
            os.path.join(programdata, "Desktop"),
            os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs"),
            os.path.join(programdata, "Microsoft", "Windows", "Start Menu", "Programs")]


def remove_shortcuts(dry_run=False):
    if not on_windows():
        return 0
    removed = 0
    for folder in shortcut_dirs():
        if not os.path.isdir(folder):
            continue
        for name in SHORTCUTS:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                if dry_run:
                    say(f"      would delete {path}")
                else:
                    try:
                        os.remove(path)
                        removed += 1
                    except OSError:
                        pass
    return removed


def relocate_self(targets):
    """A frozen exe cannot delete the folder it runs from - copy to %TEMP%."""
    if not getattr(sys, "frozen", False):
        return False
    exe = os.path.abspath(sys.executable)
    if not any(is_inside(exe, t) for t in targets if t):
        return False
    if os.environ.get("ANNO_UNINSTALL_RESUMED") == "1":
        return False
    tmp = os.path.join(tempfile.gettempdir(), "Anno2070Online-Uninstall.exe")
    # remember the extra folders (the exe may have been unzipped next to the
    # runtime instead of into %LOCALAPPDATA%) - the %TEMP% copy cannot see the
    # original location any more
    extra = []
    if "--also" not in sys.argv:
        for target in targets:
            if target and os.path.abspath(target) != install_dir():
                extra += ["--also", os.path.abspath(target)]
    try:
        shutil.copy2(exe, tmp)
        env = dict(os.environ, ANNO_UNINSTALL_RESUMED="1",
                   ANNO_UNINSTALL_TEMP=tmp)
        subprocess.Popen([tmp] + sys.argv[1:] + extra, env=env)
        return True
    except OSError as exc:
        say(f"      (could not move myself to %TEMP%: {exc} - continuing)")
        return False


def remove_roots(runtime):
    """The runtime folder plus any other copy we were started from."""
    roots = [runtime]
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    for extra in (here, arg("--also")):
        if not extra:
            continue
        extra = os.path.abspath(extra)
        if extra not in roots and looks_like_ours(extra):
            roots.append(extra)
    return roots


def schedule_self_delete():
    """Delete the %TEMP% copy once we are gone."""
    tmp = os.environ.get("ANNO_UNINSTALL_TEMP")
    if not tmp or not on_windows():
        return
    try:
        subprocess.Popen(
            ["cmd", "/c", "ping -n 4 127.0.0.1 >nul & del /f /q \"%s\"" % tmp],
            creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0) |
                           getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            close_fds=True)
    except Exception:
        pass


# ------------------------------------------------------------------ main
def main():
    dry_run = have("--dry-run")
    assume_yes = have("--yes")
    purge = have("--purge")
    runtime = install_dir()
    roots = remove_roots(runtime)

    say("=" * 69)
    say(f" {APP_NAME} : uninstall")
    say("=" * 69)

    if relocate_self(roots):
        say("      restarted from %TEMP% so the install folder can be removed")
        return 0

    cfg = os.path.join(runtime, "config.ini")
    game = find_game(arg("--game") or read_config_value(cfg, "game", "path"))
    say(f"Runtime     : {runtime}{'' if os.path.isdir(runtime) else '  (not present)'}")
    say(f"Game folder : {game or '(not found - the loader will be left alone)'}")

    # -- running programs ---------------------------------------------------
    live = process_list()
    if "anno5.exe" in live:
        say()
        say(" Anno 2070 is still running. Quit it from the game's own menu and")
        say(" run this again - a forced kill can truncate Config\\Engine.ini and")
        say(" then every later launch fails.")
        return 2
    if "anno2070online.exe" in live:
        say(" Stopping the launcher ...")
        stop_process("anno2070online.exe")
    if stop_server():
        say(" Stopped a local server started by PLAY.bat.")

    # -- plan ---------------------------------------------------------------
    say()
    say("This will:")
    say(f"  - put Ubisoft's {LOADER} back, or remove ours if there is no backup")
    say(f"  - delete {os.path.join(game or '<game>', LOG_FILE)}")
    if purge:
        say(f"  - delete {runtime} (including your saved online state)")
    else:
        say(f"  - keep state.json + config.ini, then delete {runtime}")
    say("  - remove the Desktop and Start Menu shortcuts")
    if dry_run:
        say()
        say("(dry run - nothing will be changed)")
    elif not assume_yes:
        say()
        try:
            answer = input("Continue? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            say("Nothing was changed.")
            return 0

    # -- do it --------------------------------------------------------------
    say()
    if game:
        say("[1/4] the loader in the game folder")
        uninstall_loader(game, dry_run)
        remove_log(game, dry_run)
    else:
        say("[1/4] game folder not found - skipping the loader")
        say("      delete %s from your Anno 2070 folder by hand" % LOADER)
    say()
    say("[2/4] your saved state")
    kept = None
    if not purge:
        target = unique_dir(arg("--backup-dir") or
                            os.path.join(os.environ.get("LOCALAPPDATA") or
                                         os.path.expanduser("~"),
                                         FOLDER_NAME + "-backup"))
        source = next((r for r in roots
                       if os.path.isfile(os.path.join(r, "server", "state.json"))),
                      runtime)
        kept = backup_state(source, target, dry_run)
        if kept and not dry_run:
            say(f"      kept in {kept}")
        elif not kept:
            say("      nothing to keep")
    else:
        say("      --purge: not kept")
    say()
    say("[3/4] the runtime folder")
    for folder in roots:
        if not os.path.isdir(folder):
            say("      not present:", folder)
            continue
        if not looks_like_ours(folder):
            say("      this folder does not look like ours - not deleting it")
            say(f"      ({folder})")
        elif dry_run:
            say(f"      would delete {folder}")
        elif remove_tree(folder):
            say(f"      deleted {folder}")
        else:
            say(f"      some files are still locked - delete {folder} by hand")
    say()
    say("[4/4] shortcuts")
    gone = remove_shortcuts(dry_run)
    say(f"      removed {gone}" if gone else "      none found")

    say()
    say("=" * 69)
    if dry_run:
        say(" Dry run finished - nothing was changed.")
    else:
        say(" Done. The game is back to its normal, offline behaviour.")
        if kept:
            say(f" Your state was kept in: {kept}")
    say("=" * 69)
    say()
    schedule_self_delete()
    if not assume_yes and not have("--no-pause"):
        try:
            input("Press Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
