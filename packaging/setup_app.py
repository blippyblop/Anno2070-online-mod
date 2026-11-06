#!/usr/bin/env python3
"""Setup.exe - install "Anno 2070 - Local Online" for a player.

No Python, no terminal commands for the user. It:

  1. extracts the bundled run-time (the launcher exe + the server) into
     %LOCALAPPDATA%\\Anno2070Online
  2. finds Anno 2070 (config.ini, then the Steam registry key, then the
     usual install folders)
  3. copies upc_r2_loader64.dll next to Anno5.exe
  4. writes config.ini with the player's name
  5. creates a Desktop and a Start Menu shortcut to the launcher

Non-interactive flags (used by the automated test, also handy for scripted
installs):  --yes  --name <name>  --game <path>  --dir <install dir>
"""
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

APP_NAME = "Anno 2070 - Local Online"
FOLDER_NAME = "Anno2070Online"
LAUNCHER = "Anno2070Online.exe"

GAME_CANDIDATES = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Anno 2070",
    r"C:\Program Files\Steam\steamapps\common\Anno 2070",
    r"C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\games\Anno 2070",
    r"C:\Program Files\Ubisoft\Ubisoft Game Launcher\games\Anno 2070",
    r"D:\Steam\steamapps\common\Anno 2070",
    r"D:\Games\Anno 2070",
    r"C:\Games\Anno 2070",
]


def payload_dir():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return base


def arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def say(text=""):
    print(text, flush=True)


def find_game(hint=None):
    if hint and os.path.isfile(os.path.join(hint, "Anno5.exe")):
        return hint
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
        with open("C:/Program Files (x86)/Steam/steamapps/libraryfolders.vdf", "r",
                  encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"path"' in line:
                    lib = line.split('"')[3].replace("\\\\", "\\")
                    cand = os.path.join(lib, "steamapps", "common", "Anno 2070")
                    if os.path.isfile(os.path.join(cand, "Anno5.exe")):
                        return cand
    except OSError:
        pass
    for cand in GAME_CANDIDATES:
        if os.path.isfile(os.path.join(cand, "Anno5.exe")):
            return cand
    return None


def install_dir():
    explicit = arg("--dir")
    if explicit:
        return os.path.abspath(explicit)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, FOLDER_NAME)


def copy_tree(src, dst):
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(root, name), os.path.join(target, name))


def extract_runtime(dst):
    payload = payload_dir()
    zpath = os.path.join(payload, "payload.zip")
    if not os.path.isfile(zpath):
        raise SystemExit(f"corrupt installer: payload.zip missing next to {payload}")
    tmp = tempfile.mkdtemp(prefix="anno-online-")
    with zipfile.ZipFile(zpath) as zf:
        zf.extractall(tmp)
    # the zip holds a single top folder (the built launcher directory)
    entries = [e for e in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, e))]
    src = os.path.join(tmp, entries[0]) if len(entries) == 1 else tmp
    copy_tree(src, dst)
    shutil.rmtree(tmp, ignore_errors=True)


def write_config(dst, name, game):
    src = os.path.join(payload_dir(), "config.ini")
    target = os.path.join(dst, "config.ini")
    if os.path.isfile(target):
        # keep a player's existing name/ports, refresh only the game path
        lines = open(target, "r", encoding="utf-8-sig").read().splitlines()
    else:
        lines = open(src, "r", encoding="utf-8-sig").read().splitlines()
    out, section = [], None
    for line in lines:
        st = line.strip()
        if st.startswith("[") and st.endswith("]"):
            section = st[1:-1].strip().lower()
        elif section == "player" and "=" in st and not st.startswith((";", "#")):
            if st.split("=", 1)[0].strip().lower() == "name":
                line = f"name = {name}"
        elif section == "game" and "=" in st and not st.startswith((";", "#")):
            if st.split("=", 1)[0].strip().lower() == "path":
                line = f"path = {game}"
        out.append(line)
    with open(target, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write("\n".join(out) + "\n")
    return target


def make_shortcut(exe_path, link_path, working_dir):
    ps = (
        "$W = New-Object -ComObject WScript.Shell; "
        f"$S = $W.CreateShortcut('{link_path}'); "
        f"$S.TargetPath = '{exe_path}'; "
        f"$S.WorkingDirectory = '{working_dir}'; "
        f"$S.IconLocation = '{exe_path},0'; "
        f"$S.Description = '{APP_NAME}'; "
        "$S.Save()"
    )
    try:
        subprocess.run(["powershell", "-nop", "-ExecutionPolicy", "Bypass",
                        "-Command", ps], check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception as exc:
        say(f"      could not create {os.path.basename(link_path)}: {exc!r}")
        return False


def main():
    say("=" * 69)
    say(f" {APP_NAME} : setup")
    say("=" * 69)

    name = arg("--name")
    if not name and "--yes" not in sys.argv:
        try:
            name = input("Your player name (shown online): ").strip()
        except (EOFError, KeyboardInterrupt):
            name = ""
    name = (name or "Player")[:32]

    say("[1/6] looking for Anno 2070")
    game = find_game(arg("--game"))
    if not game:
        say("      Could not find Anno5.exe.")
        game = input("      Full path to your Anno 2070 folder: ").strip().strip('"')
        if not os.path.isfile(os.path.join(game, "Anno5.exe")):
            say("      That folder does not contain Anno5.exe - stopping.")
            return 1
    say(f"      found: {game}")

    dst = install_dir()
    say(f"[2/6] installing the runtime to {dst}")
    os.makedirs(dst, exist_ok=True)
    extract_runtime(dst)

    say("[3/6] installing the loader into the game folder")
    loader = os.path.join(payload_dir(), "upc_r2_loader64.dll")
    if not os.path.isfile(loader):
        loader = os.path.join(dst, "upc_r2_loader64.dll")
    if not os.path.isfile(loader):
        say("      ERROR: upc_r2_loader64.dll is missing from the installer")
        return 1
    try:
        shutil.copy2(loader, os.path.join(game, "upc_r2_loader64.dll"))
    except OSError as exc:
        say(f"      ERROR: could not copy into the game folder: {exc}")
        say("      Close Anno 2070 and try again (or run as administrator).")
        return 1
    say("      installed")

    say("[4/6] writing config.ini")
    cfg = write_config(dst, name, game)
    say(f"      {cfg}")

    say("[5/6] creating shortcuts")
    exe = os.path.join(dst, LAUNCHER)
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    made = 0
    if os.path.isdir(desktop):
        made += make_shortcut(exe, os.path.join(desktop, f"{APP_NAME}.lnk"), dst)
    start_menu = os.path.join(os.environ.get("APPDATA", ""),
                              "Microsoft", "Windows", "Start Menu", "Programs")
    if os.path.isdir(start_menu):
        made += make_shortcut(exe, os.path.join(start_menu, f"{APP_NAME}.lnk"), dst)

    say("[6/6] done")
    say()
    say(f" Start the game with:  {exe}")
    if made:
        say(" A shortcut was added to your Desktop and Start Menu.")
    say(f" Your name and settings live in: {cfg}")
    say()
    say(" Nothing was added to the game's own files except the loader DLL, and")
    say(" it can be removed by deleting upc_r2_loader64.dll from the game folder.")
    if "--yes" not in sys.argv:
        try:
            input("\nPress Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
