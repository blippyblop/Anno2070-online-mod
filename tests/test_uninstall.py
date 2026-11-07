#!/usr/bin/env python3
"""Offline test for packaging/uninstall_app.py.

Builds a fake game folder and a fake runtime folder in a temp directory, runs
the uninstaller as a subprocess and checks what it did. No game, no Windows and
no network needed - it is exactly the code that ships in Uninstall.exe.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.normpath(os.path.join(HERE, os.pardir, "packaging", "uninstall_app.py"))

# our loader contains this string, Ubisoft's does not (that is how the
# uninstaller tells them apart)
OUR_DLL = b"MZ\x90\x00 fake loader\0upc_stub.log\0PROBE \0"
UBI_DLL = b"MZ\x90\x00 ubisoft loader, no marker here\0"

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail}")


def write(path, data):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode())


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def build(root, loader=OUR_DLL, backup=None, state=True):
    game = os.path.join(root, "Anno 2070")
    runtime = os.path.join(root, "runtime")
    os.makedirs(game, exist_ok=True)
    os.makedirs(os.path.join(runtime, "server"), exist_ok=True)
    write(os.path.join(game, "Anno5.exe"), b"MZ fake game")
    if loader is not None:
        write(os.path.join(game, "upc_r2_loader64.dll"), loader)
    if backup is not None:
        write(os.path.join(game, "upc_r2_loader64.dll.original-backup"), backup)
    write(os.path.join(game, "upc_stub.log"), "PROBE hello\n")
    write(os.path.join(runtime, "server", "mimic_server.py"), "# server\n")
    write(os.path.join(runtime, "config.ini"),
          "[player]\nname = Tester\n[game]\npath = %s\n" % game)
    if state:
        write(os.path.join(runtime, "server", "state.json"), '{"votes": 3}')
    return game, runtime


def run(args):
    return subprocess.run([sys.executable, APP] + args + ["--no-pause"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=180)


def case_with_backup(root):
    print("backup exists -> original loader comes back, state is kept")
    game, runtime = build(root, backup=UBI_DLL)
    bk = os.path.join(root, "backup")
    res = run(["--yes", "--dir", runtime, "--backup-dir", bk])
    check("exit 0", res.returncode == 0, res.returncode)
    check("original DLL restored",
          read(os.path.join(game, "upc_r2_loader64.dll")) == UBI_DLL)
    check("backup file consumed",
          not os.path.exists(os.path.join(game, "upc_r2_loader64.dll.original-backup")))
    check("log deleted", not os.path.exists(os.path.join(game, "upc_stub.log")))
    check("game exe untouched", os.path.exists(os.path.join(game, "Anno5.exe")))
    check("runtime folder gone", not os.path.isdir(runtime))
    check("state kept", os.path.exists(os.path.join(bk, "state.json")))
    check("config kept", os.path.exists(os.path.join(bk, "config.ini")))


def case_no_backup(root):
    print("no backup -> our loader is deleted, with a warning")
    game, runtime = build(root)
    res = run(["--yes", "--dir", runtime, "--backup-dir", os.path.join(root, "bk")])
    check("exit 0", res.returncode == 0, res.returncode)
    check("our DLL deleted", not os.path.exists(os.path.join(game, "upc_r2_loader64.dll")))
    check("says how to get the original back", "Verify integrity" in res.stdout)


def case_foreign_dll(root):
    print("a DLL that is not ours -> left alone")
    game, runtime = build(root, loader=UBI_DLL)
    res = run(["--yes", "--dir", runtime, "--backup-dir", os.path.join(root, "bk")])
    check("exit 0", res.returncode == 0, res.returncode)
    check("foreign DLL untouched",
          read(os.path.join(game, "upc_r2_loader64.dll")) == UBI_DLL)
    check("said it is not ours", "not ours" in res.stdout)


def case_purge(root):
    print("--purge -> nothing is kept")
    game, runtime = build(root, backup=UBI_DLL)
    bk = os.path.join(root, "backup")
    res = run(["--yes", "--purge", "--dir", runtime, "--backup-dir", bk])
    check("exit 0", res.returncode == 0, res.returncode)
    check("no backup written", not os.path.exists(bk))
    check("runtime folder gone", not os.path.isdir(runtime))


def case_dry_run(root):
    print("--dry-run -> nothing changes")
    game, runtime = build(root, backup=UBI_DLL)
    res = run(["--yes", "--dry-run", "--dir", runtime, "--backup-dir", os.path.join(root, "bk")])
    check("exit 0", res.returncode == 0, res.returncode)
    check("DLL still ours",
          read(os.path.join(game, "upc_r2_loader64.dll")) == OUR_DLL)
    check("backup still there",
          os.path.exists(os.path.join(game, "upc_r2_loader64.dll.original-backup")))
    check("runtime folder still there", os.path.isdir(runtime))
    check("said it was a dry run", "nothing was changed" in res.stdout.lower())


def case_foreign_runtime(root):
    print("a folder that is not ours (--dir misuse) -> not deleted")
    junk = os.path.join(root, "not-ours")
    write(os.path.join(junk, "important.txt"), "keep me")
    res = run(["--yes", "--dir", junk, "--game", os.path.join(root, "nogame")])
    check("exit 0", res.returncode == 0, res.returncode)
    check("folder kept", os.path.exists(os.path.join(junk, "important.txt")))


def case_flat_install_removes_only_our_files(root):
    print("package extracted into the game folder -> only the mod's files go")
    game, _runtime = build(root, backup=UBI_DLL)
    write(os.path.join(game, "PLAY.bat"), "@echo off\n")
    write(os.path.join(game, "SELFTEST.bat"), "@echo off\n")
    write(os.path.join(game, "config.ini"), "[player] name = Tester\n")
    write(os.path.join(game, "server", "mimic_server.py"), "# server\n")
    write(os.path.join(game, "docs", "install.md"), "# docs\n")
    write(os.path.join(game, "data", "mimic_state.json"), "{}")
    for name in ("bin", "maindata", "locales"):
        os.makedirs(os.path.join(game, name), exist_ok=True)
        write(os.path.join(game, name, "keep.bin"), b"game data")
    res = run(["--yes", "--game", game, "--dir", game,
               "--backup-dir", os.path.join(root, "bk")])
    check("exit 0", res.returncode == 0, res.returncode)
    check("Anno5.exe survives", os.path.exists(os.path.join(game, "Anno5.exe")))
    check("the game's own folders survive",
          all(os.path.exists(os.path.join(game, n, "keep.bin"))
              for n in ("bin", "maindata", "locales")))
    check("the player's own data folder survives",
          os.path.exists(os.path.join(game, "data", "mimic_state.json")))
    check("PLAY.bat is gone", not os.path.exists(os.path.join(game, "PLAY.bat")))
    check("config.ini is gone", not os.path.exists(os.path.join(game, "config.ini")))
    check("server/ is gone", not os.path.isdir(os.path.join(game, "server")))
    check("docs/ is gone", not os.path.isdir(os.path.join(game, "docs")))
    check("the loader was restored",
          read(os.path.join(game, "upc_r2_loader64.dll")) == UBI_DLL)
    check("said only the mod's files are removed",
          "removing only the mod" in res.stdout, res.stdout[-300:])


def case_game_folder_is_never_deleted(root):
    print("the package was unzipped into the game folder -> the game is not deleted")
    game, _runtime = build(root, backup=UBI_DLL)
    # pretend the package was extracted straight into the game folder
    write(os.path.join(game, "PLAY.bat"), "@echo off\n")
    write(os.path.join(game, "server", "mimic_server.py"), "# server\n")
    write(os.path.join(game, "server", "state.json"), '{"votes": 1}')
    res = run(["--yes", "--game", game, "--dir", game,
               "--backup-dir", os.path.join(root, "bk")])
    check("exit 0", res.returncode == 0, res.returncode)
    check("Anno5.exe still there", os.path.exists(os.path.join(game, "Anno5.exe")))
    check("PLAY.bat still there", os.path.exists(os.path.join(game, "PLAY.bat")))
    check("said it refused", "not deleting it" in res.stdout)
    check("the loader was still restored",
          read(os.path.join(game, "upc_r2_loader64.dll")) == UBI_DLL)


def case_explicit_game(root):
    print("--game overrides the game path, config keeps working")
    game, runtime = build(root, backup=UBI_DLL)
    other = os.path.join(root, "other game")
    os.makedirs(other, exist_ok=True)
    write(os.path.join(other, "Anno5.exe"), b"MZ")
    write(os.path.join(other, "upc_r2_loader64.dll"), OUR_DLL)
    res = run(["--yes", "--dir", runtime, "--game", other,
               "--backup-dir", os.path.join(root, "bk")])
    check("exit 0", res.returncode == 0, res.returncode)
    check("loader removed where --game pointed",
          not os.path.exists(os.path.join(other, "upc_r2_loader64.dll")))
    check("the config.ini game folder was left alone",
          read(os.path.join(game, "upc_r2_loader64.dll")) == OUR_DLL)


def main():
    if not os.path.isfile(APP):
        # the player zip ships tests/ but not packaging/ - nothing to test there
        print("uninstaller: skipped (packaging/ is not part of this package)")
        return 0
    for case in (case_with_backup, case_no_backup, case_foreign_dll, case_purge,
                 case_dry_run, case_foreign_runtime,
                 case_flat_install_removes_only_our_files,
                 case_game_folder_is_never_deleted,
                 case_explicit_game):
        root = tempfile.mkdtemp(prefix="anno-uninstall-")
        try:
            case(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
    if failures:
        print("FAILED: %d check(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("uninstaller: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
